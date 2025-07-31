import os
import asyncio
import aiohttp
import json
from datetime import datetime
from aiogram import Bot, Dispatcher, Router, F
from aiogram.types import Message, CallbackQuery, BotCommand, BotCommandScopeDefault
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

# --- Инициализация ---
bot = Bot(token=TELEGRAM_TOKEN, parse_mode="HTML")
dp = Dispatcher()
router = Router()

# ### ИЗМЕНЕНИЕ: Словарь для хранения ID сообщений и главного "пульта"
# Ключ - chat_id, значение - словарь с 'messages' и 'panel_id'
chat_sessions = {}

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

# ### ИЗМЕНЕНИЕ: Запрашиваем клики для расчета CPC
async def get_ad_level_insights(session: aiohttp.ClientSession, account_id: str, ad_ids: list):
    start_date = "2025-06-01"
    end_date = datetime.now().strftime("%Y-%m-%d")
    ad_ids_json_string = json.dumps(ad_ids)
    url = f"https://graph.facebook.com/{API_VERSION}/act_{account_id}/insights"
    params = {
        "fields": "ad_id,spend,actions,ctr,link_clicks", # Добавили link_clicks
        "level": "ad",
        "filtering": f'[{{"field":"ad.id","operator":"IN","value":{ad_ids_json_string}}}]',
        "time_range": f'{{"since":"{start_date}","until":"{end_date}"}}',
        "limit": 1000
    }
    data = await fb_get(session, url, params)
    return data.get("data", [])

# ============================
# ===      Помощники       ===
# ============================
def get_session(chat_id: int):
    """Получает или создает сессию для чата."""
    if chat_id not in chat_sessions:
        chat_sessions[chat_id] = {"messages": [], "panel_id": None}
    return chat_sessions[chat_id]

def metric_label(value: float) -> str:
    """Универсальная метка для CPL/CPC."""
    if value <= 1: return "🟢 Дешёвый"
    if value <= 3: return "🟡 Средний"
    return "🔴 Дорогой"

async def store_message_id(chat_id: int, message_id: int):
    """Сохраняет ID временного сообщения."""
    session = get_session(chat_id)
    session["messages"].append(message_id)

async def update_panel(chat_id: int, text: str, **kwargs):
    """Обновляет или создает главную панель управления."""
    session = get_session(chat_id)
    panel_id = session.get("panel_id")
    try:
        if panel_id:
            await bot.edit_message_text(text, chat_id, panel_id, **kwargs)
        else:
            msg = await bot.send_message(chat_id, text, **kwargs)
            session["panel_id"] = msg.message_id
    except TelegramBadRequest as e:
        if "message to edit not found" in e.message or "message is not modified" in e.message:
            # Если старую панель не нашли (удалили вручную), создаем новую
            msg = await bot.send_message(chat_id, text, **kwargs)
            session["panel_id"] = msg.message_id
        else:
            # Другие ошибки (например, неправильный HTML) пробрасываем дальше
            raise e

# ============================
# ===         Меню         ===
# ============================
async def set_bot_commands(bot: Bot):
    commands = [
        BotCommand(command="start", description="🚀 Показать пульт управления"),
        BotCommand(command="report", description="📊 Запросить отчёт"),
        BotCommand(command="clear", description="🧹 Очистить временные сообщения"),
    ]
    await bot.set_my_commands(commands, BotCommandScopeDefault())

def inline_main_menu():
    kb = InlineKeyboardBuilder()
    kb.button(text="📊 Отчёт: Активные кампании", callback_data="build_report")
    kb.button(text="🧹 Очистить временные сообщения", callback_data="clear_chat")
    return kb.as_markup()

# ============================
# ===       Хендлеры       ===
# ============================
@router.message(Command("start", "restart"))
async def start_handler(msg: Message):
    text = "👋 Привет! Я твой бот для управления рекламой.\n\nВыберите действие:"
    await update_panel(msg.chat.id, text, reply_markup=inline_main_menu())

@router.callback_query(F.data == "show_menu")
async def show_menu_handler(call: CallbackQuery):
    text = "👋 Привет! Я твой бот для управления рекламой.\n\nВыберите действие:"
    await update_panel(call.message.chat.id, text, reply_markup=inline_main_menu())
    await call.answer()

@router.message(Command("clear"))
@router.callback_query(F.data == "clear_chat")
async def clear_chat_handler(event: Message | CallbackQuery):
    chat_id = event.message.chat.id
    session = get_session(chat_id)
    messages_to_delete = session["messages"].copy()
    session["messages"] = []
    
    count = 0
    for msg_id in messages_to_delete:
        try:
            await bot.delete_message(chat_id, msg_id)
            count += 1
        except TelegramBadRequest:
            pass
    
    confirmation_text = f"✅ Готово! Удалил {count} временных сообщений."
    menu_text = "👋 Привет! Я твой бот для управления рекламой.\n\nВыберите действие:"
    await update_panel(chat_id, menu_text, reply_markup=inline_main_menu())
    
    if isinstance(event, CallbackQuery):
        await event.answer(confirmation_text, show_alert=True)
    else:
        # Отправляем и сразу сохраняем для удаления
        msg = await event.answer(confirmation_text)
        await asyncio.sleep(5)
        try:
            await bot.delete_message(chat_id, msg.message_id)
        except TelegramBadRequest:
            pass


# ============ Отчёт с лоадером ============
@router.message(Command("report"))
@router.callback_query(F.data == "build_report")
async def build_report(event: Message | CallbackQuery):
    chat_id = event.message.chat.id
    await update_panel(chat_id, "⏳ Начинаю сбор данных...")
    all_accounts_data = {}
    
    timeout = aiohttp.ClientTimeout(total=120)
    
    try:
        async with aiohttp.ClientSession(timeout=timeout) as session:
            accounts = await get_ad_accounts(session)
            if not accounts:
                await update_panel(chat_id, "❌ Нет доступных рекламных аккаунтов.", reply_markup=inline_main_menu())
                return

            total = len(accounts)
            for idx, acc in enumerate(accounts, start=1):
                base_text = f"📦({idx}/{total}) Кабинет: <b>{acc['name']}</b>\n"
                
                try:
                    await update_panel(chat_id, base_text + " Cкачиваю кампании и группы...")
                    campaigns = await get_campaigns(session, acc["account_id"])
                    campaigns_map = {c['id']: c for c in campaigns}
                    
                    adsets = await get_all_adsets(session, acc["account_id"])
                    active_adsets = [a for a in adsets if a.get("status") == "ACTIVE"]
                    if not active_adsets: continue
                    adsets_map = {a['id']: a for a in active_adsets}
                    active_adset_ids = list(adsets_map.keys())

                    await update_panel(chat_id, base_text + " Cкачиваю объявления...")
                    ads = await get_all_ads_with_creatives(session, acc["account_id"], active_adset_ids)
                    if not ads: continue
                    
                    ad_ids = [ad['id'] for ad in ads]
                    await update_panel(chat_id, base_text + f" Cкачиваю статистику для {len(ad_ids)} объявлений...")
                    insights = await get_ad_level_insights(session, acc["account_id"], ad_ids)
                    
                    insights_map = {}
                    for row in insights:
                        ad_id = row['ad_id']
                        spend = float(row.get("spend", 0))
                        leads = sum(int(a["value"]) for a in row.get("actions", []) if a.get("action_type") == LEAD_ACTION_TYPE)
                        clicks = int(row.get("link_clicks", 0))
                        ctr = float(row.get("ctr", 0))
                        insights_map[ad_id] = {"spend": spend, "leads": leads, "clicks": clicks, "ctr": ctr}

                    account_data = {}
                    for ad in ads:
                        ad_id, adset_id, campaign_id = ad['id'], ad['adset_id'], ad.get('campaign_id')
                        if adset_id not in adsets_map or campaign_id not in campaigns_map: continue
                        stats = insights_map.get(ad_id)
                        if not stats or (stats['spend'] == 0 and stats['leads'] == 0 and stats['clicks'] == 0): continue
                        
                        if campaign_id not in account_data:
                            campaign_obj = campaigns_map[campaign_id]
                            account_data[campaign_id] = {
                                "name": campaign_obj['name'],
                                "objective_raw": campaign_obj.get("objective", "N/A"),
                                "adsets": {}
                            }
                        
                        if adset_id not in account_data[campaign_id]['adsets']:
                            account_data[campaign_id]['adsets'][adset_id] = {"name": adsets_map[adset_id]['name'], "ads": []}
                        
                        ad_info = {
                            "name": ad['name'], "thumbnail_url": ad.get('creative', {}).get('thumbnail_url'),
                            "ctr": stats['ctr'], "leads": stats['leads'], "clicks": stats['clicks'], "spend": stats['spend']
                        }
                        account_data[campaign_id]['adsets'][adset_id]['ads'].append(ad_info)

                    if account_data: all_accounts_data[acc['name']] = account_data
                except asyncio.TimeoutError:
                    msg = await bot.send_message(chat_id, f"⚠️ <b>Превышен таймаут</b> при обработке кабинета <b>{acc['name']}</b>. Пропускаю его.")
                    await store_message_id(chat_id, msg.message_id)
                    continue
    
    except aiohttp.ClientResponseError as e:
        error_details = "Не удалось получить детали ошибки"
        if e.content_type == 'application/json':
            try: error_details = (await e.json()).get("error", {}).get("message", "Нет сообщения")
            except: pass
        else: error_details = e.reason
        await update_panel(chat_id, f"❌ <b>Ошибка API Facebook:</b>\nКод: {e.status}\nСообщение: {error_details}", reply_markup=inline_main_menu())
        return
    except Exception as e:
        await update_panel(chat_id, f"❌ <b>Произошла неизвестная ошибка:</b>\n{type(e).__name__}: {e}", reply_markup=inline_main_menu())
        return
        
    if not all_accounts_data:
        await update_panel(chat_id, "✅ Активных кампаний с затратами или лидами не найдено.", reply_markup=inline_main_menu())
        return
    
    # Отправляем отчеты в отдельных сообщениях
    for acc_name, campaigns_data in all_accounts_data.items():
        active_campaign_count = len(campaigns_data)
        msg_lines = [f"<b>🏢 Рекламный кабинет:</b> <u>{acc_name}</u>", f"<b>📈 Активных кампаний:</b> {active_campaign_count}", "─" * 20]
        
        for camp_id, camp_data in campaigns_data.items():
            is_traffic = 'TRAFFIC' in camp_data['objective_raw']
            objective_clean = camp_data['objective_raw'].replace('OUTCOME_', '').replace('_', ' ').capitalize()
            msg_lines.append(f"\n<b>🎯 Кампания:</b> {camp_data['name']}")
            
            for adset_id, adset_data in camp_data['adsets'].items():
                total_spend = sum(ad['spend'] for ad in adset_data['ads'])
                
                if is_traffic:
                    total_metric_val = sum(ad['clicks'] for ad in adset_data['ads'])
                    total_cost_per_action = (total_spend / total_metric_val) if total_metric_val > 0 else 0
                    metric_name = "Клики"
                    cost_name = "CPC"
                else:
                    total_metric_val = sum(ad['leads'] for ad in adset_data['ads'])
                    total_cost_per_action = (total_spend / total_metric_val) if total_metric_val > 0 else 0
                    metric_name = "Лиды"
                    cost_name = "CPL"

                adset_block = [
                    f"  <b>↳ Группа:</b> <code>{adset_data['name']}</code>",
                    f"    - <b>Цель:</b> {objective_clean}",
                    f"    - <b>{metric_name}:</b> {total_metric_val}",
                    f"    - <b>Расход:</b> ${total_spend:.2f}",
                    f"    - <b>{cost_name}:</b> ${total_cost_per_action:.2f} {metric_label(total_cost_per_action)}"
                ]
                msg_lines.extend(adset_block)
                
                if adset_data['ads']:
                    msg_lines.append("  <b>↳ Объявления:</b>")
                    for ad in sorted(adset_data['ads'], key=lambda x: x['spend'], reverse=True):
                        thumb_url = ad.get('thumbnail_url', '#')
                        
                        if is_traffic:
                            ad_cost_per_action = (ad['spend'] / ad['clicks']) if ad['clicks'] > 0 else 0
                            ad_cost_name = "CPC"
                        else:
                            ad_cost_per_action = (ad['spend'] / ad['leads']) if ad['leads'] > 0 else 0
                            ad_cost_name = "CPL"

                        ad_line = f'    <a href="{thumb_url}">🖼️</a> <b>{ad["name"]}</b> | {ad_cost_name}: ${ad_cost_per_action:.2f} | CTR: {ad["ctr"]:.2f}%'
                        msg_lines.append(ad_line)
        
        report_msg = await bot.send_message(chat_id, "\n".join(msg_lines), parse_mode="HTML", disable_web_page_preview=True)
        await store_message_id(chat_id, report_msg.message_id)

    await update_panel(chat_id, "✅ Отчёт завершён. Выберите следующее действие:", reply_markup=inline_main_menu())

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
    except KeyboardInterrupt:
        print("Бот остановлен вручную.")
