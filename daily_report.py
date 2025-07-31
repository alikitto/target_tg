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
        # Эта строка вызовет исключение (ошибку), если статус ответа 4xx или 5xx
        response.raise_for_status()
        return await response.json()

async def get_insights_for_date(session: aiohttp.ClientSession, account_id: str, date_str: str, access_token: str):
    """Получает статистику на уровне аккаунта за конкретную дату."""
    url = f"https://graph.facebook.com/{API_VERSION}/act_{account_id}/insights"
    params = {
        "fields": "spend,actions",
        "level": "account",
        "time_range": json.dumps({"since": date_str, "until": date_str}),
    }
    data = await fb_get(session, url, params=params, access_token=access_token)
    
    if not data.get("data"):
        return {"spend": 0, "leads": 0, "clicks": 0}
        
    stats = data["data"][0]
    spend = float(stats.get("spend", 0))
    leads = sum(int(a["value"]) for a in stats.get("actions", []) if a.get("action_type") == LEAD_ACTION_TYPE)
    clicks = sum(int(a["value"]) for a in stats.get("actions", []) if a.get("action_type") == LINK_CLICK_ACTION_TYPE)
    
    return {"spend": spend, "leads": leads, "clicks": clicks}

def format_comparison(current_val, prev_val):
    """Форматирует строку сравнения с эмодзи и процентами."""
    if prev_val == 0:
        return "(...)" if current_val > 0 else ""
        
    diff = current_val - prev_val
    percent_change = (diff / prev_val) * 100
    
    # Небольшое изменение: эмодзи для CPL/CPC должны быть инвертированы
    # Рост цены - это плохо (📉), падение - хорошо (📈)
    is_cost_metric = "cost" in str(diff).lower() # Простой способ проверить, метрика ли это стоимости
    
    emoji = ""
    if percent_change > 5: emoji = "📉" if is_cost_metric else "📈"
    elif percent_change < -5: emoji = "📈" if is_cost_metric else "📉"
    
    sign = "+" if percent_change >= 0 else ""
    return f"({emoji} {sign}{percent_change:.0f}%)"

def calculate_cpl_cpc(stats):
    """Вычисляет CPL или CPC."""
    if stats["leads"] > 0:
        cost = stats["spend"] / stats["leads"]
        return cost, "CPL"
    if stats["clicks"] > 0:
        cost = stats["spend"] / stats["clicks"]
        return cost, "CPC"
    return 0, "CPL"

async def generate_daily_report_text(accounts: list, meta_token: str):
    """Основная функция, которая собирает данные и генерирует текст отчета."""
    yesterday_str = (datetime.now() - timedelta(days=1)).strftime('%Y-%m-%d')
    day_before_str = (datetime.now() - timedelta(days=2)).strftime('%Y-%m-%d')
    
    report_lines = []
    
    yesterday_total = {"spend": 0, "leads": 0, "clicks": 0}
    day_before_total = {"spend": 0, "leads": 0, "clicks": 0}
    active_accounts_reports = []

    timeout = aiohttp.ClientTimeout(total=180)
    async with aiohttp.ClientSession(timeout=timeout) as session:
        tasks = [process_single_account(session, acc, yesterday_str, day_before_str, meta_token) for acc in accounts]
        account_results = await asyncio.gather(*tasks)

    for result in account_results:
        if result:
            yesterday_total["spend"] += result["yesterday_stats"]["spend"]
            yesterday_total["leads"] += result["yesterday_stats"]["leads"]
            yesterday_total["clicks"] += result["yesterday_stats"]["clicks"]
            
            day_before_total["spend"] += result["day_before_stats"]["spend"]
            day_before_total["leads"] += result["day_before_stats"]["leads"]
            day_before_total["clicks"] += result["day_before_stats"]["clicks"]
            
            active_accounts_reports.append(result["report_text"])

    if not active_accounts_reports:
        return "✅ За вчерашний день не было активности ни в одном из кабинетов."

    report_date_str = (datetime.now() - timedelta(days=1)).strftime('%d %B %Y')
    report_lines.append(f"<b>📈 Дневная сводка за {report_date_str}</b>")
    report_lines.append("─" * 20)
    
    spend_comp = format_comparison(yesterday_total["spend"], day_before_total["spend"])
    results_val, results_comp, results_label = (yesterday_total["leads"], format_comparison(yesterday_total["leads"], day_before_total["leads"]), "Лиды") if yesterday_total["leads"] > 0 else (yesterday_total["clicks"], format_comparison(yesterday_total["clicks"], day_before_total["clicks"]), "Клики")
    cost_val, cost_label = calculate_cpl_cpc(yesterday_total)
    prev_cost_val, _ = calculate_cpl_cpc(day_before_total)
    cost_comp = format_comparison(cost_val, prev_cost_val)


    report_lines.append("<b>📊 Общий итог по всем кабинетам:</b>")
    report_lines.append(f"● **Расход:** ${yesterday_total['spend']:.2f} {spend_comp}")
    report_lines.append(f"● **{results_label}:** {results_val} {results_comp}")
    report_lines.append(f"● **Средний {cost_label}:** ${cost_val:.2f} {cost_comp}")
    report_lines.append("─" * 20)
    report_lines.extend(active_accounts_reports)

    return "\n".join(report_lines)

async def process_single_account(session, acc, yesterday_str, day_before_str, meta_token):
    """Собирает и форматирует данные для одного аккаунта."""
    yesterday_stats = await get_insights_for_date(session, acc["account_id"], yesterday_str, meta_token)
    
    if yesterday_stats["spend"] == 0:
        return None
        
    day_before_stats = await get_insights_for_date(session, acc["account_id"], day_before_str, meta_token)
    
    cost_val, cost_label = calculate_cpl_cpc(yesterday_stats)
    results_val, results_label = (yesterday_stats["leads"], "Лиды") if yesterday_stats["leads"] > 0 else (yesterday_stats["clicks"], "Клики")

    acc_report_text = (
        f"<b>🏢 Кабинет: <u>{acc['name']}</u></b>\n"
        f"  ● **Расход:** ${yesterday_stats['spend']:.2f}\n"
        f"  ● **{results_label}:** {results_val}\n"
        f"  ● **{cost_label}:** ${cost_val:.2f}"
    )
    
    return {
        "yesterday_stats": yesterday_stats,
        "day_before_stats": day_before_stats,
        "report_text": acc_report_text
    }
