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
    return await session.get(url, params=params)

async def get_ad_accounts(session: aiohttp.ClientSession):
    url = f"https://graph.facebook.com/{API_VERSION}/me/adaccounts"
    params = {"fields": "name,account_id"}
    response = await fb_get(session, url, params)
    response.raise_for_status()
    data = await response.json()
    return data.get("data", [])

async def get_ad_level_insights(session: aiohttp.ClientSession, account_id: str, date_preset: str):
    url = f"https://graph.facebook.com/{API_VERSION}/act_{account_id}/insights"
    params = {
        "fields": "campaign_name,adset_name,ad_name,spend,actions,ctr",
        "level": "ad",
        "date_preset": date_preset,
        "limit": 1000
    }
    response = await fb_get(session, url, params)
    response.raise_for_status()
    data = await response.json()
    return data.get("data", [])


# ============================
# ===  ОТЧЕТ ПО АКТИВНЫМ КАМПАНИЯМ ===
# ============================

@router.message(F.text == "📊 Активные кампании")
async def active_campaigns_period_select(message: Message):
    kb = InlineKeyboardBuilder()
    kb.button(text="Сегодня", callback_data="run_active_report:today")
    kb.button(text="Вчера", callback_data="run_active_report:yesterday")
    await message.answer("Выберите период для отчета по активным кампаниям:", reply_markup=kb.as_markup())

@router.callback_query(F.data.startswith("run_active_report:"))
async def run_active_report_handler(call: CallbackQuery):
    date_preset = call.data.split(":")[1]
    await call.message.edit_text(f"Собираю отчет по активным кампаниям за период '{date_preset}'...")
    
    final_text = ""
    timeout = aiohttp.ClientTimeout(total=180)
    try:
        async with aiohttp.ClientSession(timeout=timeout) as session:
            accounts = await get_ad_accounts(session)
            if not accounts:
                await call.message.edit_text("Не найдено рекламных аккаунтов.")
                return

            for acc in accounts:
                insights = await get_ad_level_insights(session, acc['account_id'], date_preset)
                if insights:
                    final_text += f"\n\n<b>🏢 Кабинет: {acc['name']}</b>\n"
                    for insight in insights:
                        spend = float(insight.get('spend', 0))
                        if spend > 0:
                            final_text += f"- {insight['campaign_name']} | {insight['adset_name']} | ${spend:.2f}\n"
            
            if not final_text:
                await call.message.edit_text("Активности с затратами за выбранный период не найдено.")
            else:
                await call.message.edit_text("<b>Отчет готов:</b>\n" + final_text)

    except aiohttp.ClientResponseError as e:
        await call.message.answer(f"❌ ОШИБКА API при создании отчета: {e.status}, {e.message}")
    except Exception as e:
        await call.message.answer(f"❌ КРИТИЧЕСКАЯ ОШИБКА при создании отчета: {e}")

# ============================
# === ДИАГНОСТИЧЕСКАЯ КОМАНДА ===
# ============================

@router.message(Command("debug"))
async def debug_yesterday_spend(message: Message):
    await message.answer("🔍 Начинаю диагностику... Проверяю каждый кабинет.")
    timeout = aiohttp.ClientTimeout(total=120)
    async with aiohttp.ClientSession(timeout=timeout) as session:
        try:
            accounts = await get_ad_accounts(session)
            if not accounts:
                await message.answer("❌ Не удалось получить список рекламных аккаунтов. Проверьте токен.")
                return

            for acc in accounts:
                url = f"https://graph.facebook.com/{API_VERSION}/act_{acc['account_id']}/insights"
                params = {"level": "account", "date_preset": "yesterday", "fields": "spend,impressions"}

                try:
                    response = await fb_get(session, url, params)
                    if response.status == 200:
                        data = await response.json()
                        if data.get("data"):
                            spend = float(data["data"][0].get("spend", 0))
                            impressions = int(data["data"][0].get("impressions", 0))
                            await message.answer(f"✅ <b>{acc['name']}</b>\nРасход: ${spend:.2f}, Показы: {impressions}")
                        else:
                             await message.answer(f"🟡 <b>{acc['name']}</b>\nЗапрос успешен, но Facebook вернул пустой ответ.")
                    else:
                        data = await response.json()
                        error_message = data.get("error", {}).get("message", "Нет деталей")
                        await message.answer(f"❌ <b>{acc['name']}</b>\nОШИБКА API! Код: {response.status}\nСообщение: {error_message}")
                except Exception as e:
                    await message.answer(f"КРИТИЧЕСКАЯ ОШИБКА при обработке кабинета {acc['name']}: {type(e).__name__} - {e}")

        except Exception as e:
            await message.answer(f"КРИТИЧЕСКАЯ ОШИБКА при получении списка аккаунтов: {type(e).__name__} - {e}")
            
    await message.answer("✅ Диагностика завершена.")


# ============================
# ===    Базовые команды     ===
# ============================
@router.message(Command("start"))
async def start_handler(msg: Message):
    kb = ReplyKeyboardMarkup(
        keyboard=[[KeyboardButton(text="📊 Активные кампании")]],
        resize_keyboard=True
    )
    await msg.answer("Бот в режиме теста. Доступен отчет по активным кампаниям и /debug", reply_markup=kb)
    
async def set_bot_commands(bot: Bot):
    await bot.set_my_commands([
        BotCommand(command="start", description="🚀 Начало работы"),
        BotCommand(command="debug", description="⚙️ Диагностика активности"),
    ])

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
