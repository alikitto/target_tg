import asyncio
import aiohttp
from datetime import datetime, timedelta
import json

# Константы для API
API_VERSION = "v19.0"
LEAD_ACTION_TYPE = "onsite_conversion.messaging_conversation_started_7d"
LINK_CLICK_ACTION_TYPE = "link_click"

async def fb_get(session: aiohttp.ClientSession, url: str, params: dict = None, access_token: str = None):
    """Асинхронная функция для выполнения GET-запросов к Graph API."""
    params = params or {}
    params["access_token"] = access_token
    async with session.get(url, params=params) as response:
        response.raise_for_status()
        return await response.json()

# НОВАЯ функция для получения целей кампаний
async def get_campaign_objectives(session: aiohttp.ClientSession, account_id: str, access_token: str):
    """Получает словарь {id_кампании: цель_кампании}."""
    url = f"https://graph.facebook.com/{API_VERSION}/act_{account_id}/campaigns"
    params = {"fields": "id,objective", "limit": 1000}
    data = await fb_get(session, url, params=params, access_token=access_token)
    return {campaign['id']: campaign.get('objective', 'N/A') for campaign in data.get("data", [])}

# ИСПРАВЛЕННАЯ функция получения инсайтов (удалено поле objective)
async def get_ad_level_insights_for_date(session: aiohttp.ClientSession, account_id: str, date_str: str, access_token: str):
    """Получает детализированную статистику на уровне объявлений за конкретную дату."""
    url = f"https://graph.facebook.com/{API_VERSION}/act_{account_id}/insights"
    params = {
        "fields": "campaign_id,campaign_name,adset_id,adset_name,ad_id,ad_name,spend,actions,ctr,creative{thumbnail_url}",
        "level": "ad",
        "time_range": json.dumps({"since": date_str, "until": date_str}),
        "limit": 1000
    }
    data = await fb_get(session, url, params=params, access_token=access_token)
    return data.get("data", [])

def structure_insights(insights: list, objectives: dict):
    """Структурирует плоский список инсайтов в иерархию Кампания -> Группа -> Объявления."""
    campaigns = {}
    for ad in insights:
        spend = float(ad.get("spend", 0))
        if spend == 0:
            continue

        camp_id = ad['campaign_id']
        adset_id = ad['adset_id']
        
        # Пропускаем, если по какой-то причине нет цели для кампании
        if camp_id not in objectives:
            continue

        if camp_id not in campaigns:
            campaigns[camp_id] = {
                "name": ad['campaign_name'],
                "objective": objectives[camp_id], # Берем цель из нового словаря
                "adsets": {}
            }

        if adset_id not in campaigns[camp_id]['adsets']:
            campaigns[camp_id]['adsets'][adset_id] = {
                "name": ad['adset_name'],
                "ads": []
            }

        leads = sum(int(a["value"]) for a in ad.get("actions", []) if a.get("action_type") == LEAD_ACTION_TYPE)
        clicks = sum(int(a["value"]) for a in ad.get("actions", []) if a.get("action_type") == LINK_CLICK_ACTION_TYPE)
        
        ad_data = {
            "name": ad['ad_name'],
            "spend": spend,
            "leads": leads,
            "clicks": clicks,
            "ctr": float(ad.get('ctr', 0)),
            "thumbnail_url": ad.get('creative', {}).get('thumbnail_url', '#')
        }
        campaigns[camp_id]['adsets'][adset_id]['ads'].append(ad_data)
        
    return campaigns

def analyze_adsets(campaigns_data: dict):
    """Анализирует группы объявлений, считая их общую статистику и стоимость."""
    analyzed_adsets = []
    for camp_id, camp in campaigns_data.items():
        for adset_id, adset in camp['adsets'].items():
            total_spend = sum(ad['spend'] for ad in adset['ads'])
            total_leads = sum(ad['leads'] for ad in adset['ads'])
            total_clicks = sum(ad['clicks'] for ad in adset['ads'])
            
            cost = float('inf')
            cost_type = 'CPL'
            # Приоритет на лиды, если цель не трафик
            if "TRAFFIC" not in camp['objective'].upper() and total_leads > 0:
                cost = total_spend / total_leads
            elif total_clicks > 0:
                cost = total_spend / total_clicks
                cost_type = 'CPC'

            analyzed_adsets.append({
                "id": adset_id,
                "name": adset['name'],
                "campaign_name": camp['name'],
                "spend": total_spend,
                "cost": cost,
                "cost_type": cost_type,
                "ads": adset['ads']
            })
    return analyzed_adsets

def format_ad_list(ads: list, cost_type: str):
    """Форматирует список объявлений для вывода в отчет."""
    lines = []
    for ad in ads:
        if cost_type == 'CPL':
            cost = (ad['spend'] / ad['leads']) if ad['leads'] > 0 else 0
            cost_str = f"CPL: ${cost:.2f}"
        else:
            cost = (ad['spend'] / ad['clicks']) if ad['clicks'] > 0 else 0
            cost_str = f"CPC: ${cost:.2f}"
            
        lines.append(f'    <a href="{ad["thumbnail_url"]}">▫️</a> <b>{ad["name"]}</b> | {cost_str} | CTR: {ad["ctr"]:.2f}%')
    return lines

async def generate_daily_report_text(accounts: list, meta_token: str):
    """Основная функция, которая собирает, анализирует и генерирует текст отчета."""
    yesterday_str = (datetime.now() - timedelta(days=1)).strftime('%Y-%m-%d')
    report_date_str = (datetime.now() - timedelta(days=1)).strftime('%d %B %Y')

    final_report_lines = [f"<b>📈 Дневная сводка за {report_date_str}</b>"]
    
    timeout = aiohttp.ClientTimeout(total=240)
    async with aiohttp.ClientSession(timeout=timeout) as session:
        tasks = [process_single_account(session, acc, yesterday_str, meta_token) for acc in accounts]
        account_results = await asyncio.gather(*tasks, return_exceptions=True)

    active_reports = 0
    for result in account_results:
        if isinstance(result, Exception) or not result:
            continue
        final_report_lines.append(result)
        active_reports += 1
            
    if active_reports == 0:
        return "✅ За вчерашний день не было активности ни в одном из кабинетов."

    return "\n".join(final_report_lines)

async def process_single_account(session, acc, date_str, meta_token):
    """Обрабатывает один рекламный аккаунт и возвращает готовую текстовую секцию отчета."""
    # Шаг 1: Получаем цели кампаний
    objectives = await get_campaign_objectives(session, acc["account_id"], meta_token)
    if not objectives:
        return None # Нет кампаний в аккаунте

    # Шаг 2: Получаем статистику по объявлениям
    insights = await get_ad_level_insights_for_date(session, acc["account_id"], date_str, meta_token)
    if not insights:
        return None

    # Шаг 3: Структурируем данные
    campaigns_data = structure_insights(insights, objectives)
    if not campaigns_data:
        return None
        
    adsets = analyze_adsets(campaigns_data)
    
    # --- Считаем итоги по аккаунту ---
    total_spend = sum(adset['spend'] for adset in adsets)
    total_leads = sum(sum(ad['leads'] for ad in adset['ads']) for adset in adsets)
    total_clicks = sum(sum(ad['clicks'] for ad in adset['ads']) for adset in adsets)

    # --- Формируем текст ---
    report_lines = ["─" * 20, f"<b>🏢 Кабинет: <u>{acc['name']}</u></b>"]
    
    cost_str = ""
    if total_leads > 0:
        cpl = total_spend / total_leads
        cost_str += f"Лиды: {total_leads} | Ср. CPL: ${cpl:.2f}"
    if total_clicks > 0:
        cpc = total_spend / total_clicks
        if cost_str: cost_str += " | "
        cost_str += f"Клики: {total_clicks} | Ср. CPC: ${cpc:.2f}"
        
    report_lines.append(f"`Расход: ${total_spend:.2f}`")
    if cost_str:
        report_lines.append(f"`{cost_str}`")
    
    # --- Анализ групп ---
    adsets_with_cost = sorted([a for a in adsets if a['cost'] != float('inf')], key=lambda x: x['cost'])
    
    if not adsets_with_cost:
        return "\n".join(report_lines)

    best_adset = adsets_with_cost[0]
    worst_adset = adsets_with_cost[-1] if len(adsets_with_cost) > 1 else None

    report_lines.append("\n" + f"<b>Лучшая группа:</b> {best_adset['name']} ({best_adset['campaign_name']})")
    report_lines.append(f"  - Расход: ${best_adset['spend']:.2f} | {best_adset['cost_type']}: ${best_adset['cost']:.2f}")
    report_lines.extend(format_ad_list(best_adset['ads'], best_adset['cost_type']))

    if worst_adset and worst_adset['id'] != best_adset['id']:
        report_lines.append("\n" + f"<b>Худшая группа:</b> {worst_adset['name']} ({worst_adset['campaign_name']})")
        report_lines.append(f"  - Расход: ${worst_adset['spend']:.2f} | {worst_adset['cost_type']}: ${worst_adset['cost']:.2f}")
        report_lines.extend(format_ad_list(worst_adset['ads'], worst_adset['cost_type']))
        
    return "\n".join(report_lines)
