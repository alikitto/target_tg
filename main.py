Отлично, давайте начнем с улучшения кода.

Вот полный, переработанный `main.py`.

### Ключевые изменения:

1.  **Асинхронные запросы:** `requests` заменен на `httpx` для асинхронных вызовов, что исключает блокировку бота.
2.  **Обработка ошибок:** Добавлены блоки `try-except` вокруг вызовов API. Если один из аккаунтов выдаст ошибку, бот не остановится, а сообщит о проблеме и продолжит работу.
3.  **Структура и читаемость:** Логика обработки одного аккаунта вынесена в отдельную функцию `process_account`, что делает основной цикл чище.
4.  **Подготовка к уведомлениям:** Добавлены комментарии в местах, где можно будет встроить логику для проверки пороговых значений (например, CPL \> X или лидов = 0 при расходе \> Y) и отправки уведомлений.

-----

### Обновленный код `main.py`

```python
import os
import asyncio
import httpx # 👈 Замена requests
from aiogram import Bot, Dispatcher, Router, F
from aiogram.types import Message, CallbackQuery, BotCommand
from aiogram.filters import Command
from aiogram.utils.keyboard import InlineKeyboardBuilder
from dotenv import load_dotenv

# --- Загрузка конфигурации ---
load_dotenv()
TELEGRAM_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
META_TOKEN = os.getenv("META_ACCESS_TOKEN")
if not TELEGRAM_TOKEN or not META_TOKEN:
    raise ValueError("Необходимо задать TELEGRAM_BOT_TOKEN и META_ACCESS_TOKEN в .env файле")

# --- Инициализация ---
bot = Bot(token=TELEGRAM_TOKEN, parse_mode="HTML")
dp = Dispatcher()
router = Router()
http_client = httpx.AsyncClient() # 👈 Создаем один клиент для всего приложения
sent_messages = []

# ============================
# === Блок работы с Meta API ===
# ============================

async def fb_get(url: str, params: dict = None) -> dict:
    """Асинхронная функция для выполнения GET-запросов к Meta Graph API."""
    params = params or {}
    params["access_token"] = META_TOKEN
    try:
        r = await http_client.get(url, params=params, timeout=30.0)
        r.raise_for_status()
        return r.json()
    except httpx.HTTPStatusError as e:
        # Ошибка от API (например, нет прав, неверный ID)
        print(f"Ошибка статуса API Meta: {e.response.status_code} - {e.response.text}")
        return {"error": f"API Error: {e.response.status_code}"}
    except httpx.RequestError as e:
        # Сетевая ошибка (недоступен сервер, таймаут)
        print(f"Сетевая ошибка при запросе к Meta API: {e}")
        return {"error": f"Network Error: {e}"}

async def get_ad_accounts() -> list:
    """Получает список рекламных аккаунтов."""
    data = await fb_get("https://graph.facebook.com/v19.0/me/adaccounts", {"fields": "name,account_id"})
    return data.get("data", [])

async def get_active_campaigns(account_id: str) -> dict:
    """Получает активные кампании для аккаунта."""
    params = {
        "fields": "id,name,status,objective",
        "filtering": '[{"field":"status","operator":"IN","value":["ACTIVE"]}]',
        "limit": 500
    }
    data = await fb_get(f"https://graph.facebook.com/v19.0/act_{account_id}/campaigns", params)
    return {c["id"]: c for c in data.get("data", [])}

async def get_active_adsets(account_id: str, campaign_ids: list) -> list:
    """Получает активные адсеты для указанных кампаний."""
    if not campaign_ids:
        return []
    params = {
        "fields": "id,name,campaign_id,status",
        "filtering": f'[{{"field":"status","operator":"IN","value":["ACTIVE"]}}, {{"field":"campaign_id","operator":"IN","value":{campaign_ids}}}]',
        "limit": 500
    }
    data = await fb_get(f"https://graph.facebook.com/v19.0/act_{account_id}/adsets", params)
    return data.get("data", [])

async def get_adset_insights(account_id: str, adset_ids: list) -> list:
    """Получает статистику (insights) для указанных адсетов."""
    if not adset_ids:
        return []
    params = {
        "fields": "adset_id,spend,actions",
        "level": "adset",
        "filtering": f'[{{"field":"adset.id","operator":"IN","value":{adset_ids}}}]',
        "limit": 500
    }
    data = await fb_get(f"https://graph.facebook.com/v19.0/act_{account_id}/insights", params)
    return data.get("data", [])

# ============================
# === Вспомогательные функции ===
# ============================

def cpl_label(cpl: float) -> str:
    """Возвращает текстовую метку для CPL."""
    if cpl <= 1:
        return "🟢 Дешёвый"
    if cpl <= 3:
        return "🟡 Средний"
    return "🔴 Дорогой"

async def send_and_store(message: Message, text: str, **kwargs):
    """Отправляет сообщение и сохраняет его ID для последующей очистки."""
    msg = await message.answer(text, **kwargs)
    sent_messages.append(msg.message_id)
    return msg

# ============================
# === Меню и Команды бота ===
# ============================

async def set_bot_commands(bot: Bot):
    """Устанавливает команды для меню бота."""
    commands = [
        BotCommand(command="report", description="Отчёт по активным кампаниям"),
        BotCommand(command="clear", description="Очистить чат"),
        BotCommand(command="restart", description="Перезапустить бота"),
        BotCommand(command="help", description="Помощь"),
    ]
    await bot.set_my_commands(commands)

def inline_main_menu() -> InlineKeyboardBuilder.as_markup:
    """Создает инлайн-клавиатуру главного меню."""
    kb = InlineKeyboardBuilder()
    kb.button(text="📊 Отчёт: Активные кампании", callback_data="build_report")
    # TODO: Добавить кнопки для управления уведомлениями и AI-анализа
    # kb.button(text="🤖 AI Анализ", callback_data="ai_analysis")
    # kb.button(text="🔔 Настроить уведомления", callback_data="notifications_settings")
    kb.button(text="Очистить чат", callback_data="clear_chat")
    kb.button(text="Перезапустить бота", callback_data="restart_bot")
    kb.button(text="Помощь", callback_data="help")
    kb.adjust(1) # Все кнопки в один столбец
    return kb.as_markup()

# ============================
# === Логика обработки отчета ===
# ============================

async def process_account(acc: dict) -> dict | None:
    """Обрабатывает один рекламный аккаунт: собирает и структурирует данные."""
    account_id = acc["account_id"]
    active_campaigns = await get_active_campaigns(account_id)
    if not active_campaigns:
        return None

    active_adsets = await get_active_adsets(account_id, list(active_campaigns.keys()))
    if not active_adsets:
        return None

    adset_ids = [a["id"] for a in active_adsets]
    insights = await get_adset_insights(account_id, adset_ids)

    # Структурируем статистику для быстрого доступа
    spend_map = {row["adset_id"]: float(row.get("spend", 0)) for row in insights}
    chats_map = {
        row["adset_id"]: sum(
            int(a["value"]) for a in row.get("actions", [])
            if a["action_type"] == "onsite_conversion.messaging_conversation_started_7d"
        )
        for row in insights
    }
    
    # Собираем данные в единую структуру
    campaigns_data = {}
    for adset in active_adsets:
        campaign_id = adset["campaign_id"]
        campaign = active_campaigns[campaign_id]
        
        spend = spend_map.get(adset["id"], 0)
        leads = chats_map.get(adset["id"], 0)

        # Пропускаем адсеты без активности
        if spend == 0 and leads == 0:
            continue

        cpl = (spend / leads) if leads > 0 else 0
        adset_data = {
            "name": adset["name"],
            "objective": campaign.get("objective", ""),
            "cpl": cpl,
            "leads": leads,
            "spend": spend
        }
        
        # 👇 Здесь можно добавить логику для будущих уведомлений
        # if spend > 10 and leads == 0:
        #     adset_data['alert'] = "Большой расход без лидов!"
        # if cpl > 5:
        #     adset_data['alert'] = "Очень высокая цена за лид!"
        
        # Группируем адсеты по кампаниям
        if campaign_id not in campaigns_data:
            campaigns_data[campaign_id] = {"name": campaign["name"], "adsets": []}
        campaigns_data[campaign_id]["adsets"].append(adset_data)
        
    if not campaigns_data:
        return None
        
    return {
        "name": acc["name"],
        "campaigns": list(campaigns_data.values()),
        "active_count": len(campaigns_data)
    }

# ============================
# === Хендлеры (Handlers) ===
# ============================

@router.message(Command("start"))
async def start_handler(msg: Message):
    await send_and_store(msg, "👋 Привет! Я ваш бот для управления рекламой Meta. Выберите действие:", reply_markup=inline_main_menu())

# ... (хендлеры clear, restart, help можно оставить без изменений или вынести в отдельный файл)
@router.message(Command("clear"))
@router.callback_query(F.data == "clear_chat")
async def clear_chat_handler(event: Message | CallbackQuery):
    # Просто пример, как может выглядеть очистка
    # В реальном боте может понадобиться более сложная логика
    message = event if isinstance(event, Message) else event.message
    for msg_id in sent_messages:
        try:
            await bot.delete_message(message.chat.id, msg_id)
        except:
            pass # Игнорируем ошибки (сообщение уже удалено и т.д.)
    sent_messages.clear()
    await message.answer("Чат очищен.")


@router.message(Command("report"))
@router.callback_query(F.data == "build_report")
async def build_report_handler(event: Message | CallbackQuery):
    message = event if isinstance(event, Message) else event.message
    if isinstance(event, CallbackQuery):
        await event.answer("Начинаю сбор данных...")
    
    status_msg = await send_and_store(message, "⏳ Начинаю сбор данных...")

    accounts = await get_ad_accounts()
    if not accounts:
        await status_msg.edit_text("❌ Не найдено доступных рекламных аккаунтов.")
        return

    total = len(accounts)
    all_accounts_data = []

    for idx, acc in enumerate(accounts, start=1):
        await status_msg.edit_text(f"📦 Обрабатываю кабинет {idx}/{total}\n<b>{acc['name']}</b>")
        account_data = await process_account(acc)
        if account_data:
            all_accounts_data.append(account_data)
        await asyncio.sleep(0.1) # Небольшая задержка, чтобы не спамить API

    if not all_accounts_data:
        await status_msg.edit_text("❌ Нет активных кампаний с затратами или лидами.")
        return

    await status_msg.edit_text("📊 <b>Отчёт готов!</b>\nОтправляю данные...")
    await asyncio.sleep(1)
    await bot.delete_message(status_msg.chat.id, status_msg.message_id) # Удаляем статусное сообщение

    # Формируем и отправляем итоговый отчет
    for acc_data in all_accounts_data:
        msg_lines = [
            f"<b>🏢 Рекл. кабинет:</b> <u>{acc_data['name']}</u>",
            f"📈 Активных кампаний: {acc_data['active_count']}\n"
        ]
        for camp in acc_data["campaigns"]:
            msg_lines.append(f"🎯 <b>{camp['name']}</b>")
            for adset in camp["adsets"]:
                status_emoji = "🟢" if adset["leads"] > 0 else "🔴"
                msg_lines.append(
                    f"{status_emoji} <b>{adset['name']}</b>\n"
                    f"  Цель: {adset['objective']}\n"
                    f"  Лиды: {adset['leads']} | Расход: ${adset['spend']:.2f}\n"
                    f"  CPL: ${adset['cpl']:.2f} ({cpl_label(adset['cpl'])})"
                )
                # 👇 Вывод уведомления, если оно есть
                # if 'alert' in adset:
                #     msg_lines.append(f"  🚨 <b>Внимание:</b> {adset['alert']}")
            msg_lines.append("") # Пустая строка для разделения
        
        await send_and_store(message, "\n".join(msg_lines))
        await asyncio.sleep(0.5) # Задержка между отправкой сообщений

    await send_and_store(message, "✅ Отчёт завершён.", reply_markup=inline_main_menu())


# ============================
# === Точка входа в приложение ===
# ============================

async def on_shutdown():
    """Корректное завершение работы при остановке бота."""
    print("Бот останавливается...")
    await http_client.aclose() # 👈 Закрываем HTTP клиент
    print("Клиент HTTP закрыт.")

async def main():
    """Основная функция для запуска бота."""
    dp.include_router(router)
    dp.shutdown.register(on_shutdown) # 👈 Регистрируем функцию на выключение

    await set_bot_commands(bot)
    print("Бот запущен...")
    await dp.start_polling(bot)

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except (KeyboardInterrupt, SystemExit):
        print("Бот остановлен вручную.")

```
