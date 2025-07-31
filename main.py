import os
import asyncio
import aiohttp
import json
from datetime import datetime, timedelta
from aiogram import Bot, Dispatcher, Router, F
from aiogram.types import (Message, CallbackQuery, BotCommand, BotCommandScopeDefault,
                           ReplyKeyboardMarkup, KeyboardButton) # Добавлены нужные импорты
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
sent_messages_by_chat = {}

# ============================
# ===         API          ===
# ============================

async def fb_get(session: aiohttp.ClientSession, url: str, params: dict = None):
    params = params or {}
    params["access_token"] = META_TOKEN
    async with session.get(url, params=params) as response:
        response.raise_for_status()
        return await response.json()

async def get_ad_accounts(session: aiohttp.ClientSession):
    url = f"https://graph.facebook.com/{API_VERSION}/me/adaccounts"
    params = {"fields": "name,account_id"}
    data = await fb_get(session, url, params)
    return data.get("data", [])

async def get_campaigns(session: aiohttp.ClientSession, account_id: str):
    url = f"https://graph.facebook.com/{API_VERSION}/act_{account_id}/campaigns"
    params = {"fields": "id,name,status,objective", "limit": 500}
    data = await fb_get(session, url, params)
    return data.get("data", [])

async def get_all_adsets(session: aiohttp.ClientSession, account_id: str):
    url = f"https://graph.facebook.com/{API_VERSION}/act_{account_id}/adsets"
    params = {"fields": "id,name,campaign_id,status", "limit": 500}
    data = await fb_get(session, url, params)
    return data.get("data", [])

async def get_all_ads_with_creatives(session: aiohttp.ClientSession, account_id: str, active_adset_ids: list):
    url = f"https://graph.facebook.com/{API_VERSION}/act_{account_id}/ads"
    filtering = [
        {'field': 'adset.id', 'operator': 'IN', 'value': active_adset_ids},
        {'field': 'effective_status', 'operator': 'IN', 'value': ['ACTIVE']}
    ]
    params = {
        "fields": "id,name,adset_id,campaign_id,creative{thumbnail_url}",
        "filtering": json.dumps(filtering),
        "limit": 1000
    }
    data = await fb_get(session, url, params)
    return data.get("data", [])

async def get_ad_level_insights(session: aiohttp.ClientSession, account_id: str, ad_ids: list, date_preset: str, time_range_str: str = None):
    url = f"https://graph.facebook.com/{API_VERSION}/act_{account_id}/insights"
    params = {
        "fields": "ad_id,spend,actions,ctr",
        "level": "ad",
        "filtering": f'[{{"field":"ad.id","operator":"IN","value":{ad_ids}}}]',
        "limit": 1000
    }
    if time_range_str:
        params["time_range"] = time_range_str
    else:
        params["date_preset"] = date_preset
    data = await fb_get(session, url, params)
    return data.get("data", [])


# ============================
# ===       Помощники      ===
# ============================

# Эта функция больше не используется в данной версии, но оставлена на всякий случай
def cpl_label(value: float, metric: str) -> str:
    # ... (код функции без изменений)
    pass

# Эта функция также не используется напрямую, но может понадобиться для /clear
async def send_and_store(message: Message | CallbackQuery, text: str, *, is_persistent: bool = False, **kwargs):
    # ... (код функции без изменений)
    pass


# ============================
# ===         Меню         ===
# ============================

def main_reply_menu() -> ReplyKeyboardMarkup:
    """Создаёт ГЛАВНУЮ клавиатуру, которая заменяет обычную."""
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text="📊 Активные кампании"), KeyboardButton(text="📈 Дневной отчёт")]
        ],
        resize_keyboard=True,
        input_field_placeholder="Выберите тип отчета..."
    )

def inline_period_menu():
    """Создаёт инлайн-клавиатуру для выбора периода отчёта."""
    kb = InlineKeyboardBuilder()
    kb.button(text="Сегодня", callback_data="build_report:today")
    kb.button(text="Вчера", callback_data="build_report:yesterday")
    kb.button(text="За 7 дней", callback_data="build_report:last_7d")
    kb.adjust(3)
    return kb.as_markup()

# ============================
# ===       Хендлеры       ===
# ============================

@router.message(Command("start", "restart"))
async def start_handler(msg: Message):
    """Обработчик команды /start, показывает главное меню."""
    await msg.answer(
        "👋 Привет! Я твой бот для управления рекламой.\n\nВыберите действие:",
        reply_markup=main_reply_menu()
    )

# --- Хендлеры для Отчета по Активным Кампаниям ---
@router.message(F.text == "📊 Активные кампании")
async def active_campaigns_period_select(message: Message):
    """Показывает меню выбора периода для отчёта по активным кампаниям."""
    await message.answer("🗓️ Выберите период для отчёта:", reply_markup=inline_period_menu())
    
# --- Хендлер для Дневного Отчета (заглушка) ---
@router.message(F.text == "📈 Дневной отчёт")
async def daily_report_stub(message: Message):
    await message.answer("Функция 'Дневной отчёт' находится в разработке.")


# --- Основной хендлер для построения отчета "Активные кампании" ---
@router.callback_query(F.data.startswith("build_report:"))
async def build_report_handler(call: CallbackQuery):
    date_preset = call.data.split(":")[1]
    time_range_str = None
    
    await call.message.edit_text(f"⏳ Начинаю сбор данных за период: <b>{date_preset}</b>...")
    status_msg = await call.message.answer("Подключаюсь к API...")

    all_accounts_data = {}
    timeout = aiohttp.ClientTimeout(total=180)

    try:
        async with aiohttp.ClientSession(timeout=timeout) as session:
            accounts = await get_ad_accounts(session)
            if not accounts:
                await status_msg.edit_text("❌ Нет доступных рекламных аккаунтов.")
                return

            total = len(accounts)
            for idx, acc in enumerate(accounts, start=1):
                base_text = f"📦({idx}/{total}) Кабинет: <b>{acc['name']}</b>\n"
                await status_msg.edit_text(base_text + " Cкачиваю кампании и группы...")
                
                campaigns = await get_campaigns(session, acc["account_id"])
                campaigns_map = {c['id']: c for c in campaigns}
                
                adsets = await get_all_adsets(session, acc["account_id"])
                active_adsets = [a for a in adsets if a.get("status") == "ACTIVE"]
                if not active_adsets: continue
                
                adsets_map = {a['id']: a for a in active_adsets}
                active_adset_ids = list(adsets_map.keys())

                await status_msg.edit_text(base_text + " Cкачиваю объявления...")
                ads = await get_all_ads_with_creatives(session, acc["account_id"], active_adset_ids)
                if not ads: continue
                
                ad_ids = [ad['id'] for ad in ads]
                await status_msg.edit_text(base_text + f" Cкачиваю статистику для {len(ad_ids)} объявлений...")
                
                insights = await get_ad_level_insights(session, acc["account_id"], ad_ids, date_preset, time_range_str)
                insights_map = {row['ad_id']: row for row in insights}

                # ... (блок структурирования и форматирования данных остается таким же, как в вашем коде)
                account_data = {}
                for ad in ads:
                    stats = insights_map.get(ad['id'])
                    if not stats or float(stats.get('spend', 0)) == 0:
                        continue
                    
                    camp_id = ad.get('campaign_id')
                    adset_id = ad['adset_id']
                    campaign_obj = campaigns_map.get(camp_id)
                    if not campaign_obj: continue

                    if camp_id not in account_data:
                        account_data[camp_id] = {
                            "name": campaign_obj['name'], "objective": campaign_obj.get("objective", "N/A"),
                            "adsets": {}
                        }
                    if adset_id not in account_data[camp_id]['adsets']:
                         account_data[camp_id]['adsets'][adset_id] = {
                            "name": adsets_map[adset_id]['name'], "ads": []
                         }
                    account_data[camp_id]['adsets'][adset_id]['ads'].append({**ad, **stats})
                
                if account_data:
                    all_accounts_data[acc['name']] = account_data
        
        await bot.delete_message(call.message.chat.id, status_msg.message_id)

        if not all_accounts_data:
            await call.message.edit_text("✅ Активных кампаний с затратами за выбранный период не найдено.")
            return

        # --- Финальное форматирование и отправка ---
        final_report_lines = []
        for acc_name, campaigns_data in all_accounts_data.items():
            final_report_lines.append(f"<b>🏢 Рекламный кабинет: <u>{acc_name}</u></b>\n")
            for camp_data in campaigns_data.values():
                final_report_lines.append(f"<b>🎯 Кампания:</b> {camp_data['name']}")
                for adset_data in camp_data['adsets'].values():
                    final_report_lines.append(f"  <b>↳ Группа:</b> <code>{adset_data['name']}</code>")
                    for ad in adset_data['ads']:
                        spend = float(ad.get('spend', 0))
                        leads = sum(int(a["value"]) for a in ad.get("actions", []) if a.get("action_type") == LEAD_ACTION_TYPE)
                        clicks = sum(int(a["value"]) for a in ad.get("actions", []) if a.get("action_type") == LINK_CLICK_ACTION_TYPE)
                        cost_str = ""
                        if "TRAFFIC" in camp_data['objective'].upper() and clicks > 0:
                            cost_str = f"CPC: ${spend/clicks:.2f}"
                        elif leads > 0:
                            cost_str = f"CPL: ${spend/leads:.2f}"
                        
                        thumb_url = ad.get('creative', {}).get('thumbnail_url', '#')
                        final_report_lines.append(f'    <a href="{thumb_url}">🖼️</a> <b>{ad["name"]}</b> | Расход: ${spend:.2f} | {cost_str}')
        
        final_report = "\n".join(final_report_lines)
        if len(final_report) > 4096:
            for x in range(0, len(final_report), 4096):
                await call.message.answer(final_report[x:x+4096], disable_web_page_preview=True)
        else:
             await call.message.answer(final_report, disable_web_page_preview=True)
        
        await call.message.delete()

    except Exception as e:
        await call.message.answer(f"❌ Произошла ошибка: {e}")


# ============================
# ===         Запуск       ===
# ============================

async def main():
    dp.include_router(router)
    await bot.delete_webhook(drop_pending_updates=True)
    await dp.start_polling(bot)

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except (KeyboardInterrupt, SystemExit):
        print("Бот остановлен вручную.")
