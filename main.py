import os
import asyncio
import aiohttp
import json
from datetime import datetime, timedelta
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
    response = await session.get(url, params=params)
    response.raise_for_status()
    return await response.json()

async def get_ad_accounts(session: aiohttp.ClientSession):
    url = f"https://graph.facebook.com/{API_VERSION}/me/adaccounts"
    params = {"fields": "name,account_id"}
    return (await fb_get(session, url, params)).get("data", [])

# --- Функции для Отчета по Активным Кампаниям ---
async def get_campaigns(session: aiohttp.ClientSession, account_id: str):
    url = f"https://graph.facebook.com/{API_VERSION}/act_{account_id}/campaigns"
    params = {"fields": "id,name,status,objective", "limit": 500}
    return (await fb_get(session, url, params)).get("data", [])

async def get_all_adsets(session: aiohttp.ClientSession, account_id: str):
    url = f"https://graph.facebook.com/{API_VERSION}/act_{account_id}/adsets"
    params = {"fields": "id,name,campaign_id,status", "limit": 500}
    return (await fb_get(session, url, params)).get("data", [])

async def get_all_ads_with_creatives(session: aiohttp.ClientSession, account_id: str, adset_ids: list):
    url = f"https://graph.facebook.com/{API_VERSION}/act_{account_id}/ads"
    params = {
        "fields": "id,name,adset_id,campaign_id,creative{thumbnail_url}",
        "filtering": json.dumps([{'field': 'adset.id', 'operator': 'IN', 'value': adset_ids},
                                  {'field': 'effective_status', 'operator': 'IN', 'value': ['ACTIVE']}]),
        "limit": 1000
    }
    return (await fb_get(session, url, params)).get("data", [])

async def get_ad_level_insights(session: aiohttp.ClientSession, account_id: str, ad_ids: list, date_preset: str):
    url = f"https://graph.facebook.com/{API_VERSION}/act_{account_id}/insights"
    params = {
        "fields": "ad_id,spend,actions,ctr", "level": "ad",
        "filtering": f'[{{"field":"ad.id","operator":"IN","value":{json.dumps(ad_ids)}}}]',
        "date_preset": date_preset, "limit": 1000
    }
    return (await fb_get(session, url, params)).get("data", [])

# --- Функции для Дневного Отчета ---
async def get_daily_campaign_objectives(session: aiohttp.ClientSession, account_id: str):
    url = f"https://graph.facebook.com/{API_VERSION}/act_{account_id}/campaigns"
    params = {"fields": "id,objective", "limit": 1000}
    data = await fb_get(session, url, params)
    return {c['id']: c.get('objective', 'N/A') for c in data.get("data", [])}

async def get_daily_ad_level_insights(session: aiohttp.ClientSession, account_id: str):
    url = f"https://graph.facebook.com/{API_VERSION}/act_{account_id}/insights"
    params = {
        "fields": "campaign_id,campaign_name,adset_id,adset_name,ad_id,ad_name,spend,actions,ctr,creative{thumbnail_url}",
        "level": "ad", "date_preset": "yesterday", "limit": 2000
    }
    return (await fb_get(session, url, params)).get("data", [])

# ============================
# ===       Меню           ===
# ============================
def main_reply_menu() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text="📊 Активные кампании"), KeyboardButton(text="📈 Дневной отчёт")]
        ],
        resize_keyboard=True, input_field_placeholder="Выберите тип отчета..."
    )

def inline_period_menu():
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
    await msg.answer("Бот готов к работе. Выберите действие:", reply_markup=main_reply_menu())

# --- Хендлер для Дневного Отчета ---
@router.message(F.text == "📈 Дневной отчёт")
async def daily_report_handler(message: Message):
    status_msg = await message.answer("⏳ Собираю дневную сводку, это может занять до минуты...")
    try:
        timeout = aiohttp.ClientTimeout(total=240)
        async with aiohttp.ClientSession(timeout=timeout) as session:
            accounts = await get_ad_accounts(session)
            if not accounts:
                await status_msg.edit_text("❌ Не найдено рекламных аккаунтов.")
                return
            
            report_date_str = (datetime.now() - timedelta(days=1)).strftime('%d %B %Y')
            final_report_lines = [f"<b>📈 Дневная сводка за {report_date_str}</b>"]
            
            tasks = [process_daily_account(session, acc) for acc in accounts]
            account_results = await asyncio.gather(*tasks, return_exceptions=True)

            active_reports = 0
            for result in account_results:
                if isinstance(result, str):
                    final_report_lines.append(result)
                    active_reports += 1
            
            if active_reports == 0:
                final_text = "✅ За вчерашний день не было активности ни в одном из кабинетов."
            else:
                final_text = "\n".join(final_report_lines)

        await bot.delete_message(message.chat.id, status_msg.message_id)
        if len(final_text) > 4096:
            for x in range(0, len(final_text), 4096):
                await message.answer(final_text[x:x+4096], disable_web_page_preview=True)
        else:
            await message.answer(final_text, disable_web_page_preview=True)
            
    except Exception as e:
        await status_msg.edit_text(f"❌ Произошла ошибка при создании дневного отчёта:\n`{type(e).__name__}: {e}`")

async def process_daily_account(session, acc):
    objectives = await get_daily_campaign_objectives(session, acc["account_id"])
    insights = await get_daily_ad_level_insights(session, acc["account_id"])
    if not insights: return None

    campaigns = {}
    for ad in insights:
        spend = float(ad.get("spend", 0)); camp_id = ad['campaign_id']; adset_id = ad['adset_id']
        if spend == 0 or camp_id not in objectives: continue
        if camp_id not in campaigns: campaigns[camp_id] = {"name": ad['campaign_name'], "objective": objectives.get(camp_id, 'N/A'), "adsets": {}}
        if adset_id not in campaigns[camp_id]['adsets']: campaigns[camp_id]['adsets'][adset_id] = {"name": ad['adset_name'], "ads": []}
        leads = sum(int(a["value"]) for a in ad.get("actions", []) if a.get("action_type") == LEAD_ACTION_TYPE)
        clicks = sum(int(a["value"]) for a in ad.get("actions", []) if a.get("action_type") == LINK_CLICK_ACTION_TYPE)
        campaigns[camp_id]['adsets'][adset_id]['ads'].append({"name": ad['ad_name'], "spend": spend, "leads": leads, "clicks": clicks, "ctr": float(ad.get('ctr', 0)), "thumbnail_url": ad.get('creative', {}).get('thumbnail_url', '#')})

    if not campaigns: return None

    analyzed_adsets = []
    for camp in campaigns.values():
        for adset in camp['adsets'].values():
            total_spend = sum(ad['spend'] for ad in adset['ads']); total_leads = sum(ad['leads'] for ad in adset['ads']); total_clicks = sum(ad['clicks'] for ad in adset['ads'])
            cost, cost_type = float('inf'), 'CPL'
            if "TRAFFIC" in camp['objective'].upper():
                cost_type = 'CPC'
                if total_clicks > 0: cost = total_spend / total_clicks
            elif total_leads > 0: cost = total_spend / total_leads
            analyzed_adsets.append({"id": adset['name'], "name": adset['name'], "campaign_name": camp['name'], "spend": total_spend, "cost": cost, "cost_type": cost_type, "ads": adset['ads']})
    
    total_spend = sum(adset['spend'] for adset in analyzed_adsets)
    if total_spend == 0: return None

    total_leads = sum(sum(ad['leads'] for ad in adset['ads']) for adset in analyzed_adsets)
    total_clicks = sum(sum(ad['clicks'] for ad in adset['ads']) for adset in analyzed_adsets)
    report_lines = ["─" * 20, f"<b>🏢 Кабинет: <u>{acc['name']}</u></b>", f"`Расход: ${total_spend:.2f}`"]
    cost_str = ""
    if total_leads > 0: cost_str += f"Лиды: {total_leads} | Ср. CPL: ${total_spend/total_leads:.2f}"
    if total_clicks > 0:
        if cost_str: cost_str += " | "
        cost_str += f"Клики: {total_clicks} | Ср. CPC: ${total_spend/total_clicks:.2f}"
    if cost_str: report_lines.append(f"`{cost_str}`")
    
    adsets_with_cost = sorted([a for a in analyzed_adsets if a['cost'] != float('inf')], key=lambda x: x['cost'])
    if not adsets_with_cost: return "\n".join(report_lines)
    best_adset = adsets_with_cost[0]
    worst_adset = adsets_with_cost[-1] if len(adsets_with_cost) > 1 else None
    
    def format_ad_list(ads, cost_type):
        lines = []
        for ad in sorted(ads, key=lambda x: x['spend'], reverse=True):
            cost_str = f"{cost_type}: $0.00"
            if cost_type == 'CPL' and ad['leads'] > 0: cost_str = f"CPL: ${ad['spend']/ad['leads']:.2f}"
            elif cost_type == 'CPC' and ad['clicks'] > 0: cost_str = f"CPC: ${ad['spend']/ad['clicks']:.2f}"
            lines.append(f'    <a href="{ad["thumbnail_url"]}">▫️</a> <b>{ad["name"]}</b> | {cost_str} | CTR: {ad["ctr"]:.2f}%')
        return lines

    report_lines.extend(["\n" + f"<b>Лучшая группа:</b> {best_adset['name']} ({best_adset['campaign_name']})", f"  - Расход: ${best_adset['spend']:.2f} | {best_adset['cost_type']}: ${best_adset['cost']:.2f}", *format_ad_list(best_adset['ads'], best_adset['cost_type'])])
    if worst_adset and worst_adset['id'] != best_adset['id']:
        report_lines.extend(["\n" + f"<b>Худшая группа:</b> {worst_adset['name']} ({worst_adset['campaign_name']})", f"  - Расход: ${worst_adset['spend']:.2f} | {worst_adset['cost_type']}: ${worst_adset['cost']:.2f}", *format_ad_list(worst_adset['ads'], worst_adset['cost_type'])])
        
    return "\n".join(report_lines)


# --- Хендлеры для Отчета по Активным Кампаниям ---
@router.message(F.text == "📊 Активные кампании")
async def active_campaigns_period_select(message: Message):
    await message.answer("🗓️ Выберите период для отчёта:", reply_markup=inline_period_menu())

@router.callback_query(F.data.startswith("build_report:"))
async def build_report_handler(call: CallbackQuery):
    date_preset = call.data.split(":")[1]
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

            for idx, acc in enumerate(accounts, start=1):
                base_text = f"📦({idx}/{len(accounts)}) Кабинет: <b>{acc['name']}</b>\n"
                await status_msg.edit_text(base_text + " Cкачиваю кампании и группы...")
                
                campaigns = await get_campaigns(session, acc["account_id"])
                campaigns_map = {c['id']: c for c in campaigns}
                
                adsets = await get_all_adsets(session, acc["account_id"])
                active_adsets = [a for a in adsets if a.get("status") == "ACTIVE"]
                if not active_adsets: continue
                
                adsets_map = {a['id']: a for a in active_adsets}
                ad_ids = []
                ads_data = []

                await status_msg.edit_text(base_text + " Cкачиваю объявления...")
                if adsets_map:
                    ads_list = await get_all_ads_with_creatives(session, acc["account_id"], list(adsets_map.keys()))
                    if ads_list:
                        ad_ids = [ad['id'] for ad in ads_list]
                        ads_data = ads_list

                if not ad_ids: continue

                await status_msg.edit_text(base_text + f" Cкачиваю статистику для {len(ad_ids)} объявлений...")
                insights = await get_ad_level_insights(session, acc["account_id"], ad_ids, date_preset)
                insights_map = {row['ad_id']: row for row in insights}

                account_data = {}
                for ad in ads_data:
                    stats = insights_map.get(ad['id'])
                    if not stats or float(stats.get('spend', 0)) == 0: continue
                    
                    camp_id, adset_id = ad.get('campaign_id'), ad.get('adset_id')
                    campaign_obj = campaigns_map.get(camp_id)
                    if not campaign_obj: continue

                    if camp_id not in account_data:
                        account_data[camp_id] = {"name": campaign_obj['name'], "objective": campaign_obj.get("objective", "N/A"), "adsets": {}}
                    if adset_id not in account_data[camp_id]['adsets']:
                         account_data[camp_id]['adsets'][adset_id] = {"name": adsets_map[adset_id]['name'], "ads": []}
                    account_data[camp_id]['adsets'][adset_id]['ads'].append({**ad, **stats})
                
                if account_data:
                    all_accounts_data[acc['name']] = account_data
        
        await bot.delete_message(call.message.chat.id, status_msg.message_id)

        if not all_accounts_data:
            await call.message.edit_text("✅ Активных кампаний с затратами за выбранный период не найдено.")
            return

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
                        if "TRAFFIC" in camp_data['objective'].upper() and clicks > 0: cost_str = f"CPC: ${spend/clicks:.2f}"
                        elif leads > 0: cost_str = f"CPL: ${spend/leads:.2f}"
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
        await call.message.answer(f"❌ Произошла ошибка: {type(e).__name__} - {e}")

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
