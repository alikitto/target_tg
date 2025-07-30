import os
import requests
from dotenv import load_dotenv
from aiogram import Bot, Dispatcher, executor, types
from datetime import date

# === Загрузка переменных окружения (.env) ===
load_dotenv()
TELEGRAM_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
META_TOKEN = os.getenv("META_ACCESS_TOKEN")

# === Инициализация Telegram бота ===
bot = Bot(token=TELEGRAM_TOKEN)
dp = Dispatcher(bot)

# === Получение рекламных аккаунтов из Meta Ads ===
def get_ad_accounts():
    url = "https://graph.facebook.com/v19.0/me/adaccounts"
    params = {
        "fields": "name,account_id,account_status",
        "access_token": META_TOKEN
    }
    r = requests.get(url, params=params).json()
    return r.get("data", [])

# === Получение всех кампаний для конкретного аккаунта ===
def get_campaigns(ad_account_id):
    url = f"https://graph.facebook.com/v19.0/{ad_account_id}/campaigns"
    params = {
        "fields": "id,name,status,daily_budget",
        "access_token": META_TOKEN
    }
    r = requests.get(url, params=params).json()
    return r.get("data", [])

# === Получение метрик кампании (Spend, Leads, CPL) ===
def get_campaign_insights(campaign_id):
    today = date.today().strftime("%Y-%m-%d")
    url = f"https://graph.facebook.com/v19.0/{campaign_id}/insights"
    params = {
        "fields": "spend,actions",
        "time_range": f"{{'since':'{today}','until':'{today}'}}",
        "access_token": META_TOKEN
    }
    r = requests.get(url, params=params).json()
    data = r.get("data", [])
    if not data:
        return {"spend": 0, "leads": 0, "cpl": 0}
    spend = float(data[0].get("spend", 0))
    actions = data[0].get("actions", [])
    leads = 0
    for action in actions:
        if action.get("action_type") == "lead":
            leads = int(action.get("value", 0))
    cpl = round(spend / leads, 2) if leads > 0 else 0
    return {"spend": spend, "leads": leads, "cpl": cpl}

# === /start ===
@dp.message_handler(commands=['start'])
async def start(message: types.Message):
    keyboard = types.InlineKeyboardMarkup()
    btn_all_active = types.InlineKeyboardButton("Показать активные кампании", callback_data="show_active_all")
    keyboard.add(btn_all_active)
    await message.reply("Привет! Нажми кнопку, чтобы увидеть активные кампании:", reply_markup=keyboard)

# === Показать активные кампании для всех аккаунтов ===
@dp.callback_query_handler(lambda c: c.data == "show_active_all")
async def show_active_all(callback_query: types.CallbackQuery):
    accounts = get_ad_accounts()
    if not accounts:
        await callback_query.message.reply("Аккаунты не найдены.")
        return

    result_text = []
    for acc in accounts:
        campaigns = get_campaigns(acc["account_id"])
        active = [c for c in campaigns if c["status"] == "ACTIVE"]

        if active:
            result_text.append(f"--- {acc['name']} ({acc['account_id']}) ---")
            for c in active:
                insights = get_campaign_insights(c["id"])
                result_text.append(
                    f"• {c['name']}\n"
                    f"   Расход: ${insights['spend']} | Лидов: {insights['leads']} | CPL: ${insights['cpl']}\n"
                )
        else:
            result_text.append(f"--- {acc['name']} --- Нет активных кампаний")

    await callback_query.message.reply("\n".join(result_text))
    await callback_query.answer()

# === Запуск бота ===
if __name__ == "__main__":
    executor.start_polling(dp, skip_updates=True)
