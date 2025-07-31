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

async def get_campaign_insights_for_date(session: aiohttp.ClientSession, account_id: str, date_str: str, access_token: str):
    """Получает статистику на уровне кампаний за конкретную дату."""
    url = f"https://graph.facebook.com/{API_VERSION}/act_{account_id}/insights"
    params = {
        "fields": "campaign_id,campaign_name,spend,actions",
        "level": "campaign",
        "time_range": json.dumps({"since": date_str, "until": date_str}),
        "limit": 500
    }
    data = await fb_get(session, url, params=params, access_token=access_token)
    return data.get("data", [])

def process_insights(insights: list):
    """Обрабатывает сырые данные из API в структурированный словарь."""
    processed_data = {}
    for item in insights:
        spend = float(item.get("spend", 0))
        if spend == 0: continue

        leads = sum(int(a["value"]) for a in item.get("actions", []) if a.get("action_type") == LEAD_ACTION_TYPE)
        clicks = sum(int(a["value"]) for a in item.get("actions", []) if a.get("action_type") == LINK_CLICK_ACTION_TYPE)
        
        cpl = (spend / leads) if leads > 0 else float('inf')
        cpc = (spend / clicks) if clicks > 0 else float('inf')

        processed_data[item["campaign_id"]] = {
            "name": item["campaign_name"],
            "spend": spend,
            "leads": leads,
            "clicks": clicks,
            "cpl": cpl,
            "cpc": cpc
        }
    return processed_data

def format_comparison(current_val, prev_val):
    """Форматирует строку сравнения с эмодзи и процентами."""
    if prev_val == 0:
        return "(...)" if current_val > 0 else ""
        
    diff = current_val - prev_val
    percent_change = (diff / prev_val) * 100
    
    emoji = "📈" if percent_change > 5 else "📉" if percent_change < -5 else ""
    sign = "+" if percent_change >= 0 else ""
    return f"({emoji} {sign}{percent_change:.0f}%)"

def get_cost_string(campaign_data: dict):
    """Возвращает строку с CPL или CPC в зависимости от того, что актуально."""
    if campaign_data['cpl'] != float('inf'):
        return f"CPL: ${campaign_data['cpl']:.2f}"
    if campaign_data['cpc'] != float('inf'):
        return f"CPC: ${campaign_data['cpc']:.2f}"
    return "CPL/CPC: N/A"


async def generate_daily_report_text(accounts: list, meta_token: str):
    """Основная функция, которая собирает и генерирует текст отчета."""
    yesterday_str = (datetime.now() - timedelta(days=1)).strftime('%Y-%m-%d')
    day_before_str = (datetime.now() - timedelta(days=2)).strftime('%Y-%m-%d')
    
    all_accounts_yesterday_total = {"spend": 0, "leads": 0, "clicks": 0}
    all_accounts_day_before_total = {"spend": 0, "leads": 0, "clicks": 0}
    
    account_report_blocks = []

    timeout = aiohttp.ClientTimeout(total=180)
    async with aiohttp.ClientSession(timeout=timeout) as session:
        tasks = [process_single_account(session, acc, yesterday_str, day_before_str, meta_token) for acc in accounts]
        account_results = await asyncio.gather(*tasks)

    for result in account_results:
        if result:
            all_accounts_yesterday_total["spend"] += result["yesterday_total"]["spend"]
            all_accounts_yesterday_total["leads"] += result["yesterday_total"]["leads"]
            all_accounts_yesterday_total["clicks"] += result["yesterday_total"]["clicks"]
            
            all_accounts_day_before_total["spend"] += result["day_before_total"]["spend"]
            all_accounts_day_before_total["leads"] += result["day_before_total"]["leads"]
            all_accounts_day_before_total["clicks"] += result["day_before_total"]["clicks"]

            account_report_blocks.append(result["report_text"])

    if not account_report_blocks:
        return "✅ За вчерашний день не было активности ни в одном из кабинетов."

    # --- Формирование общей сводки ---
    report_date_str = (datetime.now() - timedelta(days=1)).strftime('%d %B %Y')
    final_report_lines = [f"<b>📈 Дневная сводка за {report_date_str}</b>", "─" * 20]

    spend_comp = format_comparison(all_accounts_yesterday_total["spend"], all_accounts_day_before_total["spend"])
    
    # Определяем, что важнее: лиды или клики
    if all_accounts_yesterday_total['leads'] >= all_accounts_yesterday_total['clicks']:
        results_val = all_accounts_yesterday_total["leads"]
        results_comp = format_comparison(results_val, all_accounts_day_before_total["leads"])
        results_label = "Лиды"
        cost_val = (all_accounts_yesterday_total['spend'] / results_val) if results_val > 0 else 0
        prev_cost_val = (all_accounts_day_before_total['spend'] / all_accounts_day_before_total['leads']) if all_accounts_day_before_total['leads'] > 0 else 0
        cost_label = "CPL"
    else:
        results_val = all_accounts_yesterday_total["clicks"]
        results_comp = format_comparison(results_val, all_accounts_day_before_total["clicks"])
        results_label = "Клики"
        cost_val = (all_accounts_yesterday_total['spend'] / results_val) if results_val > 0 else 0
        prev_cost_val = (all_accounts_day_before_total['spend'] / all_accounts_day_before_total['clicks']) if all_accounts_day_before_total['clicks'] > 0 else 0
        cost_label = "CPC"
        
    cost_comp = format_comparison(cost_val, prev_cost_val).replace('📈','📉').replace('📉','📈') # Инвертируем эмодзи для цены

    final_report_lines.append("<b>📊 ОБЩИЙ ИТОГ</b>")
    final_report_lines.append(f"● **Расход:** ${all_accounts_yesterday_total['spend']:.2f} {spend_comp}")
    final_report_lines.append(f"● **{results_label}:** {results_val} {results_comp}")
    final_report_lines.append(f"● **Средний {cost_label}:** ${cost_val:.2f} {cost_comp}")
    
    final_report_lines.extend(account_report_blocks)
    return "\n".join(final_report_lines)

async def process_single_account(session, acc, yesterday_str, day_before_str, meta_token):
    """Собирает, анализирует и форматирует данные для одного аккаунта."""
    yesterday_insights_raw = await get_campaign_insights_for_date(session, acc["account_id"], yesterday_str, meta_token)
    yesterday_data = process_insights(yesterday_insights_raw)

    if not yesterday_data:
        return None

    day_before_insights_raw = await get_campaign_insights_for_date(session, acc["account_id"], day_before_str, meta_token)
    day_before_data = process_insights(day_before_insights_raw)
    
    # --- Считаем итоги по аккаунту ---
    yesterday_total = {"spend": sum(c['spend'] for c in yesterday_data.values()), 
                       "leads": sum(c['leads'] for c in yesterday_data.values()), 
                       "clicks": sum(c['clicks'] for c in yesterday_data.values())}
    day_before_total = {"spend": sum(c['spend'] for c in day_before_data.values()), 
                        "leads": sum(c['leads'] for c in day_before_data.values()), 
                        "clicks": sum(c['clicks'] for c in day_before_data.values())}
    
    # --- Формируем текст ---
    report_lines = ["─" * 20, f"<b>🏢 Кабинет: <u>{acc['name']}</u></b>"]
    
    spend_comp = format_comparison(yesterday_total["spend"], day_before_total["spend"])
    report_lines.append(f"`Расход: ${yesterday_total['spend']:.2f} {spend_comp}`")

    # --- Анализ кампаний ---
    # Фильтруем кампании, чтобы определить лучшую/худшую по CPL или CPC
    cpl_campaigns = sorted([c for c in yesterday_data.values() if c['cpl'] != float('inf')], key=lambda x: x['cpl'])
    cpc_campaigns = sorted([c for c in yesterday_data.values() if c['cpc'] != float('inf')], key=lambda x: x['cpc'])
    
    best_campaign = None
    if cpl_campaigns: best_campaign = cpl_campaigns[0]
    elif cpc_campaigns: best_campaign = cpc_campaigns[0]

    worst_campaign = None
    if cpl_campaigns and len(cpl_campaigns) > 1: worst_campaign = cpl_campaigns[-1]
    elif cpc_campaigns and len(cpc_campaigns) > 1: worst_campaign = cpc_campaigns[-1]

    spend_campaign = sorted(yesterday_data.values(), key=lambda x: x['spend'])[-1]

    if best_campaign:
        report_lines.append(f"🏆 **Лучшая:** {best_campaign['name']} ({get_cost_string(best_campaign)})")
    if worst_campaign and worst_campaign['name'] != best_campaign['name']:
         report_lines.append(f"🐌 **Худшая:** {worst_campaign['name']} ({get_cost_string(worst_campaign)})")
    if spend_campaign['name'] != best_campaign['name']:
        report_lines.append(f"💰 **Затратная:** {spend_campaign['name']} (Расход: ${spend_campaign['spend']:.2f})")

    # --- Поиск алертов ---
    alerts = []
    for camp_id, camp_data in yesterday_data.items():
        prev_camp_data = day_before_data.get(camp_id)
        if not prev_camp_data: continue # Не с чем сравнивать

        # Алерт на рост цены
        current_cost = camp_data['cpl'] if camp_data['cpl'] != float('inf') else camp_data['cpc']
        prev_cost = prev_camp_data['cpl'] if prev_camp_data['cpl'] != float('inf') else prev_camp_data['cpc']

        if prev_cost > 0 and current_cost > prev_cost * 1.7: # Рост цены более чем на 70%
            increase_percent = ((current_cost - prev_cost) / prev_cost) * 100
            alerts.append(f"🔴 **Рост цены!** В кампании \"{camp_data['name']}\" CPL/CPC вырос на **{increase_percent:.0f}%** до ${current_cost:.2f}")

    if not day_before_data and yesterday_data:
        alerts.append(f"🟢 **Новая активность!** В кабинете вчера были первые результаты.")

    if alerts:
        report_lines.append("") # Пустая строка для отступа
        report_lines.extend(alerts)

    return {
        "yesterday_total": yesterday_total,
        "day_before_total": day_before_total,
        "report_text": "\n".join(report_lines)
    }
