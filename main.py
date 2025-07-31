import os
import asyncio
import aiohttp
import json
from datetime import datetime, timedelta
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

# Возвращаемся к простому списку для хранения сообщений
sent_messages = []

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
    filtering = [{'field': 'adset.id', 'operator': 'IN', 'value': active_adset_ids}, {'field': 'effective_status', 'operator': 'IN', 'value': ['ACTIVE']}]
    params = {"fields": "id,name,adset_id,campaign_id,creative{thumbnail_url}", "filtering": json.dumps(filtering), "limit": 1000}
    data = await fb_get(session, url, params)
    return data.get("data", [])

async def get_ad_level_insights(session: aiohttp.ClientSession, account_id: str, ad_ids: list):
    start_date = "2025-06-01"
    end_date = datetime.now().strftime("%Y-%m-%d")
    ad_ids_json_string = json.dumps(ad_ids)
    url = f"https://graph.facebook.com/{API_VERSION}/act_{account_id}/insights"
    params = {
        "fields": "ad_id,spend,actions,ctr,link_clicks",
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
def metric_label(value: float) -> str:
    if value <= 1: return "🟢 Дешёвый"
    if value <= 3: return "🟡 Средний"
    return "🔴 Дорогой"

async def send_and_store(message: Message, text: str, **kwargs):
    kwargs.setdefault('disable_web_page_preview', True)
    msg = await message.answer(text, **kwargs)
    sent_messages.append(msg.message_id)
    return msg

# ============================
# ===         Меню         ===
# ============================
async def set_bot_commands(bot: Bot):
    commands = [BotCommand(command="start", description="🚀 Показать меню"), BotCommand(command="report", description="📊 Запросить отчёт"), BotCommand(command="clear", description="🧹 Очистить чат")]
    await bot.set_my_commands(commands, BotCommandScopeDefault())

def inline_main_menu():
    kb = InlineKeyboardBuilder()
    kb.button(text="📊 Отчёт: Активные кампании", callback_data="build_report")
    kb.button(text="🧹 Очистить чат", callback_data="clear_chat")
    return kb.as_markup()

# ============================
# ===       Хендлеры       ===
# ============================
@router.message(Command("start", "restart"))
async def start_handler(msg: Message):
    await send_and_store(msg, "👋 Привет! Я твой бот для управления рекламой.\n\nВыберите действие:", reply_markup=inline_main_menu())

@router.callback_query(F.data == "clear_chat")
async def clear_chat_handler(call: CallbackQuery):
    chat_id = call.message.chat.id
    count = 0
    for msg_id in sent_messages:
        try:
            await bot.delete_message(chat_id, msg_id)
            count += 1
        except TelegramBadRequest:
            pass
    sent_messages.clear()
    await call.message.answer(f"✅ Готово! Удалил {count} сообщений.")
    await call.answer()
    # После очистки снова покажем меню
    await start_handler(call.message)


@router.message(Command("report"))
@router.callback_query(F.data == "build_report")
async def build_report(event: Message | CallbackQuery):
    message = event if isinstance(event, Message) else event.message
    status_msg = await message.answer("⏳ Начинаю сбор данных...")
    sent_messages.append(status_msg.message_id)
    
    all_accounts_data = {}
    timeout = aiohttp.ClientTimeout(total=300)
    
    try:
        async with aiohttp.ClientSession(timeout=timeout) as session:
            accounts = await get_ad_accounts(session)
            if not accounts:
                await status_msg.edit_text("❌ Нет доступных рекламных аккаунтов.")
                return

            total = len(accounts)
            for idx, acc in enumerate(accounts, start=1):
                base_text = f"📦({idx}/{total}) Кабинет: <b>{acc['name']}</b>\n"
                
                try:
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
                    insights = await get_ad_level_insights(session, acc["account_id"], ad_ids)
                    
                    insights_map = {}
                    for row in insights:
                        ad_id = row['ad_id']
                        spend = float(row.get("spend", 0))
                        if spend == 0: continue # Пропускаем объявления без трат
                        
                        insights_map[ad_id] = {
                            "spend": spend,
                            "leads": sum(int(a["value"]) for a in row.get("actions", []) if a.get("action_type") == LEAD_ACTION_TYPE),
                            "clicks": int(row.get("link_clicks", 0)),
                            "ctr": float(row.get("ctr", 0))
                        }

                    account_data = {}
                    for ad in ads:
                        ad_id, adset_id, campaign_id = ad['id'], ad['adset_id'], ad.get('campaign_id')
                        if ad_id not in insights_map or adset_id not in adsets_map or campaign_id not in campaigns_map: continue
                        
                        stats = insights_map[ad_id]
                        
                        if campaign_id not in account_data:
                            campaign_obj = campaigns_map[campaign_id]
                            account_data[campaign_id] = {"name": campaign_obj['name'], "objective_raw": campaign_obj.get("objective", "N/A"), "adsets": {}}
                        
                        if adset_id not in account_data[campaign_id]['adsets']:
                            account_data[campaign_id]['adsets'][adset_id] = {"name": adsets_map[adset_id]['name'], "ads": []}
                        
                        ad_info = {"name": ad['name'], "thumbnail_url": ad.get('creative', {}).get('thumbnail_url'), **stats}
                        account_data[campaign_id]['adsets'][adset_id]['ads'].append(ad_info)

                    if account_data: all_accounts_data[acc['name']] = account_data
                except asyncio.TimeoutError:
                    await send_and_store(message, f"⚠️ <b>Превышен таймаут</b> при обработке кабинета <b>{acc['name']}</b>. Пропускаю его.")
                    continue
    
    except aiohttp.ClientResponseError as e:
        error_details = "Не удалось получить детали ошибки"
        if e.content_type == 'application/json':
            try: error_details = (await e.json()).get("error", {}).get("message", "Нет сообщения")
            except: pass
        else: error_details = e.reason
        await status_msg.edit_text(f"❌ <b>Ошибка API Facebook:</b>\nКод: {e.status}\nСообщение: {error_details}")
        return
    except Exception as e:
        await status_msg.edit_text(f"❌ <b>Произошла неизвестная ошибка:</b>\n{type(e).__name__}: {e}")
        return
        
    if not all_accounts_data:
        await status_msg.edit_text("✅ Активных кампаний с затратами или лидами не найдено.")
        return
    
    await status_msg.edit_text("✅ Отчет готов, отправляю...")

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
                    metric_name, cost_name = "Клики", "CPC"
                else:
                    total_metric_val = sum(ad['leads'] for ad in adset_data['ads'])
                    total_cost_per_action = (total_spend / total_metric_val) if total_metric_val > 0 else 0
                    metric_name, cost_name = "Лиды", "CPL"

                msg_lines.extend([f"  <b>↳ Группа:</b> <code>{adset_data['name']}</code>", f"    - <b>Цель:</b> {objective_clean}", f"    - <b>{metric_name}:</b> {total_metric_val}", f"    - <b>Расход:</b> ${total_spend:.2f}", f"    - <b>{cost_name}:</b> ${total_cost_per_action:.2f} {metric_label(total_cost_per_action)}"])
                
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
                        msg_lines.append(f'    <a href="{thumb_url}">🖼️</a> <b>{ad["name"]}</b> | {ad_cost_name}: ${ad_cost_per_action:.2f} | CTR: {ad["ctr"]:.2f}%')
        
        await send_and_store(message, "\n".join(msg_lines))

    await send_and_store(message, "✅ Отчёт завершён.", reply_markup=inline_main_menu())

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
