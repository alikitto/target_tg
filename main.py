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

# --- Инициализация ---
bot = Bot(token=TELEGRAM_TOKEN, parse_mode="HTML")
dp = Dispatcher()
router = Router()

sent_messages_by_chat = {}

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
    params = {"fields": "id,name,status,objective", "limit": 500}
    data = await fb_get(session, url, params)
    return data.get("data", [])

async def get_all_adsets(session: aiohttp.ClientSession, account_id: str):
    url = f"https://graph.facebook.com/{API_VERSION}/act_{account_id}/adsets"
    params = {"fields": "id,name,campaign_id,status", "limit": 500}
    data = await fb_get(session, url, params)
    return data.get("data", [])

async def get_adset_insights(session: aiohttp.ClientSession, account_id: str, adset_ids: list):
    start_date = "2020-01-01"
    end_date = datetime.now().strftime("%Y-%m-%d")
    adset_ids_json_string = json.dumps(adset_ids)
    url = f"https://graph.facebook.com/{API_VERSION}/act_{account_id}/insights"
    params = {
        "fields": "adset_id,spend,actions",
        "level": "adset",
        "filtering": f'[{{"field":"adset.id","operator":"IN","value":{adset_ids_json_string}}}]',
        "time_range": f'{{"since":"{start_date}","until":"{end_date}"}}',
        "limit": 500
    }
    data = await fb_get(session, url, params)
    return data.get("data", [])


# ============================
# ===      Помощники       ===
# ============================
def cpl_label(cpl: float) -> str:
    if cpl <= 1: return "🟢 Дешёвый"
    if cpl <= 3: return "🟡 Средний"
    return "🔴 Дорогой"

async def send_and_store(message: Message | CallbackQuery, text: str, *, is_persistent: bool = False, **kwargs):
    msg_obj = message.message if isinstance(message, CallbackQuery) else message
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
    commands = [
        BotCommand(command="start", description="🚀 Запустить бота / Показать меню"),
        BotCommand(command="report", description="📊 Отчёт по активным кампаниям"),
        BotCommand(command="clear", description="🧹 Очистить временные сообщения"),
        BotCommand(command="help", description="ℹ️ Помощь"),
    ]
    await bot.set_my_commands(commands, BotCommandScopeDefault())

def inline_main_menu():
    kb = InlineKeyboardBuilder()
    kb.button(text="📊 Отчёт: Активные кампании", callback_data="build_report")
    kb.button(text="🧹 Очистить временные сообщения", callback_data="clear_chat")
    kb.button(text="ℹ️ Помощь", callback_data="help")
    kb.adjust(1)
    return kb.as_markup()

# ============================
# ===       Хендлеры       ===
# ============================
@router.message(Command("start", "restart"))
async def start_handler(msg: Message):
    await send_and_store(msg, "👋 Привет! Я твой бот для управления рекламой.\n\nВыберите действие:", is_persistent=True, reply_markup=inline_main_menu())

@router.callback_query(F.data == "show_menu")
async def show_menu_handler(call: CallbackQuery):
    await call.message.edit_text("👋 Привет! Я твой бот для управления рекламой.\n\nВыберите действие:", reply_markup=inline_main_menu())
    chat_id = call.message.chat.id
    if chat_id in sent_messages_by_chat:
        for msg_info in sent_messages_by_chat[chat_id]:
            if msg_info["id"] == call.message.message_id:
                msg_info["persistent"] = True
                break

@router.message(Command("help"))
@router.callback_query(F.data == "help")
async def help_handler(event: Message | CallbackQuery):
    help_text = (
        "<b>ℹ️ Справка по командам:</b>\n\n"
        "<b>/start</b> - Показать главное меню.\n"
        "<b>/report</b> - Сформировать отчёт по активным кампаниям.\n"
        "<b>/clear</b> - Удалить временные сообщения (отчёты, статусы загрузки), оставив меню и важные уведомления.\n\n"
        "Бот использует API Facebook для получения данных в реальном времени."
    )
    await send_and_store(event, help_text, is_persistent=True, reply_markup=inline_main_menu())

@router.message(Command("clear"))
@router.callback_query(F.data == "clear_chat")
async def clear_chat_handler(event: Message | CallbackQuery):
    message = event.message if isinstance(event, CallbackQuery) else event
    chat_id = message.chat.id
    if chat_id in sent_messages_by_chat and sent_messages_by_chat[chat_id]:
        messages_to_delete = [msg_info["id"] for msg_info in sent_messages_by_chat[chat_id] if not msg_info.get("persistent", False)]
        sent_messages_by_chat[chat_id] = [msg_info for msg_info in sent_messages_by_chat[chat_id] if msg_info.get("persistent", False)]
        count = 0
        for msg_id in messages_to_delete:
            try:
                await bot.delete_message(chat_id, msg_id)
                count += 1
            except TelegramBadRequest:
                pass
        await message.answer(f"✅ Готово! Удалил {count} временных сообщений.")
    else:
        await message.answer("ℹ️ Сообщений для удаления нет.")
    if isinstance(event, CallbackQuery):
        await start_handler(message)

# ============ Отчёт с лоадером ============
@router.message(Command("report"))
@router.callback_query(F.data == "build_report")
async def build_report(event: Message | CallbackQuery):
    message = event.message if isinstance(event, CallbackQuery) else event
    status_msg = await send_and_store(message, "⏳ Начинаю сбор данных...")
    active_accounts_data = []
    
    # ### ИЗМЕНЕНИЕ: Устанавливаем таймаут для всех запросов в сессии
    timeout = aiohttp.ClientTimeout(total=120) # 2 минуты на каждый запрос
    
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
                    # ### ИЗМЕНЕНИЕ: Детальные статусы для каждого шага
                    await status_msg.edit_text(base_text + " Cкачиваю кампании...")
                    campaigns = await get_campaigns(session, acc["account_id"])
                    active_campaigns = {c["id"]: c for c in campaigns if c.get("status") == "ACTIVE"}
                    if not active_campaigns: continue

                    await status_msg.edit_text(base_text + " Cкачиваю группы объявлений...")
                    adsets = await get_all_adsets(session, acc["account_id"])
                    active_adsets = [a for a in adsets if a.get("status") == "ACTIVE" and a.get("campaign_id") in active_campaigns]
                    if not active_adsets: continue

                    adset_ids = [a["id"] for a in active_adsets]
                    if not adset_ids: continue

                    await status_msg.edit_text(base_text + " Cкачиваю статистику...")
                    insights = await get_adset_insights(session, acc["account_id"], adset_ids)

                    # ... (остальная логика обработки данных)
                    spend_map, chats_map = {}, {}
                    for row in insights:
                        spend = float(row.get("spend", 0))
                        chats = sum(int(a["value"]) for a in row.get("actions", []) if a.get("action_type") == LEAD_ACTION_TYPE)
                        spend_map[row["adset_id"]] = spend
                        chats_map[row["adset_id"]] = chats

                    campaigns_data = {}
                    for ad in active_adsets:
                        camp_id = ad["campaign_id"]
                        campaign = active_campaigns.get(camp_id)
                        if not campaign: continue

                        spend = spend_map.get(ad["id"], 0)
                        leads = chats_map.get(ad["id"], 0)
                        if spend == 0 and leads == 0: continue

                        cpl = (spend / leads) if leads > 0 else 0
                        ad_data = {"name": ad["name"], "objective": campaign.get("objective", "N/A"), "cpl": cpl, "leads": leads, "spend": spend}
                        if camp_id not in campaigns_data:
                           campaigns_data[camp_id] = {"name": campaign["name"], "adsets": []}
                        campaigns_data[camp_id]["adsets"].append(ad_data)
                    
                    if campaigns_data:
                        active_accounts_data.append({"name": acc["name"], "campaigns": list(campaigns_data.values()), "active_count": len(campaigns_data)})

                except asyncio.TimeoutError:
                    await send_and_store(message, f"⚠️ <b>Превышен таймаут</b> при обработке кабинета <b>{acc['name']}</b>. Пропускаю его.")
                    continue # Переходим к следующему аккаунту
    
    except aiohttp.ClientResponseError as e:
        error_details = "Не удалось получить детали ошибки"
        if e.content_type == 'application/json':
            try:
                error_data = await e.json()
                error_details = error_data.get("error", {}).get("message", "Нет сообщения об ошибке")
            except:
                pass
        else:
            error_details = e.reason

        await status_msg.edit_text(f"❌ <b>Ошибка API Facebook:</b>\nКод: {e.status}\nСообщение: {error_details}")
        return
    except Exception as e:
        await status_msg.edit_text(f"❌ <b>Произошла неизвестная ошибка:</b>\n{type(e).__name__}: {e}")
        return

    if not active_accounts_data:
        await status_msg.edit_text("✅ Активных кампаний с затратами или лидами не найдено.")
        return
    
    try:
        await bot.delete_message(status_msg.chat.id, status_msg.message_id)
    except TelegramBadRequest:
        pass

    for acc in active_accounts_data:
        msg_lines = [f"<b>🏢 Рекламный кабинет:</b> <u>{acc['name']}</u>", f"📈 Активных кампаний: {acc['active_count']}\n"]
        for camp in acc["campaigns"]:
            msg_lines.append(f"<b>🎯 {camp['name']}</b>")
            for ad in sorted(camp["adsets"], key=lambda x: x['cpl']):
                status_emoji = "🟢" if ad["leads"] > 0 else "🔴"
                msg_lines.append(f"{status_emoji} <b>{ad['name']}</b>\n  Цель: {ad['objective']} | CPL: <b>${ad['cpl']:.2f}</b> ({cpl_label(ad['cpl'])})\n  Лиды: {ad['leads']} | Расход: ${ad['spend']:.2f}")
            msg_lines.append("")
        await send_and_store(message, "\n".join(msg_lines))

    await send_and_store(message, "✅ Отчёт завершён.", is_persistent=True, reply_markup=inline_main_menu())

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
