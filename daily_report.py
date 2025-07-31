import os
import asyncio
import aiohttp
from datetime import datetime, timedelta
import json  # <--- 1. ДОБАВЛЕН ЭТОТ ИМПОРТ
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
    """Получает статистику за указанный временной диапазон."""
    url = f"https://graph.facebook.com/{API_VERSION}/act_{account_id}/insights"
    params = {
        "fields": "campaign_id,campaign_name,adset_id,adset_name,ad_id,ad_name,spend,actions,ctr,objective",
        "level": "ad",
        "time_range": json.dumps(time_range),  # <--- 2. ИСПРАВЛЕНА ЭТА СТРОКА
        "limit": 2000
    }
    data = await fb_get(session, url, params=params)
    return data.get("data", [])


# --- Функции обработки и анализа данных ---

def process_insights_data(insights: list):
    """Обрабатывает сырые данные инсайтов, возвращая словарь с ключевыми метриками."""
    data = {}
    for ad in insights:
        spend = float(ad.get("spend", 0))
        if spend == 0: continue
        
        camp_id = ad.get('campaign_id')
        if not camp_id: continue

        if camp_id not in data:
            data[camp_id] = {
                "name": ad.get('campaign_name'),
                "objective": ad.get('objective', 'N/A'),
                "spend": 0, "leads": 0, "clicks": 0
            }
        
        leads = sum(int(a["value"]) for a in ad.get("actions", []) if a.get("action_type") == LEAD_ACTION_TYPE)
        clicks = sum(int(a["value"]) for a in ad.get("actions", []) if a.get("action_type") == LINK_CLICK_ACTION_TYPE)
        
        data[camp_id]["spend"] += spend
        data[camp_id]["leads"] += leads
        data[camp_id]["clicks"] += clicks
        
    return data

def get_change_indicator(new, old, is_cost=False):
    """Возвращает строку с процентом изменения и эмодзи."""
    if old == 0:
        return "(новая)" if new > 0 else ""
        
    percent_change = ((new - old) / old) * 100
    
    # Для CPL/CPC инвертируем логику: рост - это плохо
    if is_cost:
        emoji = "📈" if new > old else "📉"
    else:
        emoji = "📈" if new > old else "📉"
    
    return f"({emoji} {percent_change:+.0f}%)"


# --- Функции форматирования отчета ---

def format_summary(data_yesterday, data_before_yesterday):
    """Форматирует главную сводку со сравнением."""
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
        "<b>📊 Общая статистика:</b>",
        f"● Расход: ${y_spend:.2f} {spend_change}",
        f"● Лиды: {y_leads} {leads_change}",
        f"● Средний CPL: ${y_cpl:.2f} {cpl_change}",
    ]
    return "\n".join(lines)

def format_key_campaigns(data_yesterday):
    """Находит и форматирует лучшую и худшую кампании."""
    if not data_yesterday: return ""
    
    campaign_perf = []
    for camp_id, data in data_yesterday.items():
        # Определяем, что считать основной метрикой стоимости
        is_traffic = "TRAFFIC" in data['objective'].upper() or "LINK_CLICKS" in data['objective'].upper()
        
        if is_traffic:
            cost = (data['spend'] / data['clicks']) if data['clicks'] > 0 else float('inf')
            metric = "CPC"
        else:
            cost = (data['spend'] / data['leads']) if data['leads'] > 0 else float('inf')
            metric = "CPL"
        
        if cost != float('inf'):
            campaign_perf.append({"name": data['name'], "cost": cost, "metric": metric})

    if not campaign_perf: return ""

    sorted_campaigns = sorted(campaign_perf, key=lambda x: x['cost'])
    best = sorted_campaigns[0]
    worst = sorted_campaigns[-1]

    lines = ["<b>🔑 Ключевые кампании:</b>"]
    lines.append(f"🏆 Лучшая: \"{best['name']}\" ({best['metric']}: ${best['cost']:.2f})")
    if best['name'] != worst['name']:
        lines.append(f"🐌 Худшая: \"{worst['name']}\" ({worst['metric']}: ${worst['cost']:.2f})")
        
    return "\n".join(lines)

def format_notifications(data_yesterday, data_before_yesterday):
    """Создает список уведомлений и алертов."""
    alerts = []
    # Проверяем рост CPL
    for camp_id, y_data in data_yesterday.items():
        if camp_id in data_before_yesterday:
            by_data = data_before_yesterday[camp_id]
            
            y_cpl = (y_data['spend'] / y_data['leads']) if y_data['leads'] > 0 else 0
            by_cpl = (by_data['spend'] / by_data['leads']) if by_data['leads'] > 0 else 0

            if by_cpl > 0.1 and y_cpl > (by_cpl * 1.5): # Если CPL был больше 10 центов и вырос на 50%
                growth = ((y_cpl - by_cpl) / by_cpl) * 100
                alerts.append(f"🔴 <b>Внимание!</b> В кампании \"{y_data['name']}\" CPL вырос на {growth:.0f}% до ${y_cpl:.2f}!")

    # Проверяем кампании, которые остановились
    for camp_id, by_data in data_before_yesterday.items():
        if camp_id not in data_yesterday and by_data['spend'] > 1: # Если кампания вчера потратила >$1, а сегодня нет
            alerts.append(f"🟡 Кампания \"{by_data['name']}\" вчера не имела затрат.")

    if not alerts:
        alerts.append("✅ Не обнаружено критических изменений.")
        
    return "<b>💡 Уведомления:</b>\n" + "\n".join(alerts)


# --- Главная функция модуля ---

async def generate_daily_report_text() -> str:
    """Главная функция, которая собирает все данные и формирует итоговый отчет."""
    
    today = datetime.now()
    yesterday_str = (today - timedelta(days=1)).strftime('%Y-%m-%d')
    before_yesterday_str = (today - timedelta(days=2)).strftime('%Y-%m-%d')
    
    time_range_yesterday = {'since': yesterday_str, 'until': yesterday_str}
    time_range_before_yesterday = {'since': before_yesterday_str, 'until': before_yesterday_str}

    all_insights_yesterday = []
    all_insights_before_yesterday = []

    timeout = aiohttp.ClientTimeout(total=300)
    async with aiohttp.ClientSession(timeout=timeout) as session:
        accounts_response = await fb_get(session, f"https://graph.facebook.com/{API_VERSION}/me/adaccounts", {"fields": "name,account_id"})
        accounts = accounts_response.get("data", [])
        
        if not accounts: return "❌ Не найдено ни одного рекламного аккаунта."

        for acc in accounts:
            try:
                y_insights = await get_insights_for_range(session, acc['account_id'], time_range_yesterday)
                by_insights = await get_insights_for_range(session, acc['account_id'], time_range_before_yesterday)
                all_insights_yesterday.extend(y_insights)
                all_insights_before_yesterday.extend(by_insights)
            except Exception as e:
                print(f"Ошибка при получении данных для аккаунта {acc['name']}: {e}")

    processed_yesterday = process_insights_data(all_insights_yesterday)

    if not processed_yesterday:
        return "✅ За вчерашний день не было активности ни в одном из кабинетов."
    
    processed_before_yesterday = process_insights_data(all_insights_before_yesterday)
    
    report_date_str = (today - timedelta(days=1)).strftime('%d %B %Y')
    prev_date_str = (today - timedelta(days=2)).strftime('%d %B')
    
    header = f"<b>📈 Дневная сводка за {report_date_str}</b>\n<i>Сравнение с предыдущим днём ({prev_date_str})</i>"
    summary_block = format_summary(processed_yesterday, processed_before_yesterday)
    key_campaigns_block = format_key_campaigns(processed_yesterday)
    notifications_block = format_notifications(processed_yesterday, processed_before_yesterday)
    
    final_report = "\n\n".join(filter(None, [header, summary_block, key_campaigns_block, notifications_block]))
    
    return final_report
