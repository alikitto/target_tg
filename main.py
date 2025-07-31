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

async def get_ad_accounts(session: aiohttp.ClientSession):
    url = f"https://graph.facebook.com/{API_VERSION}/me/adaccounts"
    params = {"fields": "name,account_id"}
    data = await fb_get(session, url, params)
    return data.get("data", [])

async def get_campaigns(session: aiohttp.ClientSession, account_id: str):
    url = f"https://graph.facebook.com/{API_VERSION}/act_{account_id}/campaigns"
    params = {"fields": "id,name,objective", "limit": 500}
    data = await fb_get(session, url, params)
    return data.get("data", [])

# ### ИЗМЕНЕНИЕ: Единственная функция для получения статистики, как в Apps Script
async def get_adset_level_insights(session: aiohttp.ClientSession, account_id: str, start_date: str, end_date: str):
    """Получает статистику на уровне ГРУПП ОБЪЯВЛЕНИЙ для всех работающих групп."""
    url = f"https://graph.facebook.com/{API_VERSION}/act_{account_id}/insights"
    # Фильтр по реально работающим группам, у которых были показы
    filtering = f'[{{"field":"impressions","operator":"GREATER_THAN","value":0}}]'
    params = {
        "fields": "campaign_id,adset_id,adset_name,spend,actions,link_clicks,ctr",
        "level": "adset",
        "filtering": filtering,
        "time_range": f'{{"since":"{start_date}","until":"{end_date}"}}',
        "limit": 500
    }
    data = await fb_get(session, url, params)
    return data.get("data", [])

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
            print(f"Error updating panel: {e}")

# ============================
# ===         Меню         ===
# ============================
async def set_bot_commands(bot: Bot):
    commands = [BotCommand(command="start", description="🚀 Показать пульт управления"), BotCommand(command="report", description="📊 Запросить отчёт"), BotCommand(command="clear", description="🧹 Очистить чат")]
    await bot.set_my_commands(commands, BotCommandScopeDefault())

def inline_main_menu():
    kb = InlineKeyboardBuilder()
    kb.button(text="📊 Отчёт: Активные кампании", callback_data="select_report_period")
    kb.button(text="🧹 Очистить отчёты", callback_data="clear_chat")
    return kb.as_markup()

def inline_period_menu():
    kb = InlineKeyboardBuilder()
    kb.button(text="Сегодня", callback_data="report_period:today")
    kb.button(text="Вчера", callback_data="report_period:yesterday")
    kb.button(text="7 дней", callback_data="report_period:week")
    kb.button(text="30 дней", callback_data="report_period:month")
    kb.button(text="С 1 июня 2025", callback_data="report_period:all_time")
    kb.button(text="⬅️ Назад", callback_data="show_menu")
    kb.adjust(2, 2, 2)
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

@router.callback_query(F.data == "clear_chat")
async def clear_chat_handler(call: CallbackQuery):
    chat_id = call.message.chat.id
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
    await call.answer(f"✅ Готово! Удалил {count} отчётов.", show_alert=True)
    await start_handler(call.message)

@router.callback_query(F.data == "select_report_period")
async def select_period_handler(call: CallbackQuery):
    await update_panel(call.message.chat.id, "🗓️ Выберите период для отчета:", reply_markup=inline_period_menu())
    await call.answer()

@router.callback_query(F.data.startswith("report_period:"))
async def build_report(call: CallbackQuery):
    chat_id = call.message.chat.id
    period = call.data.split(":")[1]
    
    today = datetime.now()
    if period == 'today':
        start_date = end_date = today.strftime("%Y-%m-%d")
    elif period == 'yesterday':
        start_date = end_date = (today - timedelta(days=1)).strftime("%Y-%m-%d")
    elif period == 'week':
        start_date = (today - timedelta(days=7)).strftime("%Y-%m-%d")
        end_date = today.strftime("%Y-%m-%d")
    elif period == 'month':
        start_date = (today - timedelta(days=30)).strftime("%Y-%m-%d")
        end_date = today.strftime("%Y-%m-%d")
    else: # all_time
        start_date = "2025-06-01"
        end_date = today.strftime("%Y-%m-%d")

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
                    await update_panel(chat_id, base_text + " Поиск и анализ кампаний...")
                    campaigns = await get_campaigns(session, acc["account_id"])
                    campaigns_map = {c['id']: c for c in campaigns}
                    
                    insights = await get_adset_level_insights(session, acc["account_id"], start_date, end_date)
                    if not insights:
                        continue
                    
                    account_data = {}
                    for adset_insight in insights:
                        campaign_id = adset_insight.get('campaign_id')
                        if campaign_id not in campaigns_map: continue
                        
                        if campaign_id not in account_data:
                            campaign_obj = campaigns_map[campaign_id]
                            account_data[campaign_id] = {"name": campaign_obj['name'], "objective_raw": campaign_obj.get("objective", "N/A"), "adsets": []}
                        
                        account_data[campaign_id]['adsets'].append(adset_insight)

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
            
            for adset in sorted(camp_data['adsets'], key=lambda x: float(x.get('spend', 0)), reverse=True):
                spend = float(adset.get('spend', 0))
                if is_traffic:
                    metric_val = int(adset.get('link_clicks', 0))
                    cost_per_action = (spend / metric_val) if metric_val > 0 else 0
                    metric_name, cost_name = "Клики", "CPC"
                else:
                    metric_val = sum(int(a["value"]) for a in adset.get("actions", []) if a.get("action_type") == LEAD_ACTION_TYPE)
                    cost_per_action = (spend / metric_val) if metric_val > 0 else 0
                    metric_name, cost_name = "Лиды", "CPL"

                msg_lines.extend([
                    f"  <b>↳ Группа:</b> <code>{adset['adset_name']}</code>",
                    f"    - <b>Цель:</b> {objective_clean}",
                    f"    - <b>{metric_name}:</b> {metric_val}",
                    f"    - <b>Расход:</b> ${spend:.2f}",
                    f"    - <b>{cost_name}:</b> ${cost_per_action:.2f} {metric_label(cost_per_action)}"
                ])
        
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
