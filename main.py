import os
import asyncio
import requests
from aiogram import Bot, Dispatcher, Router, F
from aiogram.types import Message, CallbackQuery
from aiogram.filters import Command
from aiogram.utils.keyboard import InlineKeyboardBuilder
from dotenv import load_dotenv

load_dotenv()
TELEGRAM_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
META_TOKEN = os.getenv("META_ACCESS_TOKEN")

bot = Bot(token=TELEGRAM_TOKEN, parse_mode="HTML")
dp = Dispatcher()
router = Router()

# === Храним отправленные сообщения бота для очистки ===
sent_messages = []

# ================= Graph API helpers =================
def fb_get(url, params=None):
    if not params:
        params = {}
    params["access_token"] = META_TOKEN
    r = requests.get(url, params=params)
    r.raise_for_status()
    return r.json()

def get_ad_accounts():
    url = "https://graph.facebook.com/v19.0/me/adaccounts"
    data = fb_get(url, {"fields": "name,account_id"})
    return data.get("data", [])

def get_campaigns(account_id):
    url = f"https://graph.facebook.com/v19.0/act_{account_id}/campaigns"
    params = {"fields": "id,name,status,objective", "limit": 500}
    return fb_get(url, params).get("data", [])

def get_all_adsets(account_id):
    url = f"https://graph.facebook.com/v19.0/act_{account_id}/adsets"
    params = {"fields": "id,name,campaign_id,status", "limit": 500}
    return fb_get(url, params).get("data", [])

def get_adset_insights(account_id, adset_ids):
    url = f"https://graph.facebook.com/v19.0/act_{account_id}/insights"
    params = {
        "fields": "adset_id,spend,actions",
        "level": "adset",
        "filtering": f'[{{"field":"adset.id","operator":"IN","value":{adset_ids}}}]',
        "limit": 500
    }
    return fb_get(url, params).get("data", [])

# ================= Utils =================
def progress_bar(current, total, length=20):
    filled = int(length * current // total)
    return "▓" * filled + "░" * (length - filled)

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

# ================= Меню =================
def main_menu():
    kb = InlineKeyboardBuilder()
    kb.button(text="Отчёт: Активные кампании", callback_data="build_report")
    kb.button(text="Очистить чат", callback_data="clear_chat")
    kb.button(text="Перезапустить бота", callback_data="restart_bot")
    kb.button(text="Помощь", callback_data="help")
    kb.button(text="Выход", callback_data="exit")
    return kb.as_markup()

@router.message(Command("start"))
async def start_handler(msg: Message):
    await send_and_store(msg, "Привет! Это бот для анализа активных кампаний.\nВыберите действие:", reply_markup=main_menu())

@router.callback_query(F.data == "help")
async def help_callback(callback: CallbackQuery):
    await send_and_store(callback.message,
        "Бот собирает данные по активным кампаниям во всех ваших рекламных кабинетах "
        "Meta и выводит их в удобном формате.\n\n"
        "Кнопки меню:\n"
        "• <b>Отчёт: Активные кампании</b> – показать активные кампании\n"
        "• <b>Очистить чат</b> – удалить все сообщения бота\n"
        "• <b>Перезапустить бота</b> – вернуться к стартовому меню\n"
        "• <b>Выход</b> – закрыть меню"
    )
    await callback.answer()

@router.callback_query(F.data == "exit")
async def exit_callback(callback: CallbackQuery):
    await send_and_store(callback.message, "Меню закрыто. Для открытия введите /start")
    await callback.answer()

@router.callback_query(F.data == "restart_bot")
async def restart_callback(callback: CallbackQuery):
    global sent_messages
    sent_messages = []
    await send_and_store(callback.message, "Бот перезапущен! Выберите действие:", reply_markup=main_menu())
    await callback.answer()

@router.callback_query(F.data == "clear_chat")
async def clear_chat(callback: CallbackQuery):
    chat_id = callback.message.chat.id
    count = 0
    for msg_id in sent_messages:
        try:
            await bot.delete_message(chat_id, msg_id)
            count += 1
        except:
            pass
    sent_messages.clear()
    await send_and_store(callback.message, f"Чат очищен! Удалено сообщений: {count}", reply_markup=main_menu())
    await callback.answer()

# ================= Сбор отчёта =================
@router.callback_query(F.data == "build_report")
async def build_report(callback: CallbackQuery):
    status_msg = await send_and_store(callback.message, "Начинаю сбор данных…")
    await callback.answer()

    accounts = get_ad_accounts()
    if not accounts:
        await status_msg.edit_text("Нет рекламных аккаунтов.")
        return

    active_accounts_data = []

    for acc in accounts:
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
            adset_id = row["adset_id"]
            spend = float(row.get("spend", 0))
            chats = sum(int(a["value"]) for a in row.get("actions", [])
                        if a["action_type"] == "onsite_conversion.messaging_conversation_started_7d")
            spend_map[adset_id] = spend
            chats_map[adset_id] = chats

        campaigns_data = {}
        for ad in active_adsets:
            camp_id = ad["campaign_id"]
            campaign = active_campaigns.get(camp_id)
            if not campaign:
                continue

            spend = spend_map.get(ad["id"], 0)
            leads = chats_map.get(ad["id"], 0)

            # фильтр: нет трат и лидов → пропускаем
            if spend == 0 and leads == 0:
                continue

            cpl = (spend / leads) if leads > 0 else 0
            ad_data = {
                "name": ad["name"],
                "objective": campaign.get("objective", ""),
                "cpl": cpl,
                "leads": leads,
                "spend": spend,
            }
            if camp_id not in campaigns_data:
                campaigns_data[camp_id] = {"name": campaign["name"], "adsets": []}
            campaigns_data[camp_id]["adsets"].append(ad_data)

        if campaigns_data:
            active_accounts_data.append({
                "name": acc["name"],
                "campaigns": list(campaigns_data.values()),
                "active_count": len(campaigns_data)
            })

        await asyncio.sleep(0.2)

    if not active_accounts_data:
        await status_msg.edit_text("Активных кампаний не найдено.")
        return

    await status_msg.edit_text("Отчёт большой, отправляю по кабинетам…")

    for acc in active_accounts_data:
        msg_lines = []
        msg_lines.append(f"<b>Рекл. кабинет:</b> <u>{acc['name']}</u>")
        msg_lines.append(f"📈 Активных кампаний: {acc['active_count']}\n")
        for camp in acc["campaigns"]:
            msg_lines.append(f"🎯 <b>{camp['name']}</b>")
            for ad in camp["adsets"]:
                status_emoji = "🟢" if ad["leads"] > 0 else "🔴"
                msg_lines.append(
                    f"{status_emoji} Ad Set: {ad['name']}\n"
                    f"   Цель: {ad['objective']} | CPL: ${ad['cpl']:.2f} ({cpl_label(ad['cpl'])}) | "
                    f"Лиды: {ad['leads']} | Расход: ${ad['spend']:.2f}"
                )
            msg_lines.append("")
        text = "\n".join(msg_lines)
        await send_and_store(callback.message, text)

    await status_msg.edit_text("Готово ✅")

# ================= Run =================
dp.include_router(router)
async def main():
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())
