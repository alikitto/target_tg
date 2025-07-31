import os
import asyncio
import requests
from datetime import date
from collections import defaultdict
from aiogram import Bot, Dispatcher, Router
from aiogram.types import Message, CallbackQuery
from aiogram.filters import Command
from aiogram.utils.keyboard import InlineKeyboardBuilder
from dotenv import load_dotenv
import time

load_dotenv()
TELEGRAM_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
META_TOKEN = os.getenv("META_ACCESS_TOKEN")

bot = Bot(token=TELEGRAM_TOKEN)
dp = Dispatcher()
router = Router()

def fb_get(url, params=None):
    if not params:
        params = {}
    params["access_token"] = META_TOKEN
    r = requests.get(url, params=params)
    r.raise_for_status()
    return r.json()

def get_active_adsets(ad_account_id, start_date, end_date):
    # Insights по активным adset
    active_filter = '[{"field":"adset.effective_status","operator":"IN","value":["ACTIVE"]}]'
    url = f"https://graph.facebook.com/v19.0/{ad_account_id}/insights"
    params = {
        "fields": "campaign_id,campaign_name,adset_id,adset_name,spend,actions,ctr,cpm,impressions,frequency",
        "level": "adset",
        "filtering": active_filter,
        "time_range": f'{{"since":"{start_date}","until":"{end_date}"}}',
        "limit": 500
    }
    data = fb_get(url, params).get("data", [])
    if not data:
        return {}
    result = {}
    for item in data:
        chats = 0
        for a in item.get("actions", []):
            if a["action_type"] == "onsite_conversion.messaging_conversation_started_7d":
                chats += int(a["value"])
        result[item["adset_id"]] = {
            "campaign_id": item["campaign_id"],
            "campaign_name": item["campaign_name"],
            "adset_name": item["adset_name"],
            "totalSpend": float(item.get("spend", 0)),
            "totalChats": chats,
            "totalImpressions": int(item.get("impressions", 0)),
            "avgCtr": float(item.get("ctr", 0)),
            "avgCpm": float(item.get("cpm", 0)),
            "frequency": float(item.get("frequency", 0)),
            "placements": {"feed": {"spend": 0, "chats": 0}, "reels": {"spend": 0, "chats": 0}, "stories": {"spend": 0, "chats": 0}},
            "ageBrackets": {k: {"spend": 0, "chats": 0} for k in ["18-24","25-34","35-44","45-54","55-64","65+"]}
        }
    return result

def fill_placements(ad_account_id, adset_ids, start_date, end_date, aggregatedData):
    adsetFilter = f'[{{"field":"adset.id","operator":"IN","value":{adset_ids}}}]'
    url = f"https://graph.facebook.com/v19.0/{ad_account_id}/insights"
    params = {
        "fields": "adset_id,spend,actions,platform_position",
        "level": "ad",
        "breakdowns": "publisher_platform,platform_position",
        "filtering": adsetFilter,
        "time_range": f'{{"since":"{start_date}","until":"{end_date}"}}',
        "limit": 1000
    }
    for item in fb_get(url, params).get("data", []):
        adsetId = item["adset_id"]
        if adsetId not in aggregatedData:
            continue
        spend = float(item.get("spend", 0))
        chats = 0
        for a in item.get("actions", []):
            if a["action_type"] == "onsite_conversion.messaging_conversation_started_7d":
                chats += int(a["value"])
        pos = item.get("platform_position", "").lower()
        if "feed" in pos or "marketplace" in pos:
            aggregatedData[adsetId]["placements"]["feed"]["spend"] += spend
            aggregatedData[adsetId]["placements"]["feed"]["chats"] += chats
        if "reels" in pos:
            aggregatedData[adsetId]["placements"]["reels"]["spend"] += spend
            aggregatedData[adsetId]["placements"]["reels"]["chats"] += chats
        if "story" in pos:
            aggregatedData[adsetId]["placements"]["stories"]["spend"] += spend
            aggregatedData[adsetId]["placements"]["stories"]["chats"] += chats

def fill_ages(ad_account_id, adset_ids, start_date, end_date, aggregatedData):
    adsetFilter = f'[{{"field":"adset.id","operator":"IN","value":{adset_ids}}}]'
    url = f"https://graph.facebook.com/v19.0/{ad_account_id}/insights"
    params = {
        "fields": "adset_id,spend,actions,age",
        "level": "ad",
        "breakdowns": "age",
        "filtering": adsetFilter,
        "time_range": f'{{"since":"{start_date}","until":"{end_date}"}}',
        "limit": 1000
    }
    for item in fb_get(url, params).get("data", []):
        adsetId = item["adset_id"]
        age = item.get("age")
        if adsetId not in aggregatedData or age not in aggregatedData[adsetId]["ageBrackets"]:
            continue
        spend = float(item.get("spend", 0))
        chats = 0
        for a in item.get("actions", []):
            if a["action_type"] == "onsite_conversion.messaging_conversation_started_7d":
                chats += int(a["value"])
        aggregatedData[adsetId]["ageBrackets"][age]["spend"] += spend
        aggregatedData[adsetId]["ageBrackets"][age]["chats"] += chats

def get_campaign_objectives(campaign_ids):
    objectives = {}
    for cid in campaign_ids:
        url = f"https://graph.facebook.com/v19.0/{cid}"
        data = fb_get(url, {"fields": "objective"})
        objectives[cid] = data.get("objective", "").replace("OUTCOME_", "").upper()
        time.sleep(0.2)
    return objectives

def get_adset_statuses(adset_ids):
    statuses = {}
    for aid in adset_ids:
        url = f"https://graph.facebook.com/v19.0/{aid}"
        data = fb_get(url, {"fields": "status"})
        statuses[aid] = data.get("status", "ERR")
        time.sleep(0.2)
    return statuses

def get_ad_accounts():
    url = "https://graph.facebook.com/v19.0/me/adaccounts"
    data = fb_get(url, {"fields": "name,account_id"})
    return data.get("data", [])

@router.message(Command("start"))
async def start_handler(msg: Message):
    kb = InlineKeyboardBuilder()
    kb.button(text="Собрать отчёт", callback_data="build_report")
    await msg.answer("Привет! Нажми кнопку, чтобы собрать отчёт по активным кампаниям.", reply_markup=kb.as_markup())

@router.callback_query(lambda c: c.data == "build_report")
async def build_report(callback: CallbackQuery):
    start_date = (date.today()).strftime("%Y-%m-%d")
    end_date = (date.today()).strftime("%Y-%m-%d")
    accounts = get_ad_accounts()
    if not accounts:
        await callback.message.answer("Нет рекламных аккаунтов.")
        return

    for acc in accounts:
        aggregatedData = get_active_adsets(acc["account_id"], start_date, end_date)
        if not aggregatedData:
            await callback.message.answer(f"--- {acc['name']} --- Нет активных кампаний.")
            continue

        adset_ids = list(aggregatedData.keys())
        fill_placements(acc["account_id"], adset_ids, start_date, end_date, aggregatedData)
        fill_ages(acc["account_id"], adset_ids, start_date, end_date, aggregatedData)

        campaign_ids = [aggregatedData[x]["campaign_id"] for x in adset_ids]
        campaign_objectives = get_campaign_objectives(set(campaign_ids))
        adset_statuses = get_adset_statuses(adset_ids)

        report_lines = [f"--- {acc['name']} ---"]
        for adsetId, stats in aggregatedData.items():
            cpl = stats["totalSpend"] / stats["totalChats"] if stats["totalChats"] > 0 else 0
            report_lines.append(
                f"{stats['campaign_name']} | {stats['adset_name']} | "
                f"Статус: {adset_statuses.get(adsetId,'')} | "
                f"Цель: {campaign_objectives.get(stats['campaign_id'],'')} | "
                f"Лидов: {stats['totalChats']} | CPL: ${cpl:.2f} | "
                f"Расход: ${stats['totalSpend']:.2f}"
            )
        await callback.message.answer("\n".join(report_lines))
        time.sleep(1)

    await callback.answer()

dp.include_router(router)

async def main():
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())
