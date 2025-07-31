import os
import asyncio
import aiohttp
import json 
from datetime import datetime, timedelta
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
# ===         API          ===
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
# ===      Помощники       ===
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
    await send_and_store(msg, "👋 Привет! Я твой бот для управления рекламой.\n\nВыберите действие:", is_persistent=True, reply_markup=inline_main_menu())

@router.callback_query(F.data == "show_menu")
async def show_menu_handler(call: CallbackQuery):
    """Показывает главное меню, редактируя существующее сообщение."""
    await call.message.edit_text("👋 Привет! Я твой бот для управления рекламой.\n\nВыберите действие:", reply_markup=inline_main_menu())

@router.callback_query(F.data == "report_period_select")
async def report_period_select_handler(call: CallbackQuery):
    """Показывает меню выбора периода для отчёта."""
    await call.message.edit_text("🗓️ Выберите период для отчёта:", reply_markup=inline_period_menu())


@router.message(Command("clear"))
@router.callback_query(F.data == "clear_chat")
async def clear_chat_handler(event: Message | CallbackQuery):
    """Удаляет все временные сообщения, оставяляя постоянные (меню)."""
    message = event.message if isinstance(event, CallbackQuery) else event
    chat_id = message.chat.id
    
    if chat_id in sent_messages_by_chat and sent_messages_by_chat[chat_id]:
        messages_to_delete = [msg_info["id"] for msg_info in sent_messages_by_chat[chat_id] if not msg_info.get("persistent")]
        # Оставляем только постоянные сообщения
        sent_messages_by_chat[chat_id] = [msg_info for msg_info in sent_messages_by_chat[chat_id] if msg_info.get("persistent")]
        
        count = 0
        for msg_id in messages_to_delete:
            try:
                await bot.delete_message(chat_id, msg_id)
                count += 1
            except TelegramBadRequest:
                pass # Игнорируем ошибки, если сообщение уже удалено
        
        status_msg = await message.answer(f"✅ Готово! Удалил {count} временных сообщений.")
        await asyncio.sleep(3)
        await bot.delete_message(chat_id, status_msg.message_id)
    else:
        status_msg = await message.answer("ℹ️ Временных сообщений для удаления нет.")
        await asyncio.sleep(3)
        await bot.delete_message(chat_id, status_msg.message_id)

    if isinstance(event, CallbackQuery):
        # Если это было нажатие кнопки, просто скрываем её
        await event.answer()


# ============ Отчёт с лоадером ============
@router.callback_query(F.data.startswith("build_report:"))
async def build_report_handler(call: CallbackQuery):
    """Основной хендлер для построения отчёта."""
    date_preset = call.data.split(":")[1]
    
    # Для кастомной даты "С 1 июня 2025"
    if date_preset == "from_june_1":
        start_date = "2025-06-01"
        end_date = datetime.now().strftime('%Y-%m-%d')
        time_range = f'{{"since":"{start_date}","until":"{end_date}"}}'
    else:
        time_range = None # Используем стандартные date_preset

    await call.message.edit_text(f"⏳ Начинаю сбор данных за период: <b>{date_preset}</b>...")
    status_msg = await send_and_store(call, "Подключаюсь к API...")

    all_accounts_data = {}
    timeout = aiohttp.ClientTimeout(total=180) # Увеличим общий таймаут

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
                    
                    # Передаем правильный параметр в функцию
                    insights_params = {"account_id": acc["account_id"], "ad_ids": ad_ids}
                    if time_range:
                        insights_params["date_preset"] = None # Не используем preset если есть time_range
                        # Добавляем time_range в сам запрос внутри функции
                        url = f"https://graph.facebook.com/{API_VERSION}/act_{acc['account_id']}/insights"
                        params = {
                            "fields": "ad_id,spend,actions,ctr", "level": "ad",
                            "filtering": f'[{{"field":"ad.id","operator":"IN","value":{ad_ids}}}]',
                            "time_range": time_range, "limit": 1000
                        }
                        insights_data = await fb_get(session, url, params)
                        insights = insights_data.get("data", [])
                    else:
                        insights_params["date_preset"] = date_preset
                        insights = await get_ad_level_insights(session, **insights_params)

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
        error_details = "Не удалось получить детали ошибки"
        if e.content_type == 'application/json':
            try: error_details = (await e.json()).get("error", {}).get("message", "Нет сообщения")
            except: pass
        else: error_details = e.reason
        await status_msg.edit_text(f"❌ <b>Ошибка API Facebook:</b>\nКод: {e.status}\nСообщение: {error_details}")
        return
    except Exception as e:
        await status_msg.edit_text(f"❌ <b>Произошла неизвестная ошибка:</b>\n{type(e).__name__}: {e}")
        return
        
    if not all_accounts_data:
        await status_msg.edit_text("✅ Активных кампаний с затратами за выбранный период не найдено.")
        await asyncio.sleep(5)
        await show_menu_handler(call)
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
                    
                    # Сортировка в зависимости от цели
                    sort_key = 'cpc' if "TRAFFIC" in adset_data['ads'][0]["objective"] else 'cpl'
                    sorted_ads = sorted(adset_data['ads'], key=lambda x: x.get(sort_key, float('inf')))

                    for ad in sorted_ads:
                        thumb_url = ad.get('thumbnail_url', '#')
                        if "TRAFFIC" in ad["objective"]:
                            ad_line = f'    <a href="{thumb_url}">🖼️</a> <b>{ad["name"]}</b> | CPC: ${ad["cpc"]:.2f} | CTR: {ad["ctr"]:.2f}%'
                        else:
                            ad_line = f'    <a href="{thumb_url}">🖼️</a> <b>{ad["name"]}</b> | CPL: ${ad["cpl"]:.2f} | CTR: {ad["ctr"]:.2f}%'
                        msg_lines.append(ad_line)

        # Отправляем одним большим сообщением для каждого аккаунта
        final_report = "\n".join(msg_lines)
        # Разбиваем на части, если сообщение слишком длинное
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
