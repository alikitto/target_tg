import os
import asyncio
import requests
from datetime import date, timedelta
from collections import defaultdict
from aiogram import Bot, Dispatcher, Router
from aiogram.types import Message, CallbackQuery
from aiogram.filters import Command
from aiogram.utils.keyboard import InlineKeyboardBuilder
from dotenv import load_dotenv

# === Загружаем токен ===
load_dotenv()
TELEGRAM_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
META_TOKEN = os.getenv("META_ACCESS_TOKEN")

bot = Bot(token=TELEGRAM_TOKEN)
dp = Dispatcher()
router = Router()

# === Получаем активные кампании (через Insights) ===
def get_active_campaigns(ad_account_id):
    # Диапазон дат (последние 30 дней)
    since = (date.today() - timedelta(days=30)).strftime("%Y-%m-%d")
    until = date.today().strftime("%Y-%m-%d")

    url = f"https://graph.facebook.com/v19.0/{ad_account_id}/insights"
    params = {
        "fields": "campaign_id,campaign_name,spend,actions",
        "level": "adset",
        "filtering": '[{"field":"adset.effective_status","operator":"IN","value":["ACTIVE"]}]',
        "time_range": f'{{"since":"{since}","until":"{until}"}}',
        "access_token": META_TOKEN
    }
    r = requests.get(url, params=params).json()
    data = r.get("data", [])
    if not data:
        return []

    campaigns = defaultdict(lambda: {"spend": 0.0, "leads": 0})
    for row in data:
        cid = row["campaign_id"]
        cname = row["campaign_name"]
        spend = float(row.get("spend", 0))
        leads = 0
        for action in row.get("actions", []):
            if action["action_type"] == "lead":
                leads += int(action["value"])
        campaigns[cid]["name"] = cname
        campaigns[cid]["spend"] += spend
        campaigns[cid]["leads"] += leads

    result = []
    for cid, stats in campaigns.items():
        cpl = round(stats["spend"] / stats["leads"], 2) if stats["leads"] > 0 else 0
        result.append({
            "id": cid,
            "name": stats["name"],
            "spend": round(stats["spend"], 2),
            "leads": stats["leads"],
            "cpl": cpl
        })
    return result

# === Получаем аккаунты (от твоего пользователя) ===
def get_ad_accounts():
    url = "https://graph.facebook.com/v19.0/me/adaccounts"
    params = {"fields": "name,account_id", "access_token": META_TOKEN}
    r = requests.get(url, params=params).json()
    return r.get("data", [])

# === Команды бота ===
@router.message(Command("start"))
async def start_handler(msg: Message):
    kb = InlineKeyboardBuilder()
    kb.button(text="Показать активные кампании", callback_data="show_active_all")
    await msg.answer("Привет! Этот бот работает только для твоего аккаунта Meta Ads.\nВыбери действие:", reply_markup=kb.as_markup())

@router.callback_query(lambda c: c.data == "show_active_all")
async def show_active_all(callback: CallbackQuery):
    accounts = get_ad_accounts()
    if not accounts:
        await callback.message.answer("Аккаунты не найдены или нет доступа.")
        await callback.answer()
        return

    result_text = []
    for acc in accounts:
        campaigns = get_active_campaigns(acc["account_id"])
        if campaigns:
            result_text.append(f"--- {acc['name']} ({acc['account_id']}) ---")
            for c in campaigns:
                result_text.append(
                    f"• {c['name']}\n"
                    f"   Расход: ${c['spend']} | Лидов: {c['leads']} | CPL: ${c['cpl']}\n"
                )
        else:
            result_text.append(f"--- {acc['name']} --- Нет активных кампаний за последние 30 дней")

    await callback.message.answer("\n".join(result_text))
    await callback.answer()

dp.include_router(router)

async def main():
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())
