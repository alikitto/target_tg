import os
import asyncio
import aiohttp
from datetime import datetime, timedelta
from dotenv import load_dotenv

# --- Конфигурация и константы ---
load_dotenv() # Этот модуль сам загружает переменные из .env
API_VERSION = "v19.0"
META_TOKEN = os.getenv("META_ACCESS_TOKEN") # И сам получает токен
LEAD_ACTION_TYPE = "onsite_conversion.messaging_conversation_started_7d"
LINK_CLICK_ACTION_TYPE = "link_click"


# --- Функции API ---

async def fb_get(session: aiohttp.ClientSession, url: str, params: dict = None):
    params = params or {}
    params["access_token"] = META_TOKEN
    async with session.get(url, params=params) as response:
        response.raise_for_status()
        return await response.json()

async def get_ad_accounts(session: aiohttp.ClientSession):
    url = f"https://graph.facebook.com/{API_VERSION}/me/adaccounts"
    params = {"fields": "name,account_id"}
    data = await fb_get(session, url, params=params)
    return data.get("data", [])

async def get_campaign_objectives(session: aiohttp.ClientSession, account_id: str):
    url = f"https://graph.facebook.com/{API_VERSION}/act_{account_id}/campaigns"
    params = {"fields": "id,objective", "limit": 1000}
    data = await fb_get(session, url, params=params)
    return {campaign['id']: campaign.get('objective', 'N/A') for campaign in data.get("data", [])}

async def get_ad_level_insights_for_yesterday(session: aiohttp.ClientSession, account_id: str):
    url = f"https://graph.facebook.com/{API_VERSION}/act_{account_id}/insights"
    params = {
        "fields": "campaign_id,campaign_name,adset_id,adset_name,ad_id,ad_name,spend,actions,ctr,creative{thumbnail_url}",
        "level": "ad",
        "date_preset": "yesterday",
        "limit": 2000
    }
    data = await fb_get(session, url, params=params)
    return data.get("data", [])

# --- Функции обработки данных ---

def structure_insights(insights: list, objectives: dict):
    campaigns = {}
    for ad in insights:
        spend = float(ad.get("spend", 0))
        if spend == 0: continue
        camp_id, adset_id = ad.get('campaign_id'), ad.get('adset_id')
        if not all([camp_id, adset_id, camp_id in objectives]): continue

        if camp_id not in campaigns:
            campaigns[camp_id] = {"name": ad['campaign_name'], "objective": objectives.get(camp_id, 'N/A'), "adsets": {}}
        if adset_id not in campaigns[camp_id]['adsets']:
            campaigns[camp_id]['adsets'][adset_id] = {"name": ad['adset_name'], "ads": []}

        ad_data = {
            "name": ad['ad_name'],
            "spend": spend,
            "leads": sum(int(a["value"]) for a in ad.get("actions", []) if a.get("action_type") == LEAD_ACTION_TYPE),
            "clicks": sum(int(a["value"]) for a in ad.get("actions", []) if a.get("action_type") == LINK_CLICK_ACTION_TYPE),
            "ctr": float(ad.get('ctr', 0)),
            "thumbnail_url": ad.get('creative', {}).get('thumbnail_url', '#')
        }
        campaigns[camp_id]['adsets'][adset_id]['ads'].append(ad_data)
    return campaigns

def analyze_adsets_and_format(campaigns_data: dict):
    report_lines = []
    for camp_id, camp in campaigns_data.items():
        report_lines.append(f"<b>🎯 Кампания: {camp['name']}</b>")
        for adset_id, adset in camp['adsets'].items():
            total_spend = sum(ad['spend'] for ad in adset['ads'])
            total_leads = sum(ad['leads'] for ad in adset['ads'])
            total_clicks = sum(ad['clicks'] for ad in adset['ads'])

            cost_str = ""
            if "TRAFFIC" in camp['objective'].upper():
                cpc = (total_spend / total_clicks) if total_clicks > 0 else 0
                cost_str = f"Клики: {total_clicks} | CPC: ${cpc:.2f}"
            else:
                cpl = (total_spend / total_leads) if total_leads > 0 else 0
                cost_str = f"Лиды: {total_leads} | CPL: ${cpl:.2f}"
            
            report_lines.append(f"  <b>↳ Группа:</b> {adset['name']}")
            report_lines.append(f"    - {cost_str} | Расход: ${total_spend:.2f}")
    return "\n".join(report_lines)

# --- Главная функция модуля ---

async def generate_daily_report_text() -> str:
    """
    Главная функция, которая собирает данные и генерирует полный текст отчета.
    Не принимает аргументов, так как сама получает все необходимое.
    """
    timeout = aiohttp.ClientTimeout(total=240)
    async with aiohttp.ClientSession(timeout=timeout) as session:
        accounts = await get_ad_accounts(session)
        if not accounts:
            return "❌ Не найдено ни одного рекламного аккаунта."

        all_reports = []
        for acc in accounts:
            try:
                insights = await get_ad_level_insights_for_yesterday(session, acc['account_id'])
                if not insights: continue
                objectives = await get_campaign_objectives(session, acc['account_id'])
                campaigns_data = structure_insights(insights, objectives)
                if not campaigns_data: continue

                account_header = f"\n<b>🏢 Кабинет: <u>{acc['name']}</u></b>"
                account_body = analyze_adsets_and_format(campaigns_data)
                all_reports.append(account_header + "\n" + account_body)
            except Exception as e:
                print(f"Ошибка при обработке аккаунта {acc['name']}: {e}")
                continue

    if not all_reports:
        return "✅ За вчерашний день не было активности ни в одном из кабинетов."

    report_date_str = (datetime.now() - timedelta(days=1)).strftime('%d %B %Y')
    final_header = f"<b>📈 Дневная сводка за {report_date_str}</b>"
    return final_header + "\n" + "\n".join(all_reports)
