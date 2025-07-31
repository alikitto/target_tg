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

# --- Инициализация ---
bot = Bot(token=TELEGRAM_TOKEN, parse_mode="HTML")
dp = Dispatcher()
router = Router()

chat_sessions = {}

# ============================
# ===         API          ===
# ============================
async def fb_get(session: aiohttp.ClientSession, url: str, params: dict = None):
    params = params or {}
    params["access_token"] = META_TOKEN
    async with session.get(url, params=params) as response:
        response.raise_for_status()
        return await response.json()

async def fb_post(session: aiohttp.ClientSession, url: str, params: dict = None):
    params = params or {}
    params["access_token"] = META_TOKEN
    async with session.post(url, params=params) as response:
        response.raise_for_status()
        return await response.json()

async def get_ad_accounts(session: aiohttp.ClientSession):
    url = f"https://graph.facebook.com/{API_VERSION}/me/adaccounts"
    params = {"fields": "name,account_id"}
    data = await fb_get(session, url, params)
    return data.get("data", [])

async def get_campaigns(session: aiohttp.ClientSession, account_id: str):
    url = f"https://graph.facebook.com/{API_VERSION}/act_{account_id}/campaigns"
    params = {"fields": "id,name,status,objective", "limit": 500}
    data = await fb_get(session, url, params)
    return data.get("data", [])

async def get_all_adsets(session: aiohttp.ClientSession, account_id: str):
    url = f"https://graph.facebook.com/{API_VERSION}/act_{account_id}/adsets"
    params = {"fields": "id,name,campaign_id,status", "limit": 500}
    data = await fb_get(session, url, params)
    return data.get("data", [])

async def get_all_ads_with_creatives(session: aiohttp.ClientSession, account_id: str, active_adset_ids: list):
    url = f"https://graph.facebook.com/{API_VERSION}/act_{account_id}/ads"
    filtering = [{'field': 'adset.id', 'operator': 'IN', 'value': active_adset_ids}, {'field': 'effective_status', 'operator': 'IN', 'value': ['ACTIVE']}]
    params = {"fields": "id,name,adset_id,campaign_id,creative{thumbnail_url}", "filtering": json.dumps(filtering), "limit": 1000}
    data = await fb_get(session, url, params)
    return data.get("data", [])

async def start_async_insights_job(session: aiohttp.ClientSession, account_id: str, ad_ids: list, start_date: str):
    """Отправляет запрос на создание отчета в фоновом режиме."""
    end_date = datetime.now().strftime("%Y-%m-%d")
    ad_ids_json_string = json.dumps(ad_ids)
    url = f"https://graph.facebook.com/{API_VERSION}/act_{account_id}/insights"
    params = {
        "fields": json.dumps(["ad_id", "spend", "actions", "ctr", "link_clicks"]),
        "level": "ad",
        "filtering": f'[{{"field":"ad.id","operator":"IN","value":{ad_ids_json_string}}}]',
        "time_range": f'{{"since":"{start_date}","until":"{end_date}"}}',
    }
    response = await fb_post(session, url, params=params)
    return response.get('report_run_id')

async def check_async_job_status(session: aiohttp.ClientSession, report_run_id: str):
    """Проверяет статус готовности отчета."""
    url = f"https://graph.facebook.com/{API_VERSION}/{report_run_id}"
    params = {"fields": "async_status,async_percent_completion"}
    return await fb_get(session, url, params=params)

async def get_async_job_results(session: aiohttp.ClientSession, report_run_id: str):
    """Получает результаты завершенного асинхронного отчета."""
    url = f"https://graph.facebook.com/{API_VERSION}/{report_run_id}/insights"
    params = {"limit": 1000}
    all_results = []
    response = await fb_get(session, url, params=params)
    all_results.extend(response.get("data", []))
    next_page_url = response.get("paging", {}).get("next")
    while next_page_url:
        async with session.get(next_page_url) as next_response:
            next_response.raise_for_status()
            paged_data = await next_response.json()
            all_results.extend(paged_data.get("data", []))
            next_page_url = paged_data.get("paging", {}).get("next")
    return all_results

# ============================
# ===      Помощники       ===
# ============================
def get_session(chat_id: int):
    if chat_id not in chat_sessions:
        chat_sessions[chat_id] = {"messages": [], "panel_id": None}
    return chat_sessions[chat_id]

def metric_label(value: float) -> str:
    if value <= 1: return "🟢 Дешёвый"
    if value <= 3: return "🟡 Средний"
    return "🔴 Дорогой"

async def store_message_id(chat_id: int, message_id: int):
    session = get_session(chat_id)
    session["messages"].append(message_id)

async def update_panel(chat_id: int, text: str, **kwargs):
    session = get_session(chat_id)
    panel_id = session.get("panel_id")
    try:
        if panel_id:
            await bot.edit_message_text(text, chat_id, panel_id, **kwargs)
        else:
            msg = await bot.send_message(chat_id, text, **kwargs)
            session["panel_id"] = msg.message_id
    except TelegramBadRequest as e:
        if "message to edit not found" in e.message or "message is not modified" in e.message:
            msg = await bot.send_message(chat_id, text, **kwargs)
            session["panel_id"] = msg.message_id
        else:
            raise e

# ============================
# ===         Меню         ===
# ============================
async def set_bot_commands(bot: Bot):
    commands = [
        BotCommand(command="start", description="🚀 Показать пульт управления"),
        BotCommand(command="report", description="📊 Запросить отчёт"),
        BotCommand(command="clear", description="🧹 Очистить временные сообщения"),
    ]
    await bot.set_my_commands(commands, BotCommandScopeDefault())

def inline_main_menu():
    kb = InlineKeyboardBuilder()
    kb.button(text="📊 Отчёт: Активные кампании", callback_data="select_report_period")
    kb.button(text="🧹 Очистить временные сообщения", callback_data="clear_chat")
    return kb.as_markup()

# ### ИЗМЕНЕНИЕ: Новое меню для выбора периода отчета
def inline_period_menu():
    kb = InlineKeyboardBuilder()
    kb.button(text="За сегодня", callback_data="report_period:today")
    kb.button(text="За 7 дней", callback_data="report_period:week")
    kb.button(text="За 30 дней", callback_data="report_period:month")
    kb.button(text="С 1 июня 2025", callback_data="report_period:all_time")
    kb.button(text="⬅️ Назад", callback_data="show_menu")
    kb.adjust(2, 2, 1) # Красиво располагаем кнопки
    return kb.as_markup()

# ============================
# ===       Хендлеры       ===
# ============================
@router.message(Command("start", "restart"))
async def start_handler(msg: Message):
    text = "👋 Привет! Я твой бот для управления рекламой.\n\nВыберите действие:"
    await update_panel(msg.chat.id, text, reply_markup=inline_main_menu())

@router.callback_query(F.data == "show_menu")
async def show_menu_handler(call: CallbackQuery):
    text = "👋 Привет! Я твой бот для управления рекламой.\n\nВыберите действие:"
    await update_panel(call.message.chat.id, text, reply_markup=inline_main_menu())
    await call.answer()

@router.message(Command("clear"))
@router.callback_query(F.data == "clear_chat")
async def clear_chat_handler(event: Message | CallbackQuery):
    chat_id = event.message.chat.id
    session = get_session(chat_id)
    messages_to_delete = session["messages"].copy()
    session["messages"] = []
    count = 0
    for msg_id in messages_to_delete:
        try:
            await bot.delete_message(chat_id, msg_id)
            count += 1
        except TelegramBadRequest:
            pass
    confirmation_text = f"✅ Готово! Удалил {count} временных сообщений."
    menu_text = "👋 Привет! Я твой бот для управления рекламой.\n\nВыберите действие:"
    await update_panel(chat_id, menu_text, reply_markup=inline_main_menu())
    if isinstance(event, CallbackQuery):
        await event.answer(confirmation_text, show_alert=True)
    else:
        msg = await event.answer(confirmation_text)
        await asyncio.sleep(5)
        try:
            await bot.delete_message(chat_id, msg.message_id)
        except TelegramBadRequest:
            pass

# ### ИЗМЕНЕНИЕ: Хендлер для показа меню выбора периода
@router.callback_query(F.data == "select_report_period")
async def select_period_handler(call: CallbackQuery):
    await update_panel(call.message.chat.id, "🗓️ Выберите период для отчета:", reply_markup=inline_period_menu())
    await call.answer()

# ### ИЗМЕНЕНИЕ: Основной хендлер теперь запускается после выбора периода
@router.callback_query(F.data.startswith("report_period:"))
async def build_report(call: CallbackQuery):
    chat_id = call.message.chat.id
    period = call.data.split(":")[1]

    # Определяем дату начала в зависимости от выбора
    today = datetime.now()
    if period == 'today':
        start_date = today.strftime("%Y-%m-%d")
    elif period == 'week':
        start_date = (today - timedelta(days=7)).strftime("%Y-%m-%d")
    elif period == 'month':
        start_date = (today - timedelta(days=30)).strftime("%Y-%m-%d")
    else: # all_time
        start_date = "2025-06-01"

    await update_panel(chat_id, "⏳ Начинаю сбор данных...")
    all_accounts_data = {}
    
    timeout = aiohttp.ClientTimeout(total=300)
    
    try:
        async with aiohttp.ClientSession(timeout=timeout) as session:
            accounts = await get_ad_accounts(session)
            if not accounts:
                await update_panel(chat_id, "❌ Нет доступных рекламных аккаунтов.", reply_markup=inline_main_menu())
                return

            total = len(accounts)
            for idx, acc in enumerate(accounts, start=1):
                base_text = f"📦({idx}/{total}) Кабинет: <b>{acc['name']}</b>\n"
                
                try:
                    await update_panel(chat_id, base_text + " Cкачиваю кампании и группы...")
                    campaigns = await get_campaigns(session, acc["account_id"])
                    campaigns_map = {c['id']: c for c in campaigns}
                    
                    adsets = await get_all_adsets(session, acc["account_id"])
                    active_adsets = [a for a in adsets if a.get("status") == "ACTIVE"]
                    if not active_adsets: continue
                    adsets_map = {a['id']: a for a in active_adsets}
                    active_adset_ids = list(adsets_map.keys())

                    await update_panel(chat_id, base_text + " Cкачиваю объявления...")
                    ads = await get_all_ads_with_creatives(session, acc["account_id"], active_adset_ids)
                    if not ads: continue
                    
                    ad_ids = [ad['id'] for ad in ads]
                    await update_panel(chat_id, base_text + f" Запускаю асинхронный отчет для {len(ad_ids)} объявлений...")
                    report_run_id = await start_async_insights_job(session, acc["account_id"], ad_ids, start_date)

                    if not report_run_id:
                        msg = await bot.send_message(chat_id, f"⚠️ Не удалось запустить отчет для кабинета <b>{acc['name']}</b>.")
                        await store_message_id(chat_id, msg.message_id)
                        continue

                    insights = []
                    while True:
                        status_data = await check_async_job_status(session, report_run_id)
                        status = status_data.get('async_status')
                        percent = status_data.get('async_percent_completion', 0)
                        await update_panel(chat_id, base_text + f" Отчет готовится: {percent}%...")
                        if status == 'Job Completed':
                            insights = await get_async_job_results(session, report_run_id)
                            break
                        elif status == 'Job Failed':
                            msg = await bot.send_message(chat_id, f"❌ Отчет для кабинета <b>{acc['name']}</b> не удался.")
                            await store_message_id(chat_id, msg.message_id)
                            break
                        await asyncio.sleep(15)
                    
                    if not insights: continue

                    insights_map = {}
                    for row in insights:
                        ad_id = row['ad_id']
                        insights_map[ad_id] = {
                            "spend": float(row.get("spend", 0)),
                            "leads": sum(int(a["value"]) for a in row.get("actions", []) if a.get("action_type") == LEAD_ACTION_TYPE),
                            "clicks": int(row.get("link_clicks", 0)),
                            "ctr": float(row.get("ctr", 0))
                        }

                    account_data = {}
                    for ad in ads:
                        ad_id, adset_id, campaign_id = ad['id'], ad['adset_id'], ad.get('campaign_id')
                        if adset_id not in adsets_map or campaign_id not in campaigns_map: continue
                        stats = insights_map.get(ad_id)
                        if not stats or (stats['spend'] == 0 and stats['leads'] == 0 and stats['clicks'] == 0): continue
                        
                        if campaign_id not in account_data:
                            campaign_obj = campaigns_map[campaign_id]
                            account_data[campaign_id] = {"name": campaign_obj['name'], "objective_raw": campaign_obj.get("objective", "N/A"), "adsets": {}}
                        
                        if adset_id not in account_data[campaign_id]['adsets']:
                            account_data[campaign_id]['adsets'][adset_id] = {"name": adsets_map[adset_id]['name'], "ads": []}
                        
                        ad_info = {"name": ad['name'], "thumbnail_url": ad.get('creative', {}).get('thumbnail_url'), **stats}
                        account_data[campaign_id]['adsets'][adset_id]['ads'].append(ad_info)

                    if account_data: all_accounts_data[acc['name']] = account_data
                except asyncio.TimeoutError:
                    msg = await bot.send_message(chat_id, f"⚠️ <b>Превышен таймаут</b> при обработке кабинета <b>{acc['name']}</b>. Пропускаю его.")
                    await store_message_id(chat_id, msg.message_id)
                    continue
    
    except aiohttp.ClientResponseError as e:
        error_details = "Не удалось получить детали ошибки"
        if e.content_type == 'application/json':
            try: error_details = (await e.json()).get("error", {}).get("message", "Нет сообщения")
            except: pass
        else: error_details = e.reason
        await update_panel(chat_id, f"❌ <b>Ошибка API Facebook:</b>\nКод: {e.status}\nСообщение: {error_details}", reply_markup=inline_main_menu())
        return
    except Exception as e:
        await update_panel(chat_id, f"❌ <b>Произошла неизвестная ошибка:</b>\n{type(e).__name__}: {e}", reply_markup=inline_main_menu())
        return
        
    if not all_accounts_data:
        await update_panel(chat_id, "✅ Активных кампаний с затратами или лидами не найдено.", reply_markup=inline_main_menu())
        return
    
    for acc_name, campaigns_data in all_accounts_data.items():
        active_campaign_count = len(campaigns_data)
        msg_lines = [f"<b>🏢 Рекламный кабинет:</b> <u>{acc_name}</u>", f"<b>📈 Активных кампаний:</b> {active_campaign_count}", "─" * 20]
        
        for camp_id, camp_data in campaigns_data.items():
            is_traffic = 'TRAFFIC' in camp_data['objective_raw']
            objective_clean = camp_data['objective_raw'].replace('OUTCOME_', '').replace('_', ' ').capitalize()
            msg_lines.append(f"\n<b>🎯 Кампания:</b> {camp_data['name']}")
            
            for adset_id, adset_data in camp_data['adsets'].items():
                total_spend = sum(ad['spend'] for ad in adset_data['ads'])
                if is_traffic:
                    total_metric_val = sum(ad['clicks'] for ad in adset_data['ads'])
                    total_cost_per_action = (total_spend / total_metric_val) if total_metric_val > 0 else 0
                    metric_name, cost_name = "Клики", "CPC"
                else:
                    total_metric_val = sum(ad['leads'] for ad in adset_data['ads'])
                    total_cost_per_action = (total_spend / total_metric_val) if total_metric_val > 0 else 0
                    metric_name, cost_name = "Лиды", "CPL"

                msg_lines.extend([
                    f"  <b>↳ Группа:</b> <code>{adset_data['name']}</code>",
                    f"    - <b>Цель:</b> {objective_clean}",
                    f"    - <b>{metric_name}:</b> {total_metric_val}",
                    f"    - <b>Расход:</b> ${total_spend:.2f}",
                    f"    - <b>{cost_name}:</b> ${total_cost_per_action:.2f} {metric_label(total_cost_per_action)}"
                ])
                
                if adset_data['ads']:
                    msg_lines.append("  <b>↳ Объявления:</b>")
                    for ad in sorted(adset_data['ads'], key=lambda x: x['spend'], reverse=True):
                        thumb_url = ad.get('thumbnail_url', '#')
                        if is_traffic:
                            ad_cost_per_action = (ad['spend'] / ad['clicks']) if ad['clicks'] > 0 else 0
                            ad_cost_name = "CPC"
                        else:
                            ad_cost_per_action = (ad['spend'] / ad['leads']) if ad['leads'] > 0 else 0
                            ad_cost_name = "CPL"
                        msg_lines.append(f'    <a href="{thumb_url}">🖼️</a> <b>{ad["name"]}</b> | {ad_cost_name}: ${ad_cost_per_action:.2f} | CTR: {ad["ctr"]:.2f}%')
        
        report_msg = await bot.send_message(chat_id, "\n".join(msg_lines), parse_mode="HTML", disable_web_page_preview=True)
        await store_message_id(chat_id, report_msg.message_id)

    await update_panel(chat_id, "✅ Отчёт завершён. Выберите следующее действие:", reply_markup=inline_main_menu())

# ============================
# ===         Запуск       ===
# ============================
async def main():
    dp.include_router(router)
    await set_bot_commands(bot)
    await bot.delete_webhook(drop_pending_updates=True)
    await dp.start_polling(bot)

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("Бот остановлен вручную.")
