import os
import asyncio
import aiohttp
import json
from datetime import datetime
from aiogram import Bot, Dispatcher, Router, F
from aiogram.types import Message, CallbackQuery, BotCommand, BotCommandScopeDefault
from aiogram.filters import Command
from aiogram.utils.keyboard import InlineKeyboardBuilder
from aiogram.exceptions import TelegramBadRequest
from dotenv import load_dotenv

# --- Конфигурация и константы ---
load_dotenv()
TELEGRAM_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
META_TOKEN = os.getenv("META_ACCESS_TOKEN")
API_VERSION = "v19.0"
LEAD_ACTION_TYPE = "onsite_conversion.messaging_conversation_started_7d"
LINK_CLICK_ACTION_TYPE = "link_click"

# --- Инициализация ---
bot = Bot(token=TELEGRAM_TOKEN, parse_mode="HTML")
dp = Dispatcher()
router = Router()

# Словарь для хранения ID сообщений для последующей очистки
sent_messages_by_chat = {}

# ============================
# ===     API (Общие)       ===
# ============================

async def fb_get(session: aiohttp.ClientSession, url: str, params: dict = None):
    """Асинхронная функция для выполнения GET-запросов к Graph API."""
    params = params or {}
    params["access_token"] = META_TOKEN
    async with session.get(url, params=params) as response:
        response.raise_for_status()
        return await response.json()

async def get_ad_accounts(session: aiohttp.ClientSession):
    """Получает список рекламных аккаунтов."""
    url = f"https://graph.facebook.com/{API_VERSION}/me/adaccounts"
    params = {"fields": "name,account_id"}
    data = await fb_get(session, url, params)
    return data.get("data", [])

# ==========================================================
# === API и Хелперы (Для Отчета "ВЧЕРА" - Daily Report) ===
# ==========================================================

async def get_campaign_objectives_daily(session: aiohttp.ClientSession, account_id: str):
    """Получает словарь {id_кампании: цель_кампании} для ВСЕХ кампаний."""
    url = f"https://graph.facebook.com/{API_VERSION}/act_{account_id}/campaigns"
    params = {"fields": "id,objective", "limit": 1000}
    data = await fb_get(session, url, params=params)
    return {campaign['id']: campaign.get('objective', 'N/A') for campaign in data.get("data", [])}

async def get_ad_level_insights_for_yesterday(session: aiohttp.ClientSession, account_id: str):
    """Получает детализированную статистику на уровне объявлений за вчерашний день."""
    url = f"https://graph.facebook.com/{API_VERSION}/act_{account_id}/insights"
    params = {
        "fields": "campaign_id,campaign_name,adset_id,adset_name,ad_id,ad_name,spend,actions,ctr,creative{thumbnail_url}",
        "level": "ad",
        "date_preset": "yesterday",
        "limit": 2000
    }
    data = await fb_get(session, url, params=params)
    return data.get("data", [])

def structure_insights_daily(insights: list, objectives: dict):
    """Структурирует плоский список инсайтов в иерархию Кампания -> Группа -> Объявления."""
    campaigns = {}
    for ad in insights:
        spend = float(ad.get("spend", 0))
        if spend == 0:
            continue
        camp_id = ad['campaign_id']
        adset_id = ad['adset_id']
        if camp_id not in objectives:
            continue
        if camp_id not in campaigns:
            campaigns[camp_id] = {"name": ad['campaign_name'], "objective": objectives.get(camp_id, 'N/A'), "adsets": {}}
        if adset_id not in campaigns[camp_id]['adsets']:
            campaigns[camp_id]['adsets'][adset_id] = {"name": ad['adset_name'], "ads": []}
        
        leads = sum(int(a["value"]) for a in ad.get("actions", []) if a.get("action_type") == LEAD_ACTION_TYPE)
        clicks = sum(int(a["value"]) for a in ad.get("actions", []) if a.get("action_type") == LINK_CLICK_ACTION_TYPE)
        
        ad_data = {
            "name": ad['ad_name'], "spend": spend, "leads": leads, "clicks": clicks,
            "ctr": float(ad.get('ctr', 0)), "thumbnail_url": ad.get('creative', {}).get('thumbnail_url', '#')
        }
        campaigns[camp_id]['adsets'][adset_id]['ads'].append(ad_data)
    return campaigns

def analyze_adsets_daily(campaigns_data: dict):
    """Анализирует группы объявлений, считая их общую статистику и стоимость."""
    analyzed_adsets = []
    for camp_id, camp in campaigns_data.items():
        for adset_id, adset in camp['adsets'].items():
            total_spend = sum(ad['spend'] for ad in adset['ads'])
            total_leads = sum(ad['leads'] for ad in adset['ads'])
            total_clicks = sum(ad['clicks'] for ad in adset['ads'])
            cost, cost_type = float('inf'), 'CPL'
            
            if "TRAFFIC" in camp['objective'].upper():
                cost_type = 'CPC'
                if total_clicks > 0: cost = total_spend / total_clicks
            elif total_leads > 0:
                cost = total_spend / total_leads
            
            analyzed_adsets.append({
                "id": adset_id, "name": adset['name'], "campaign_name": camp['name'], "spend": total_spend,
                "cost": cost, "cost_type": cost_type, "ads": adset['ads']
            })
    return analyzed_adsets

def format_ad_list_daily(ads: list, cost_type: str):
    """Форматирует список объявлений для вывода в отчет."""
    lines = []
    # Сортируем объявления по расходу для наглядности
    for ad in sorted(ads, key=lambda x: x['spend'], reverse=True):
        cost_str = ""
        if cost_type == 'CPL':
            cost = (ad['spend'] / ad['leads']) if ad['leads'] > 0 else 0
            cost_str = f"CPL: ${cost:.2f}"
        elif cost_type == 'CPC':
            cost = (ad['spend'] / ad['clicks']) if ad['clicks'] > 0 else 0
            cost_str = f"CPC: ${cost:.2f}"
        
        lines.append(f'    <a href="{ad["thumbnail_url"]}">▫️</a> <b>{ad["name"]}</b> | {cost_str} | CTR: {ad["ctr"]:.2f}%')
    return lines

async def process_single_account_daily(session, acc):
    """Обрабатывает один рекламный аккаунт и возвращает готовую текстовую секцию отчета."""
    objectives = await get_campaign_objectives_daily(session, acc["account_id"])
    insights = await get_ad_level_insights_for_yesterday(session, acc["account_id"])
    if not insights: return None

    campaigns_data = structure_insights_daily(insights, objectives)
    if not campaigns_data: return None

    adsets = analyze_adsets_daily(campaigns_data)
    total_spend = sum(adset['spend'] for adset in adsets)
    if total_spend == 0: return None
    
    total_leads = sum(sum(ad['leads'] for ad in adset['ads']) for adset in adsets)
    total_clicks = sum(sum(ad['clicks'] for ad in adset['ads']) for adset in adsets)

    report_lines = ["─" * 20, f"<b>🏢 Кабинет: <u>{acc['name']}</u></b>", f"<code>Расход: ${total_spend:.2f}</code>"]
    
    cost_str = ""
    if total_leads > 0:
        cost_str += f"Лиды: {total_leads} | Ср. CPL: ${total_spend / total_leads:.2f}"
    if total_clicks > 0:
        if cost_str: cost_str += " | "
        cost_str += f"Клики: {total_clicks} | Ср. CPC: ${total_spend / total_clicks:.2f}"
    if cost_str: report_lines.append(f"<code>{cost_str}</code>")

    adsets_with_cost = sorted([a for a in adsets if a['cost'] != float('inf')], key=lambda x: x['cost'])
    if not adsets_with_cost: return "\n".join(report_lines)

    best_adset = adsets_with_cost[0]
    worst_adset = adsets_with_cost[-1] if len(adsets_with_cost) > 1 else None
    
    report_lines.extend(["\n" + f"<b>✅ Лучшая группа:</b> {best_adset['name']} ({best_adset['campaign_name']})",
                         f"  - Расход: ${best_adset['spend']:.2f} | {best_adset['cost_type']}: ${best_adset['cost']:.2f}",
                         *format_ad_list_daily(best_adset['ads'], best_adset['cost_type'])])
    
    if worst_adset and worst_adset['id'] != best_adset['id']:
        report_lines.extend(["\n" + f"<b>❌ Худшая группа:</b> {worst_adset['name']} ({worst_adset['campaign_name']})",
                             f"  - Расход: ${worst_adset['spend']:.2f} | {worst_adset['cost_type']}: ${worst_adset['cost']:.2f}",
                             *format_ad_list_daily(worst_adset['ads'], worst_adset['cost_type'])])
        
    return "\n".join(report_lines)

# ==========================================================
# === API и Хелперы (Для ОСТАЛЬНЫХ отчетов) ===
# ==========================================================

async def get_campaigns(session: aiohttp.ClientSession, account_id: str):
    """Получает список кампаний для аккаунта."""
    url = f"https://graph.facebook.com/{API_VERSION}/act_{account_id}/campaigns"
    params = {"fields": "id,name,status,objective", "limit": 500}
    data = await fb_get(session, url, params)
    return data.get("data", [])

async def get_all_adsets(session: aiohttp.ClientSession, account_id: str):
    """Получает все группы объявлений для аккаунта."""
    url = f"https://graph.facebook.com/{API_VERSION}/act_{account_id}/adsets"
    params = {"fields": "id,name,campaign_id,status", "limit": 500}
    data = await fb_get(session, url, params)
    return data.get("data", [])

async def get_all_ads_with_creatives(session: aiohttp.ClientSession, account_id: str, active_adset_ids: list):
    """Получает все активные объявления для указанных групп с их креативами."""
    url = f"https://graph.facebook.com/{API_VERSION}/act_{account_id}/ads"
    filtering = [
        {'field': 'adset.id', 'operator': 'IN', 'value': active_adset_ids},
        {'field': 'effective_status', 'operator': 'IN', 'value': ['ACTIVE']}
    ]
    params = {
        "fields": "id,name,adset_id,campaign_id,creative{thumbnail_url}",
        "filtering": json.dumps(filtering),
        "limit": 1000
    }
    data = await fb_get(session, url, params)
    return data.get("data", [])

async def get_ad_level_insights(session: aiohttp.ClientSession, account_id: str, ad_ids: list, date_preset: str):
    """Получает статистику для конкретных объявлений за выбранный период."""
    url = f"https://graph.facebook.com/{API_VERSION}/act_{account_id}/insights"
    params = {
        "fields": "ad_id,spend,actions,ctr",
        "level": "ad",
        "filtering": f'[{{"field":"ad.id","operator":"IN","value":{ad_ids}}}]',
        "date_preset": date_preset,
        "limit": 1000
    }
    data = await fb_get(session, url, params)
    return data.get("data", [])

# ============================
# ===       Помощники      ===
# ============================

def cpl_label(value: float, metric: str) -> str:
    """Возвращает текстовую метку для CPL или CPC."""
    if metric == "cpc":
        if value <= 0.1: return "🟢 Дешёвый"
        if value <= 0.3: return "🟡 Средний"
        return "🔴 Дорогой"
    # CPL
    if value <= 1: return "🟢 Дешёвый"
    if value <= 3: return "🟡 Средний"
    return "🔴 Дорогой"

async def send_and_store(message: Message | CallbackQuery, text: str, *, is_persistent: bool = False, **kwargs):
    """Отправляет сообщение и сохраняет его ID для последующей очистки."""
    msg_obj = message.message if isinstance(message, CallbackQuery) else message
    kwargs.setdefault('disable_web_page_preview', True)
    msg = await msg_obj.answer(text, **kwargs)
    chat_id = msg.chat.id
    if chat_id not in sent_messages_by_chat:
        sent_messages_by_chat[chat_id] = []
    sent_messages_by_chat[chat_id].append({"id": msg.message_id, "persistent": is_persistent})
    return msg

# ============================
# ===         Меню         ===
# ============================

async def set_bot_commands(bot: Bot):
    """Устанавливает команды в меню Telegram."""
    commands = [
        BotCommand(command="start", description="🚀 Запустить бота / Показать меню"),
        BotCommand(command="report", description="📊 Отчёт по активным кампаниям"),
        BotCommand(command="clear", description="🧹 Очистить временные сообщения"),
    ]
    await bot.set_my_commands(commands, BotCommandScopeDefault())

def inline_main_menu():
    """Создаёт инлайн-клавиатуру главного меню."""
    kb = InlineKeyboardBuilder()
    kb.button(text="📊 Отчёт: Активные кампании", callback_data="report_period_select")
    kb.button(text="🧹 Очистить временные сообщения", callback_data="clear_chat")
    kb.adjust(1)
    return kb.as_markup()

def inline_period_menu():
    """Создаёт инлайн-клавиатуру для выбора периода отчёта."""
    kb = InlineKeyboardBuilder()
    kb.button(text="Сегодня", callback_data="build_report:today")
    kb.button(text="Вчера", callback_data="build_report:yesterday")
    kb.button(text="За 7 дней", callback_data="build_report:last_7d")
    kb.button(text="За 30 дней", callback_data="build_report:last_30d")
    kb.button(text="С 1 июня 2025", callback_data="build_report:from_june_1")
    kb.button(text="🔙 Назад в меню", callback_data="show_menu")
    kb.adjust(2, 2, 1, 1)
    return kb.as_markup()

# ============================
# ===       Хендлеры       ===
# ============================

@router.message(Command("start", "restart"))
async def start_handler(msg: Message):
    """Обработчик команды /start, показывает главное меню."""
    await clear_all_messages(msg.chat.id) # Очищаем чат перед показом нового меню
    await send_and_store(msg, "👋 Привет! Я твой бот для управления рекламой.\n\nВыберите действие:", is_persistent=True, reply_markup=inline_main_menu())

@router.callback_query(F.data == "show_menu")
async def show_menu_handler(call: CallbackQuery):
    """Показывает главное меню, редактируя существующее сообщение."""
    await call.message.edit_text("👋 Привет! Я твой бот для управления рекламой.\n\nВыберите действие:", reply_markup=inline_main_menu())

@router.callback_query(F.data == "report_period_select")
async def report_period_select_handler(call: CallbackQuery):
    """Показывает меню выбора периода для отчёта."""
    await call.message.edit_text("🗓️ Выберите период для отчёта:", reply_markup=inline_period_menu())

async def clear_all_messages(chat_id: int):
    """Внутренняя функция для полной очистки сообщений."""
    if chat_id in sent_messages_by_chat and sent_messages_by_chat[chat_id]:
        messages_to_delete = [msg_info["id"] for msg_info in sent_messages_by_chat[chat_id]]
        sent_messages_by_chat[chat_id] = [] # Очищаем список
        
        for msg_id in messages_to_delete:
            try:
                await bot.delete_message(chat_id, msg_id)
            except TelegramBadRequest:
                pass # Игнорируем ошибки, если сообщение уже удалено

@router.message(Command("clear"))
@router.callback_query(F.data == "clear_chat")
async def clear_chat_handler(event: Message | CallbackQuery):
    """Удаляет все временные сообщения, оставяляя постоянные (меню)."""
    message = event.message if isinstance(event, CallbackQuery) else event
    chat_id = message.chat.id
    
    messages_to_keep = [msg for msg in sent_messages_by_chat.get(chat_id, []) if msg.get("persistent")]
    messages_to_delete = [msg["id"] for msg in sent_messages_by_chat.get(chat_id, []) if not msg.get("persistent")]
    
    count = 0
    for msg_id in messages_to_delete:
        try:
            await bot.delete_message(chat_id, msg_id)
            count += 1
        except TelegramBadRequest:
            pass 
            
    sent_messages_by_chat[chat_id] = messages_to_keep # Сохраняем только постоянные

    if isinstance(event, CallbackQuery):
        await event.answer(f"✅ Готово! Удалил {count} временных сообщений.", show_alert=True)
    else:
        status_msg = await message.answer(f"✅ Готово! Удалил {count} временных сообщений.")
        await asyncio.sleep(3)
        await bot.delete_message(chat_id, status_msg.message_id)

# ============ Основной обработчик отчетов ============
@router.callback_query(F.data.startswith("build_report:"))
async def build_report_handler(call: CallbackQuery):
    """
    Основной хендлер для построения отчёта.
    Использует новую логику для date_preset='yesterday' и старую для остальных.
    """
    date_preset = call.data.split(":")[1]
    
    await call.message.edit_text(f"⏳ Начинаю сбор данных за период: <b>{date_preset}</b>...")
    status_msg = await send_and_store(call, "Подключаюсь к API...")

    # =================================================================
    # === НОВЫЙ БЛОК: Обработка отчета "ВЧЕРА" (Daily Report)        ===
    # =================================================================
    if date_preset == "yesterday":
        try:
            report_date_str = (datetime.now() - timedelta(days=1)).strftime('%d %B %Y')
            final_report_lines = [f"<b>📈 Дневная сводка за {report_date_str}</b>"]
            
            timeout = aiohttp.ClientTimeout(total=240)
            async with aiohttp.ClientSession(timeout=timeout) as session:
                accounts = await get_ad_accounts(session)
                if not accounts:
                    await status_msg.edit_text("❌ Нет доступных рекламных аккаунтов.")
                    return

                await status_msg.edit_text(f"📥 Собираю данные по {len(accounts)} кабинетам...")
                tasks = [process_single_account_daily(session, acc) for acc in accounts]
                account_results = await asyncio.gather(*tasks, return_exceptions=True)
            
            active_reports = 0
            for result in account_results:
                if isinstance(result, str) and result:
                    final_report_lines.append(result)
                    active_reports += 1
                elif isinstance(result, Exception):
                    print(f"Error processing account: {result}") # Логируем ошибку
            
            await bot.delete_message(status_msg.chat.id, status_msg.message_id)

            if active_reports == 0:
                final_report = "✅ За вчерашний день не было активности ни в одном из кабинетов."
            else:
                final_report = "\n".join(final_report_lines)

            # Отправляем отчет
            if len(final_report) > 4096:
                for x in range(0, len(final_report), 4096):
                    await send_and_store(call, final_report[x:x+4096])
            else:
                await send_and_store(call, final_report)

            # Возвращаем главное меню
            await call.message.edit_text("✅ Отчёт завершён. Выберите следующее действие:", reply_markup=inline_main_menu())

        except Exception as e:
            await status_msg.edit_text(f"❌ <b>Произошла ошибка при создании дневного отчёта:</b>\n{type(e).__name__}: {e}")
        return # Завершаем выполнение хендлера здесь

    # =================================================================
    # === СТАРЫЙ БЛОК: Обработка всех остальных отчетов             ===
    # =================================================================
    
    # Для кастомной даты "С 1 июня 2025"
    if date_preset == "from_june_1":
        start_date = "2025-06-01"
        end_date = datetime.now().strftime('%Y-%m-%d')
        time_range = f'{{"since":"{start_date}","until":"{end_date}"}}'
    else:
        time_range = None

    all_accounts_data = {}
    timeout = aiohttp.ClientTimeout(total=180) 

    try:
        async with aiohttp.ClientSession(timeout=timeout) as session:
            accounts = await get_ad_accounts(session)
            if not accounts:
                await status_msg.edit_text("❌ Нет доступных рекламных аккаунтов.")
                return

            total = len(accounts)
            for idx, acc in enumerate(accounts, start=1):
                base_text = f"📦({idx}/{total}) Кабинет: <b>{acc['name']}</b>\n"
                
                try:
                    await status_msg.edit_text(base_text + " Cкачиваю кампании и группы...")
                    campaigns = await get_campaigns(session, acc["account_id"])
                    campaigns_map = {c['id']: c for c in campaigns}
                    
                    adsets = await get_all_adsets(session, acc["account_id"])
                    active_adsets = [a for a in adsets if a.get("status") == "ACTIVE"]
                    if not active_adsets: continue
                    
                    adsets_map = {a['id']: a for a in active_adsets}
                    active_adset_ids = list(adsets_map.keys())

                    await status_msg.edit_text(base_text + " Cкачиваю объявления...")
                    ads = await get_all_ads_with_creatives(session, acc["account_id"], active_adset_ids)
                    if not ads: continue
                    
                    ad_ids = [ad['id'] for ad in ads]
                    await status_msg.edit_text(base_text + f" Cкачиваю статистику для {len(ad_ids)} объявлений...")
                    
                    insights = []
                    if time_range:
                        url = f"https://graph.facebook.com/{API_VERSION}/act_{acc['account_id']}/insights"
                        params = {
                            "fields": "ad_id,spend,actions,ctr", "level": "ad",
                            "filtering": f'[{{"field":"ad.id","operator":"IN","value":{ad_ids}}}]',
                            "time_range": time_range, "limit": 1000
                        }
                        insights_data = await fb_get(session, url, params)
                        insights = insights_data.get("data", [])
                    else:
                        insights = await get_ad_level_insights(session, acc["account_id"], ad_ids, date_preset)

                    insights_map = {}
                    for row in insights:
                        ad_id = row['ad_id']
                        spend = float(row.get("spend", 0))
                        leads = sum(int(a["value"]) for a in row.get("actions", []) if a.get("action_type") == LEAD_ACTION_TYPE)
                        clicks = sum(int(a["value"]) for a in row.get("actions", []) if a.get("action_type") == LINK_CLICK_ACTION_TYPE)
                        ctr = float(row.get("ctr", 0))
                        insights_map[ad_id] = {"spend": spend, "leads": leads, "clicks": clicks, "ctr": ctr}

                    account_data = {}
                    for ad in ads:
                        ad_id = ad['id']
                        adset_id = ad['adset_id']
                        campaign_id = ad.get('campaign_id')

                        if adset_id not in adsets_map or campaign_id not in campaigns_map:
                            continue

                        stats = insights_map.get(ad_id)
                        if not stats or stats['spend'] == 0:
                            continue
                        
                        campaign_obj = campaigns_map[campaign_id]
                        objective = campaign_obj.get("objective", "N/A")

                        if campaign_id not in account_data:
                            objective_clean = objective.replace('OUTCOME_', '').replace('_', ' ').capitalize()
                            account_data[campaign_id] = {
                                "name": campaign_obj['name'],
                                "objective": objective_clean,
                                "adsets": {}
                            }
                        
                        if adset_id not in account_data[campaign_id]['adsets']:
                            adset_obj = adsets_map[adset_id]
                            account_data[campaign_id]['adsets'][adset_id] = {
                                "name": adset_obj['name'],
                                "ads": []
                            }
                        
                        ad_info = {
                            "name": ad['name'],
                            "thumbnail_url": ad.get('creative', {}).get('thumbnail_url'),
                            "spend": stats['spend'],
                            "ctr": stats['ctr'],
                            "objective": objective
                        }

                        if "TRAFFIC" in ad_info["objective"]:
                            ad_info["clicks"] = stats["clicks"]
                            ad_info["cpc"] = (stats['spend'] / stats['clicks']) if stats['clicks'] > 0 else 0
                        else:
                            ad_info["leads"] = stats["leads"]
                            ad_info["cpl"] = (stats['spend'] / stats['leads']) if stats['leads'] > 0 else 0

                        account_data[campaign_id]['adsets'][adset_id]['ads'].append(ad_info)

                    if account_data:
                        all_accounts_data[acc['name']] = account_data

                except asyncio.TimeoutError:
                    await send_and_store(call, f"⚠️ <b>Превышен таймаут</b> при обработке кабинета <b>{acc['name']}</b>. Пропускаю его.")
                    continue
    
    except aiohttp.ClientResponseError as e:
        error_details = await e.text()
        await status_msg.edit_text(f"❌ <b>Ошибка API Facebook:</b>\nКод: {e.status}\nСообщение: {error_details}")
        return
    except Exception as e:
        await status_msg.edit_text(f"❌ <b>Произошла неизвестная ошибка:</b>\n{type(e).__name__}: {e}")
        return
        
    if not all_accounts_data:
        await status_msg.edit_text("✅ Активных кампаний с затратами за выбранный период не найдено.")
        await asyncio.sleep(5)
        await show_menu_handler(call) # Возврат в меню
        return
    
    try: await bot.delete_message(status_msg.chat.id, status_msg.message_id)
    except TelegramBadRequest: pass

    # Форматирование и отправка отчёта
    for acc_name, campaigns_data in all_accounts_data.items():
        active_campaign_count = len(campaigns_data)
        msg_lines = [
            f"<b>🏢 Рекламный кабинет:</b> <u>{acc_name}</u>",
            f"<b>📈 Активных кампаний:</b> {active_campaign_count}",
            "─" * 20
        ]
        
        for camp_id, camp_data in campaigns_data.items():
            msg_lines.append(f"\n<b>🎯 Кампания:</b> {camp_data['name']}")
            
            for adset_id, adset_data in camp_data['adsets'].items():
                total_spend = sum(ad['spend'] for ad in adset_data['ads'])
                
                adset_block = [f"  <b>↳ Группа:</b> <code>{adset_data['name']}</code>"]
                
                # Общая статистика для группы
                if "TRAFFIC" in adset_data['ads'][0]["objective"]:
                    total_clicks = sum(ad['clicks'] for ad in adset_data['ads'])
                    total_cpc = (total_spend / total_clicks) if total_clicks > 0 else 0
                    adset_block.append(f"    - <b>Цель:</b> {camp_data['objective']}")
                    adset_block.append(f"    - <b>Клики:</b> {total_clicks}")
                    adset_block.append(f"    - <b>Расход:</b> ${total_spend:.2f}")
                    adset_block.append(f"    - <b>CPC:</b> ${total_cpc:.2f} {cpl_label(total_cpc, 'cpc')}")
                else:
                    total_leads = sum(ad['leads'] for ad in adset_data['ads'])
                    total_cpl = (total_spend / total_leads) if total_leads > 0 else 0
                    adset_block.append(f"    - <b>Цель:</b> {camp_data['objective']}")
                    adset_block.append(f"    - <b>Лиды:</b> {total_leads}")
                    adset_block.append(f"    - <b>Расход:</b> ${total_spend:.2f}")
                    adset_block.append(f"    - <b>CPL:</b> ${total_cpl:.2f} {cpl_label(total_cpl, 'cpl')}")

                msg_lines.extend(adset_block)
                
                if adset_data['ads']:
                    msg_lines.append("  <b>↳ Объявления:</b>")
                    
                    sort_key = 'cpc' if "TRAFFIC" in adset_data['ads'][0]["objective"] else 'cpl'
                    sorted_ads = sorted(adset_data['ads'], key=lambda x: x.get(sort_key, float('inf')))

                    for ad in sorted_ads:
                        thumb_url = ad.get('thumbnail_url', '#')
                        if "TRAFFIC" in ad["objective"]:
                            ad_line = f'    <a href="{thumb_url}">🖼️</a> <b>{ad["name"]}</b> | CPC: ${ad["cpc"]:.2f} | CTR: {ad["ctr"]:.2f}%'
                        else:
                            ad_line = f'    <a href="{thumb_url}">🖼️</a> <b>{ad["name"]}</b> | CPL: ${ad["cpl"]:.2f} | CTR: {ad["ctr"]:.2f}%'
                        msg_lines.append(ad_line)

        final_report = "\n".join(msg_lines)
        if len(final_report) > 4096:
            for x in range(0, len(final_report), 4096):
                await send_and_store(call, final_report[x:x+4096])
        else:
            await send_and_store(call, final_report)

    # Возвращаем главное меню в исходное сообщение
    await call.message.edit_text("✅ Отчёт завершён. Выберите следующее действие:", reply_markup=inline_main_menu())


# ============================
# ===         Запуск       ===
# ============================

async def main():
    """Основная функция для запуска бота."""
    dp.include_router(router)
    await set_bot_commands(bot)
    await bot.delete_webhook(drop_pending_updates=True)
    await dp.start_polling(bot)

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except (KeyboardInterrupt, SystemExit):
        print("Бот остановлен вручную.")
