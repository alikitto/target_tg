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

# --- Инициализация ---
bot = Bot(token=TELEGRAM_TOKEN, parse_mode="HTML")
dp = Dispatcher()
router = Router()

# ============================
# ===    Базовые функции API     ===
# ============================

async def fb_get(session: aiohttp.ClientSession, url: str, params: dict = None):
    """Базовая асинхронная функция для выполнения GET-запросов к Graph API."""
    params = params or {}
    params["access_token"] = META_TOKEN
    async with session.get(url, params=params) as response:
        # ВАЖНО: Мы больше не вызываем response.raise_for_status() здесь,
        # чтобы обработать ошибки вручную в месте вызова.
        return response

async def get_ad_accounts(session: aiohttp.ClientSession):
    """Получает список рекламных аккаунтов."""
    url = f"https://graph.facebook.com/{API_VERSION}/me/adaccounts"
    params = {"fields": "name,account_id"}
    response = await fb_get(session, url, params)
    if response.status == 200:
        data = await response.json()
        return data.get("data", [])
    response.raise_for_status() # Если сам запрос аккаунтов не прошел, показываем ошибку
    return []


# ============================
# === ДИАГНОСТИЧЕСКАЯ КОМАНДА ===
# ============================

@router.message(Command("debug"))
async def debug_yesterday_spend(message: Message):
    """
    Проверяет каждый аккаунт на наличие расхода и показов за вчера.
    """
    await message.answer("🔍 Начинаю диагностику... Проверяю каждый кабинет.")
    
    timeout = aiohttp.ClientTimeout(total=120)
    async with aiohttp.ClientSession(timeout=timeout) as session:
        try:
            accounts = await get_ad_accounts(session)
            if not accounts:
                await message.answer("❌ Не удалось получить список рекламных аккаунтов. Проверьте токен.")
                return

            for acc in accounts:
                acc_id = acc['account_id']
                acc_name = acc['name']
                
                url = f"https://graph.facebook.com/{API_VERSION}/act_{acc_id}/insights"
                params = {
                    "level": "account",
                    "date_preset": "yesterday",
                    "fields": "spend,impressions"
                }

                try:
                    response = await fb_get(session, url, params)
                    data = await response.json()

                    if response.status == 200:
                        if data.get("data"):
                            spend = float(data["data"][0].get("spend", 0))
                            impressions = int(data["data"][0].get("impressions", 0))
                            if spend > 0 or impressions > 0:
                                await message.answer(f"✅ <b>{acc_name}</b>\nНайдена активность. Расход: ${spend:.2f}, Показы: {impressions}")
                            else:
                                await message.answer(f"🟡 <b>{acc_name}</b>\nЗапрос успешен, но расход и показы за вчера равны нулю.")
                        else:
                             await message.answer(f"🟡 <b>{acc_name}</b>\nЗапрос успешен, но Facebook вернул пустой ответ (нет данных).")
                    else:
                        # Если статус не 200, но есть JSON с ошибкой
                        error_message = data.get("error", {}).get("message", "Нет деталей")
                        await message.answer(f"❌ <b>{acc_name}</b>\nОШИБКА API! Код: {response.status}\nСообщение: {error_message}")

                except Exception as e:
                    await message.answer(f"CRITICAL: Произошла критическая ошибка при обработке кабинета {acc_name}: {e}")

        except aiohttp.ClientResponseError as e:
            await message.answer(f"CRITICAL: Не удалось получить список аккаунтов. Ошибка API: {e.status}, {e.message}")
        except Exception as e:
            await message.answer(f"CRITICAL: Неизвестная критическая ошибка: {e}")
            
    await message.answer("✅ Диагностика завершена.")


# ============================
# ===    Остальные команды     ===
# ============================
# Оставим только самые базовые команды, чтобы ничего не мешало диагностике

@router.message(Command("start"))
async def start_handler(msg: Message):
    await msg.answer("Бот в режиме диагностики. Используйте команду /debug")
    
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
