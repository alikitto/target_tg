import os
import asyncio
import requests
from aiogram import Bot, Dispatcher, Router
from aiogram.types import Message, CallbackQuery
from aiogram.filters import Command
from aiogram.utils.keyboard import InlineKeyboardBuilder
from dotenv import load_dotenv

load_dotenv()
TELEGRAM_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
META_TOKEN = os.getenv("META_ACCESS_TOKEN")

bot = Bot(token=TELEGRAM_TOKEN)
dp = Dispatcher()
router = Router()

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

# ================= Progress bar =================
def progress_bar(current, total, length=20):
    filled = int(length * current // total)
    return "▓" * filled + "░" * (length - filled)

# ================= Bot Handlers =================
@router.message(Command("start"))
async def start_handler(msg: Message):
    kb = InlineKeyboardBuilder()
    kb.button(text="Собрать отчёт", callback_data="build_report")
    await msg.answer("Привет! Нажми кнопку, чтобы собрать отчёт по активным кампаниям.", reply_markup=kb.as_markup())

@router.callback_query(lambda c: c.data == "build_report")
async def build_report(callback: CallbackQuery):
    status_msg = await callback.message.answer("Начинаю сбор данных…")
    await callback.answer()

    accounts = get_ad_accounts()
    if not accounts:
        await status_msg.edit_text("Нет рекламных аккаунтов.")
        return

    active_accounts_data = []

    for i, acc in enumerate(accounts, start=1):
        bar = progress_bar(i, len(accounts))
        await status_msg.edit_text(f"{bar}\nОбработка {i}/{len(accounts)}: {acc['name']}")

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

            cpl = (spend_map.get(ad["id"], 0) / chats_map.get(ad["id"], 1)) if chats_map.get(ad["id"], 0) > 0 else 0
            ad_data = {
                "name": ad["name"],
                "objective": campaign.get("objective", ""),
                "cpl": cpl,
                "leads": chats_map.get(ad["id"], 0),
                "spend": spend_map.get(ad["id"], 0),
            }
            if camp_id not in campaigns_data:
                campaigns_data[camp_id] = {"name": campaign["name"], "adsets": []}
            campaigns_data[camp_id]["adsets"].append(ad_data)

        active_accounts_data.append({
            "name": acc["name"],
            "campaigns": list(campaigns_data.values()),
            "active_count": len(campaigns_data)
        })

        await asyncio.sleep(0.3)

    if not active_accounts_data:
        await status_msg.edit_text("Активных кампаний не найдено.")
        return

    await status_msg.edit_text("Отчёт большой, отправляю частями…")

    for acc in active_accounts_data:
        msg_lines = []
        msg_lines.append("━━━━━━━━━━━━━━━━━━━━━━━━━━━━")
        msg_lines.append(f"🏢 Рекл. кабинет: {acc['name']}")
        msg_lines.append(f"📈 Активных кампаний: {acc['active_count']}\n")
        for camp in acc["campaigns"]:
            msg_lines.append(f"🎯 Кампания: {camp['name']}")
            for ad in camp["adsets"]:
                msg_lines.append(
                    f"• Ad Set: {ad['name']}\n"
                    f"   Цель: {ad['objective']} | CPL: ${ad['cpl']:.2f} | "
                    f"Лиды: {ad['leads']} | Расход: ${ad['spend']:.2f}"
                )
            msg_lines.append("")

        text = "\n".join(msg_lines)
        try:
            if len(text) > 3500:
                chunks = [text[i:i + 3500] for i in range(0, len(text), 3500)]
                for chunk in chunks:
                    await callback.message.answer(chunk)
                    await asyncio.sleep(0.2)
            else:
                await callback.message.answer(text)
        except Exception as e:
            await callback.message.answer(f"Ошибка при отправке данных аккаунта {acc['name']}: {e}")

    await status_msg.edit_text("Готово ✅")

# ================= Run =================
dp.include_router(router)
async def main():
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())
