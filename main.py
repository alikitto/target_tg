import os
import asyncio
import requests
from aiogram import Bot, Dispatcher, Router, F
from aiogram.types import Message, CallbackQuery, BotCommand
from aiogram.filters import Command
from aiogram.utils.keyboard import InlineKeyboardBuilder
from dotenv import load_dotenv

load_dotenv()
TELEGRAM_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
META_TOKEN = os.getenv("META_ACCESS_TOKEN")

bot = Bot(token=TELEGRAM_TOKEN, parse_mode="HTML")
dp = Dispatcher()
router = Router()
sent_messages = []  # для очистки чата

# ============ Graph API ============
def fb_get(url, params=None):
    params = params or {}
    params["access_token"] = META_TOKEN
    r = requests.get(url, params=params)
    r.raise_for_status()
    return r.json()

def get_ad_accounts():
    return fb_get("https://graph.facebook.com/v19.0/me/adaccounts",
                  {"fields": "name,account_id"}).get("data", [])

def get_campaigns(account_id):
    return fb_get(f"https://graph.facebook.com/v19.0/act_{account_id}/campaigns",
                  {"fields": "id,name,status,objective", "limit": 500}).get("data", [])

def get_all_adsets(account_id):
    return fb_get(f"https://graph.facebook.com/v19.0/act_{account_id}/adsets",
                  {"fields": "id,name,campaign_id,status", "limit": 500}).get("data", [])

def get_adset_insights(account_id, adset_ids):
    return fb_get(f"https://graph.facebook.com/v19.0/act_{account_id}/insights",
                  {"fields": "adset_id,spend,actions", "level": "adset",
                   "filtering": f'[{{"field":"adset.id","operator":"IN","value":{adset_ids}}}]',
                   "limit": 500}).get("data", [])

# ============ Helpers ============
def cpl_label(cpl):
    if cpl <= 1:
        return "🟢 Дешёвый"
    elif cpl <= 3:
        return "🟡 Средний"
    return "🔴 Дорогой"

async def send_and_store(message, text, **kwargs):
    msg = await message.answer(text, **kwargs)
    sent_messages.append(msg.message_id)
    return msg

# ============ Меню ============
async def set_bot_commands(bot: Bot):
    commands = [
        BotCommand(command="report", description="Отчёт по активным кампаниям"),
        BotCommand(command="clear", description="Очистить чат"),
        BotCommand(command="restart", description="Перезапустить бота"),
        BotCommand(command="help", description="Помощь"),
    ]
    await bot.set_my_commands(commands)

def inline_main_menu():
    kb = InlineKeyboardBuilder()
    kb.button(text="Отчёт: Активные кампании", callback_data="build_report")
    kb.button(text="Очистить чат", callback_data="clear_chat")
    kb.button(text="Перезапустить бота", callback_data="restart_bot")
    kb.button(text="Помощь", callback_data="help")
    return kb.as_markup()

# ============ Handlers ============
@router.message(Command("start"))
async def start_handler(msg: Message):
    await send_and_store(msg, "👋 Привет! Выберите действие:", reply_markup=inline_main_menu())

# ... (остальные хендлеры clear, restart, help остаются прежними)

# ============ Отчёт с лоадером ============
@router.message(Command("report"))
@router.callback_query(F.data == "build_report")
async def build_report(event):
    message = event.message if isinstance(event, CallbackQuery) else event
    status_msg = await send_and_store(message, "⏳ Начинаю сбор данных...")

    accounts = get_ad_accounts()
    if not accounts:
        await status_msg.edit_text("❌ Нет рекламных аккаунтов.")
        return

    total = len(accounts)
    active_accounts_data = []

    for idx, acc in enumerate(accounts, start=1):
        await status_msg.edit_text(f"📦 Обрабатываю кабинет {idx}/{total}\n<b>{acc['name']}</b>")
        await asyncio.sleep(0.2)

        campaigns = get_campaigns(acc["account_id"])
        active_campaigns = {c["id"]: c for c in campaigns if c.get("status") == "ACTIVE"}
        adsets = get_all_adsets(acc["account_id"])
        active_adsets = [a for a in adsets if a.get("status") == "ACTIVE" and a.get("campaign_id") in active_campaigns]

        if not active_adsets:
            continue

        adset_ids = [a["id"] for a in active_adsets]
        insights = get_adset_insights(acc["account_id"], adset_ids)

        spend_map, chats_map = {}, {}
        for row in insights:
            spend = float(row.get("spend", 0))
            chats = sum(int(a["value"]) for a in row.get("actions", [])
                        if a["action_type"] == "onsite_conversion.messaging_conversation_started_7d")
            spend_map[row["adset_id"]] = spend
            chats_map[row["adset_id"]] = chats

        campaigns_data = {}
        for ad in active_adsets:
            camp_id = ad["campaign_id"]
            campaign = active_campaigns.get(camp_id)
            if not campaign:
                continue

            spend = spend_map.get(ad["id"], 0)
            leads = chats_map.get(ad["id"], 0)
            if spend == 0 and leads == 0:
                continue

            cpl = (spend / leads) if leads > 0 else 0
            ad_data = {"name": ad["name"], "objective": campaign.get("objective", ""),
                       "cpl": cpl, "leads": leads, "spend": spend}
            campaigns_data.setdefault(camp_id, {"name": campaign["name"], "adsets": []})
            campaigns_data[camp_id]["adsets"].append(ad_data)

        if campaigns_data:
            active_accounts_data.append({"name": acc["name"],
                                         "campaigns": list(campaigns_data.values()),
                                         "active_count": len(campaigns_data)})

    if not active_accounts_data:
        await status_msg.edit_text("Нет активных кампаний.")
        return

    await status_msg.edit_text("📊 <b>Отчёт готов!</b>\nОтправляю данные...")

    for acc in active_accounts_data:
        msg_lines = [f"<b>🏢 Рекл. кабинет:</b> <u>{acc['name']}</u>",
                     f"📈 Активных кампаний: {acc['active_count']}\n"]
        for camp in acc["campaigns"]:
            msg_lines.append(f"🎯 <b>{camp['name']}</b>")
            for ad in camp["adsets"]:
                status_emoji = "🟢" if ad["leads"] > 0 else "🔴"
                msg_lines.append(
                    f"{status_emoji} <b>{ad['name']}</b>\n"
                    f"   Цель: {ad['objective']} | CPL: ${ad['cpl']:.2f} ({cpl_label(ad['cpl'])})\n"
                    f"   Лиды: {ad['leads']} | Расход: ${ad['spend']:.2f}"
                )
            msg_lines.append("")
        await send_and_store(message, "\n".join(msg_lines))

    await send_and_store(message, "✅ Отчёт завершён.", reply_markup=inline_main_menu())

# ============ Запуск ============
dp.include_router(router)
async def main():
    await set_bot_commands(bot)
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())
