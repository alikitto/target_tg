import os
import asyncio
import aiohttp
from datetime import datetime, timedelta
import json
from dotenv import load_dotenv

# --- Конфигурация ---
load_dotenv()
API_VERSION = "v19.0"
META_TOKEN = os.getenv("META_ACCESS_TOKEN")
LEAD_ACTION_TYPE = "onsite_conversion.messaging_conversation_started_7d"
LINK_CLICK_ACTION_TYPE = "link_click"


# --- Функции API ---

async def fb_get(session: aiohttp.ClientSession, url: str, params: dict = None):
    params = params or {}
    params["access_token"] = META_TOKEN
    async with session.get(url, params=params) as response:
        response.raise_for_status()
        return await response.json()

async def get_insights_for_range(session: aiohttp.ClientSession, account_id: str, time_range: dict):
    url = f"https://graph.facebook.com/{API_VERSION}/act_{account_id}/insights"
    params = {
        "fields": "campaign_id,campaign_name,spend,actions,objective",
        "level": "campaign", # Собираем данные на уровне кампаний для анализа
        "time_range": json.dumps(time_range),
        "limit": 500
    }
    data = await fb_get(session, url, params=params)
    return data.get("data", [])


# --- Функции обработки и анализа данных ---

def process_insights_data(insights: list):
    data = {}
    for campaign in insights:
        spend = float(campaign.get("spend", 0))
        if spend == 0: continue
        
        camp_id = campaign.get('campaign_id')
        if not camp_id: continue

        data[camp_id] = {
            "name": campaign.get('campaign_name'),
            "objective": campaign.get('objective', 'N/A'),
            "spend": spend,
            "leads": sum(int(a["value"]) for a in campaign.get("actions", []) if a.get("action_type") == LEAD_ACTION_TYPE),
            "clicks": sum(int(a["value"]) for a in campaign.get("actions", []) if a.get("action_type") == LINK_CLICK_ACTION_TYPE),
        }
    return data

def get_change_indicator(new, old, is_cost=False):
    if old == 0:
        return "(новая)" if new > 0 else ""
    percent_change = ((new - old) / old) * 100
    emoji = "📈" if new > old else "📉"
    if is_cost: # Для CPL/CPC инвертируем логику
        emoji = "📈" if new > old else "📉"
    return f"({emoji} {percent_change:+.0f}%)"


# --- Функции форматирования отчета ---

def format_summary(title: str, data_yesterday: dict, data_before_yesterday: dict):
    """Форматирует блок сводки (как общую, так и для одного кабинета)."""
    y_spend = sum(c['spend'] for c in data_yesterday.values())
    y_leads = sum(c['leads'] for c in data_yesterday.values())
    y_cpl = (y_spend / y_leads) if y_leads > 0 else 0

    by_spend = sum(c['spend'] for c in data_before_yesterday.values())
    by_leads = sum(c['leads'] for c in data_before_yesterday.values())
    by_cpl = (by_spend / by_leads) if by_leads > 0 else 0

    spend_change = get_change_indicator(y_spend, by_spend)
    leads_change = get_change_indicator(y_leads, by_leads)
    cpl_change = get_change_indicator(y_cpl, by_cpl, is_cost=True)

    lines = [
        f"<b>{title}</b>",
        f"● Расход: ${y_spend:.2f} {spend_change}",
        f"● Лиды: {y_leads} {leads_change}",
        f"● Средний CPL: ${y_cpl:.2f} {cpl_change}",
    ]
    return "\n".join(lines)

def format_key_campaigns(data_yesterday: dict):
    """Находит и форматирует лучшую и худшую кампании."""
    campaign_perf = []
    for camp_id, data in data_yesterday.items():
        is_traffic = "TRAFFIC" in data['objective'].upper() or "LINK_CLICKS" in data['objective'].upper()
        cost = float('inf')
        metric = "CPL"
        if is_traffic:
            metric = "CPC"
            if data['clicks'] > 0: cost = data['spend'] / data['clicks']
        elif data['leads'] > 0:
            cost = data['spend'] / data['leads']
        
        if cost != float('inf'):
            campaign_perf.append({"name": data['name'], "cost": cost, "metric": metric})

    if not campaign_perf: return ""
    
    sorted_campaigns = sorted(campaign_perf, key=lambda x: x['cost'])
    best = sorted_campaigns[0]
    worst = sorted_campaigns[-1]

    lines = ["<b>🔑 Ключевые кампании:</b>"]
    lines.append(f"🏆 Лучшая: \"{best['name']}\" ({best['metric']}: ${best['cost']:.2f})")
    if best['name'] != worst['name'] and len(sorted_campaigns) > 1:
        lines.append(f"🐌 Худшая: \"{worst['name']}\" ({worst['metric']}: ${worst['cost']:.2f})")
    return "\n".join(lines)


# --- Главные функции модуля ---

async def process_single_account(session: aiohttp.ClientSession, acc: dict, time_yesterday: dict, time_before_yesterday: dict):
    """Новая функция: Собирает, анализирует и форматирует отчет для ОДНОГО аккаунта."""
    try:
        insights_yesterday = await get_insights_for_range(session, acc['account_id'], time_yesterday)
        insights_before_yesterday = await get_insights_for_range(session, acc['account_id'], time_before_yesterday)

        processed_yesterday = process_insights_data(insights_yesterday)
        if not processed_yesterday:
            return None # Если не было активности, пропускаем аккаунт

        processed_before_yesterday = process_insights_data(insights_before_yesterday)
        
        # Собираем текстовый блок для этого аккаунта
        summary_title = f"🏢 Кабинет: <u>{acc['name']}</u>"
        summary_block = format_summary(summary_title, processed_yesterday, processed_before_yesterday)
        key_campaigns_block = format_key_campaigns(processed_yesterday)

        report_text = "\n\n".join(filter(None, [summary_block, key_campaigns_block]))
        
        # Возвращаем и текст, и данные для общей сводки
        return {
            "text": report_text,
            "data_y": processed_yesterday,
            "data_by": processed_before_yesterday,
        }
    except Exception as e:
        print(f"Ошибка при обработке аккаунта {acc['name']}: {e}")
        return None

async def generate_daily_report_text() -> str:
    """Главная функция, которая формирует итоговый отчет."""
    today = datetime.now()
    yesterday_str = (today - timedelta(days=1)).strftime('%Y-%m-%d')
    before_yesterday_str = (today - timedelta(days=2)).strftime('%Y-%m-%d')
    
    time_range_yesterday = {'since': yesterday_str, 'until': yesterday_str}
    time_range_before_yesterday = {'since': before_yesterday_str, 'until': before_yesterday_str}

    timeout = aiohttp.ClientTimeout(total=300)
    async with aiohttp.ClientSession(timeout=timeout) as session:
        accounts_response = await fb_get(session, f"https://graph.facebook.com/{API_VERSION}/me/adaccounts", {"fields": "name,account_id"})
        accounts = accounts_response.get("data", [])
        
        if not accounts: return "❌ Не найдено ни одного рекламного аккаунта."

        tasks = [process_single_account(session, acc, time_range_yesterday, time_range_before_yesterday) for acc in accounts]
        results = await asyncio.gather(*tasks)

    valid_results = [res for res in results if res]

    if not valid_results:
        return "✅ За вчерашний день не было активности ни в одном из кабинетов."
    
    # --- Формирование общей сводки ---
    total_y_data = {}
    total_by_data = {}
    for res in valid_results:
        for camp_id, data in res['data_y'].items():
            total_y_data[f"{res['text']}_{camp_id}"] = data # Используем уникальные ключи
        for camp_id, data in res['data_by'].items():
            total_by_data[f"{res['text']}_{camp_id}"] = data
            
    total_summary_block = format_summary("📊 Общая сводка по всем кабинетам", total_y_data, total_by_data)
    
    # --- Сборка финального отчета ---
    report_date_str = (today - timedelta(days=1)).strftime('%d %B %Y')
    prev_date_str = (today - timedelta(days=2)).strftime('%d %B')
    
    header = f"<b>📈 Дневная сводка за {report_date_str}</b>\n<i>Сравнение с предыдущим днём ({prev_date_str})</i>"
    
    detailed_reports = [res['text'] for res in valid_results]
    
    # Соединяем все в один отчет: общая сводка, а затем детальные отчеты
    final_report = header + "\n\n" + total_summary_block + "\n" + "\n---\n".join(detailed_reports)
    
    return final_report
