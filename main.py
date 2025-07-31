import os
import asyncio
import aiohttp
import json
from datetime import datetime
from aiogram import Bot, Dispatcher, Router, F
from aiogram.types import (Message, CallbackQuery, BotCommand, BotCommandScopeDefault,
                           ReplyKeyboardMarkup, KeyboardButton)
from aiogram.filters import Command
from aiogram.utils.keyboard import InlineKeyboardBuilder
from aiogram.exceptions import TelegramBadRequest
from dotenv import load_dotenv

# Импортируем функцию для дневного отчета
from daily_report import generate_daily_report_text

# --- Конфигурация и константы ---
load_dotenv()
TELEGRAM_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
META_TOKEN = os.getenv("META_ACCESS_TOKEN")
API_VERSION = "v19.0"
LEAD_ACTION_TYPE = "onsite_conversion.messaging_conversation_started_7d"
LINK_CLICK_ACTION_TYPE = "link_click"

# --- Инициализация ---
bot = Bot(token=TELEGRAM_TOKEN, parse_mode="HTML")
dp = Dispatcher()
router = Router()

# ============================
# ===    Функции API     ===
# ============================

async def fb_get(session: aiohttp.ClientSession, url: str, params: dict = None):
    params = params or {}
    params["access_token"] = META_TOKEN
    response = await session.get(url, params=params)
    response.raise_for_status()
    return await response.json()

async def get_ad_accounts(session: aiohttp.ClientSession):
    url = f"https://graph.facebook.com/{API_VERSION}/me/adaccounts"
    params = {"fields": "name,account_id"}
    data = await fb_get(session, url, params)
    return data.get("data", [])

async def get_ad_level_insights(session: aiohttp.ClientSession, account_id: str, date_preset: str):
    url = f"https://graph.facebook.com/{API_VERSION}/act_{account_id}/insights"
    params = {
        "fields": "campaign_name,adset_name,ad_name,spend,actions,ctr,objective,creative{thumbnail_url}",
        "level": "ad",
        "date_preset": date_preset,
        "limit": 1000
    }
    data = await fb_get(session, url, params)
    return data.get("data", [])

# ============================
# ===       Меню           ===
# ============================
def main_reply_menu() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text="📊 Активные кампании"), KeyboardButton(text="📈 Дневной отчёт")],
            [KeyboardButton(text="💡 Рекомендации (AI)"), KeyboardButton(text="🆘 Помощь")]
        ],
        resize_keyboard=True,
        input_field_placeholder="Выберите действие..."
    )

def inline_period_menu():
    kb = InlineKeyboardBuilder()
    kb.button(text="Сегодня", callback_data="run_active_report:today")
    kb.button(text="Вчера", callback_data="run_active_report:yesterday")
    kb.button(text="За 7 дней", callback_data="run_active_report:last_7d")
    kb.adjust(3)
    return kb.as_markup()

async def set_bot_commands(bot: Bot):
    await bot.set_my_commands([
        BotCommand(command="start", description="🚀 Перезапустить бота"),
        BotCommand(command="clear", description="🧹 Очистить чат"),
    ])

# ============================
# ===       Хендлеры       ===
# ============================

@router.message(Command("start", "restart"))
async def start_handler(msg: Message):
    await msg.answer("Бот готов к работе. Выберите тип отчета:", reply_markup=main_reply_menu())

@router.message(F.text.in_({"💡 Рекомендации (AI)", "🆘 Помощь"}))
async def future_functions_handler(message: Message):
    await message.answer(f"Вы выбрали: {message.text}\n\nЭтот функционал находится в разработке.")

# --- Хендлеры для Дневного Отчета ---
@router.message(F.text == "📈 Дневной отчёт")
async def daily_report_handler(message: Message):
    status_msg = await message.answer("⏳ Собираю дневную сводку, это может занять до минуты...")
    try:
        async with aiohttp.ClientSession() as session:
            accounts = await get_ad_accounts(session)
        if not accounts:
            await status_msg.edit_text("❌ Не найдено ни одного рекламного аккаунта.")
            return
        report_text = await generate_daily_report_text(accounts, META_TOKEN)
        await bot.delete_message(message.chat.id, status_msg.message_id)
        if len(report_text) > 4096:
            for x in range(0, len(report_text), 4096):
                await message.answer(report_text[x:x+4096], disable_web_page_preview=True)
        else:
            await message.answer(report_text, disable_web_page_preview=True)
    except Exception as e:
        await status_msg.edit_text(f"❌ Произошла ошибка при создании дневного отчёта:\n`{type(e).__name__}: {e}`")

# --- Хендлеры для Отчета по Активным Кампаниям ---
@router.message(F.text == "📊 Активные кампании")
async def active_campaigns_period_select(message: Message):
    await message.answer("Выберите период для отчета по активным кампаниям:", reply_markup=inline_period_menu())

@router.callback_query(F.data.startswith("run_active_report:"))
async def run_active_report_handler(call: CallbackQuery):
    date_preset = call.data.split(":")[1]
    await call.message.edit_text(f"⏳ Собираю отчет по кампаниям за период '{date_preset.replace('_', ' ')}'...")
    
    timeout = aiohttp.ClientTimeout(total=180)
    try:
        async with aiohttp.ClientSession(timeout=timeout) as session:
            accounts = await get_ad_accounts(session)
            if not accounts:
                await call.message.edit_text("Не найдено рекламных аккаунтов.")
                return

            report_lines = [f"<b>Отчет за период: {date_preset.replace('_', ' ').capitalize()}</b>"]
            total_accounts_with_activity = 0

            for acc in accounts:
                insights = await get_ad_level_insights(session, acc['account_id'], date_preset)
                if not insights:
                    continue
                
                # --- Структурирование данных ---
                structured_data = {}
                has_activity_in_acc = False
                for ad in insights:
                    spend = float(ad.get('spend', 0))
                    if spend <= 0:
                        continue
                    has_activity_in_acc = True
                    camp_name = ad['campaign_name']
                    adset_name = ad['adset_name']
                    if camp_name not in structured_data:
                        structured_data[camp_name] = {}
                    if adset_name not in structured_data[camp_name]:
                        structured_data[camp_name][adset_name] = []
                    structured_data[camp_name][adset_name].append(ad)

                if has_activity_in_acc:
                    total_accounts_with_activity += 1
                    report_lines.append(f"\n<b>🏢 Кабинет: <u>{acc['name']}</u></b>")

                    for camp_name, adsets in structured_data.items():
                        report_lines.append(f"\n<b>🎯 {camp_name}</b>")
                        for adset_name, ads in adsets.items():
                            report_lines.append(f"  <b>↳ Группа:</b> {adset_name}")
                            for ad in sorted(ads, key=lambda x: float(x.get('spend', 0)), reverse=True):
                                spend = float(ad['spend'])
                                leads = sum(int(a["value"]) for a in ad.get("actions", []) if a.get("action_type") == LEAD_ACTION_TYPE)
                                clicks = sum(int(a["value"]) for a in ad.get("actions", []) if a.get("action_type") == LINK_CLICK_ACTION_TYPE)
                                objective = ad.get('objective', 'N/A').upper()
                                cost_str = ""
                                if "TRAFFIC" in objective and clicks > 0:
                                    cost_str = f"CPC: ${spend/clicks:.2f}"
                                elif leads > 0:
                                    cost_str = f"CPL: ${spend/leads:.2f}"
                                
                                thumb_url = ad.get('creative', {}).get('thumbnail_url', '#')
                                report_lines.append(f'    <a href="{thumb_url}">▫️</a> {ad["ad_name"]}: ${spend:.2f} | {cost_str}')
            
            if total_accounts_with_activity == 0:
                await call.message.edit_text("✅ Активности с затратами за выбранный период не найдено.")
            else:
                final_report = "\n".join(report_lines)
                if len(final_report) > 4096:
                    await call.message.edit_text("Отчет слишком большой, отправляю по частям...")
                    for x in range(0, len(final_report), 4096):
                        await call.message.answer(final_report[x:x+4096], disable_web_page_preview=True)
                else:
                    await call.message.edit_text(final_report, disable_web_page_preview=True)

    except aiohttp.ClientResponseError as e:
        data = await e.json()
        error_message = data.get("error", {}).get("message", e.message)
        await call.message.answer(f"❌ ОШИБКА API: {e.status}, {error_message}")
    except Exception as e:
        await call.message.answer(f"❌ КРИТИЧЕСКАЯ ОШИБКА: {type(e).__name__} - {e}")

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
    except (KeyboardInterrupt, SystemExit):
        print("Бот остановлен.")
