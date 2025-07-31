import os
import asyncio
import requests
from datetime import date
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

# === Graph API helper ===
def fb_get(url, params=None):
    if not params:
        params = {}
    params["access_token"] = META_TOKEN
    r = requests.get(url, params=params)
    r.raise_for_status()
    return r.json()

# === Получение всех аккаунтов ===
def get_ad_accounts():
    url = "https://graph.facebook.com/v19.0/me/adaccounts"
    data = fb_get(url, {"fields": "name,account_id"})
    return data.get("data", [])

# === Получение всех adset текущего аккаунта ===
def get_all_adsets(account_id):
    url = f"https://graph.facebook.com/v19.0/act_{account_id}/adsets"
    params = {"fields": "id,name,campaign_id,status", "limit": 500}
    return fb_get(url, params).get("data", [])

# === Получение кампаний ===
def get_campaigns(account_id):
    url = f"https://graph.facebook.com/v19.0/act_{account_id}/campaigns"
    params = {"fields": "id,name,status,objective", "limit": 500}
    return fb_get(url, params).get("data", [])

# === Insights для CPL и сообщений ===
def get_adset_insights(account_id, adset_ids):
    url = f"https://graph.facebook.com/v19.0/act_{account_id}/insights"
    params = {
        "fields": "adset_id,spend,actions",
        "level": "adset",
        "filtering": f'[{{"field":"adset.id","operator":"IN","value":{adset_ids}}}]',
        "limit": 500
    }
    return fb_get(url, params).get("data", [])

# === Прогресс-бар ===
def progress_bar(current, total, length=20):
    filled = int(length * current // total)
    return "▓" * filled + "░" * (length - filled)

# === /start ===
@router.message(Command("start"))
async def start_handler(msg: Message):
    kb = InlineKeyboardBuilder()
    kb.button(text="Собрать отчёт", callback_data="build_report")
    await msg.answer("Привет! Нажми кнопку, чтобы собрать отчёт по активным кампаниям.", reply_markup=kb.as_markup())

# === Основная логика ===
@router.callback_query(lambda c: c.data == "build_report")
async def build_report(callback: CallbackQuery):
    status_msg = await callback.message.answer("Начинаю сбор данных…")
    await callback.answer()

    accounts = get_ad_accounts()
    if not accounts:
        await status_msg.edit_text("Нет рекламных аккаунтов.")
        return

    report_lines_all = []
    for i, acc in enumerate(accounts, start=1):
        bar = progress_bar(i, len(accounts))
        await status_msg.edit_text(f"{bar}\nОбработка {i}/{len(accounts)}: {acc['name']}")

        # === Получаем кампании и adsets ===
        campaigns = get_campaigns(acc["account_id"])
        active_campaigns = {c["id"]: c for c in campaigns if c.get("status") == "ACTIVE"}
        adsets = get_all_adsets(acc["account_id"])
        active_adsets = [a for a in adsets if a.get("status") == "ACTIVE" and a.get("campaign_id") in active_campaigns]

        if not active_adsets:
            report_lines_all.append(f"--- {acc['name']} --- Нет активных кампаний.")
            continue

        # === Insights по активным adsets ===
        adset_ids = [a["id"] for a in active_adsets]
        insights = get_adset_insights(acc["account_id"], adset_ids)
        spend_map = {}
        chats_map = {}
        for row in insights:
            adset_id = row["adset_id"]
            spend = float(row.get("spend", 0))
            chats = 0
            for action in row.get("actions", []):
                if action["action_type"] == "onsite_conversion.messaging_conversation_started_7d":
                    chats += int(action["value"])
            spend_map[adset_id] = spend
            chats_map[adset_id] = chats

        # === Формируем отчёт ===
        report_lines_all.append(f"--- {acc['name']} ---")
        for ad in active_adsets:
            spend = spend_map.get(ad["id"], 0)
            chats = chats_map.get(ad["id"], 0)
            cpl = (spend / chats) if chats > 0 else 0
            campaign = active_campaigns.get(ad["campaign_id"], {})
            report_lines_all.append(
                f"{campaign.get('name','')} | {ad['name']} | "
                f"Статус: {ad.get('status')} | Цель: {campaign.get('objective','')} | "
                f"Лидов: {chats} | CPL: ${cpl:.2f} | Расход: ${spend:.2f}"
            )
        await asyncio.sleep(0.5)

    await status_msg.edit_text("Отчёт готов. Отправляю данные…")
    await callback.message.answer("\n".join(report_lines_all))
    await status_msg.edit_text("Готово ✅")

# === Запуск ===
dp.include_router(router)
async def main():
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())
