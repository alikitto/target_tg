import logging
import requests
import asyncio
from telegram import (
    Update,
    ReplyKeyboardMarkup
)
from telegram.ext import (
    ApplicationBuilder,
    CommandHandler,
    MessageHandler,
    ContextTypes,
    filters
)

# ==== НАСТРОЙКИ ====
TELEGRAM_TOKEN = "YOUR_TELEGRAM_BOT_TOKEN"
META_ACCESS_TOKEN = "YOUR_META_ACCESS_TOKEN"
API_VERSION = "v19.0"

# ==== Список клиентов (пример) ====
CLIENTS = [
    {"name": "Ahad Nazim", "account_id": "act_284902192299330"},
    {"name": "Tural Multi", "account_id": "act_1234567890"},
    # добавь свои аккаунты
]

# ==== Маппинг целей -> action type ====
OBJECTIVE_ACTION_MAP = {
    "OUTCOME_ENGAGEMENT": "onsite_conversion.messaging_conversation_started_7d",
    "MESSAGES": "onsite_conversion.messaging_conversation_started_7d",
    "OUTCOME_TRAFFIC": "link_click",
    "LEAD_GENERATION": "lead"
}

# ==== УТИЛИТЫ ====
def get_cpl_color(cpl):
    if cpl == 0:
        return "🔴 Нет лидов"
    elif cpl < 0.5:
        return "🟢 Дешёвый"
    elif cpl < 1.5:
        return "🟡 Средний"
    else:
        return "🔴 Дорогой"

def fetch_insights(account_id, date_preset="today"):
    url = f"https://graph.facebook.com/{API_VERSION}/{account_id}/insights"
    params = {
        "fields": "campaign_name,adset_name,objective,actions,spend",
        "level": "adset",
        "filtering": '[{"field":"adset.effective_status","operator":"IN","value":["ACTIVE"]}]',
        "date_preset": date_preset,
        "access_token": META_ACCESS_TOKEN
    }
    return requests.get(url, params=params).json()

def parse_data(data):
    results = []
    for item in data.get("data", []):
        objective = item.get("objective")
        action_type = OBJECTIVE_ACTION_MAP.get(objective, "link_click")
        spend = float(item.get("spend", 0))
        actions = item.get("actions", [])
        leads = 0
        for a in actions:
            if a["action_type"] == action_type:
                leads = int(a["value"])
        cpl = spend / leads if leads > 0 else 0
        results.append({
            "campaign": item.get("campaign_name", ""),
            "adset": item.get("adset_name", ""),
            "objective": objective,
            "leads": leads,
            "spend": spend,
            "cpl": cpl
        })
    return results

def format_report(client_name, adsets):
    if not adsets:
        return None
    message = f"📢 <b>{client_name}</b>\nАктивных групп: {len(adsets)}\n\n"
    for adset in adsets:
        status_emoji = "🟢" if adset["leads"] > 0 else "🔴"
        cpl_color = get_cpl_color(adset["cpl"])
        message += (
            f"{status_emoji} {adset['campaign']}\n"
            f"{adset['adset']}\n"
            f"Цель: {adset['objective']} | CPL: ${adset['cpl']:.2f} ({cpl_color})\n"
            f"Лиды: {adset['leads']} | Расход: ${adset['spend']:.2f}\n\n"
        )
    return message

# ==== ОБРАБОТЧИКИ ====
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    keyboard = [
        ["📊 Отчёт (все активные)"],
        ["📅 Отчёт за сегодня"],
        ["🧹 Очистить чат"],
        ["🔄 Перезапуск"]
    ]
    reply_markup = ReplyKeyboardMarkup(keyboard, resize_keyboard=True)
    await update.message.reply_text("Привет! Выбери действие:", reply_markup=reply_markup)

async def clear_chat(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    messages = await context.bot.get_chat(chat_id)
    async for message in context.bot.get_chat_history(chat_id):
        try:
            await context.bot.delete_message(chat_id=chat_id, message_id=message.message_id)
        except:
            pass
    await update.message.reply_text("Чат очищен!")

async def generate_report(update: Update, context: ContextTypes.DEFAULT_TYPE, date_preset="today"):
    progress = await update.message.reply_text("⏳ Начинаю сбор данных...")
    total = len(CLIENTS)

    for i, client in enumerate(CLIENTS, start=1):
        await progress.edit_text(f"🔍 Обработка клиента {i}/{total}: {client['name']}")
        await asyncio.sleep(0.3)

        data = fetch_insights(client["account_id"], date_preset)
        adsets = parse_data(data)
        msg = format_report(client["name"], adsets)
        if msg:
            await context.bot.send_message(chat_id=update.effective_chat.id, text=msg, parse_mode="HTML")

    await progress.edit_text("✅ Отчёт завершён!")

async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text
    if "Отчёт (все активные)" in text:
        await generate_report(update, context, date_preset="maximum")  # можно заменить на last_30d
    elif "Отчёт за сегодня" in text:
        await generate_report(update, context, date_preset="today")
    elif "Очистить чат" in text:
        await clear_chat(update, context)
    elif "Перезапуск" in text:
        await start(update, context)

# ==== MAIN ====
def main():
    logging.basicConfig(level=logging.INFO)
    app = ApplicationBuilder().token(TELEGRAM_TOKEN).build()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))

    app.run_polling()

if __name__ == "__main__":
    main()
