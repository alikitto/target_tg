import asyncio
import aiohttp
from datetime import datetime, timedelta

# Эти константы и функция fb_get дублируются из main.py для независимости модуля.
# В более крупных проектах их выносят в общий config.py или api_client.py.
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

async def get_insights_for_period(session: aiohttp.ClientSession, account_id: str, date_preset: str, access_token: str):
    """Получает статистику на уровне аккаунта за определенный период."""
    url = f"https://graph.facebook.com/{API_VERSION}/act_{account_id}/insights"
    params = {
        "fields": "spend,actions",
        "level": "account",
        "date_preset": date_preset,
    }
    data = await fb_get(session, url, params=params, access_token=access_token)
    
    # Insights могут вернуться пустыми, если не было активности
    if not data.get("data"):
        return {"spend": 0, "leads": 0, "clicks": 0}
        
    stats = data["data"][0]
    spend = float(stats.get("spend", 0))
    leads = sum(int(a["value"]) for a in stats.get("actions", []) if a.get("action_type") == LEAD_ACTION_TYPE)
    clicks = sum(int(a["value"]) for a in stats.get("actions", []) if a.get("action_type") == LINK_CLICK_ACTION_TYPE)
    
    return {"spend": spend, "leads": leads, "clicks": clicks}

def format_comparison(today_val, yesterday_val):
    """Форматирует строку сравнения с эмодзи и процентами."""
    if yesterday_val == 0:
        return "(...)" if today_val > 0 else ""
        
    diff = today_val - yesterday_val
    percent_change = (diff / yesterday_val) * 100
    
    emoji = ""
    if percent_change > 5: emoji = "📈"
    elif percent_change < -5: emoji = "📉"
    
    sign = "+" if percent_change > 0 else ""
    return f"({emoji} {sign}{percent_change:.0f}%)"

def calculate_cpl_cpc(stats):
    """Вычисляет CPL или CPC в зависимости от наличия лидов или кликов."""
    if stats["leads"] > 0:
        cost = stats["spend"] / stats["leads"]
        return f"${cost:.2f}", "CPL"
    if stats["clicks"] > 0:
        cost = stats["spend"] / stats["clicks"]
        return f"${cost:.2f}", "CPC"
    return "$0.00", "CPL"

async def generate_daily_report_text(accounts: list, meta_token: str):
    """
    Основная функция, которая собирает данные и генерирует текст отчета.
    """
    report_lines = []
    
    yesterday_total = {"spend": 0, "leads": 0, "clicks": 0}
    day_before_total = {"spend": 0, "leads": 0, "clicks": 0}
    
    active_accounts_reports = []

    timeout = aiohttp.ClientTimeout(total=180)
    async with aiohttp.ClientSession(timeout=timeout) as session:
        tasks = []
        for acc in accounts:
            # Запускаем сбор данных для каждого аккаунта параллельно
            task = asyncio.create_task(
                process_single_account(session, acc, meta_token)
            )
            tasks.append(task)
            
        account_results = await asyncio.gather(*tasks)

    for result in account_results:
        if result: # Если по аккаунту была активность
            yesterday_total["spend"] += result["yesterday_stats"]["spend"]
            yesterday_total["leads"] += result["yesterday_stats"]["leads"]
            yesterday_total["clicks"] += result["yesterday_stats"]["clicks"]
            
            day_before_total["spend"] += result["day_before_stats"]["spend"]
            day_before_total["leads"] += result["day_before_stats"]["leads"]
            day_before_total["clicks"] += result["day_before_stats"]["clicks"]
            
            active_accounts_reports.append(result["report_text"])

    if not active_accounts_reports:
        return "✅ За вчерашний день не было активности ни в одном из кабинетов."

    # --- Формирование общей сводки ---
    report_date = (datetime.now() - timedelta(days=1)).strftime('%d %B %Y')
    report_lines.append(f"<b>📈 Дневная сводка за {report_date}</b>")
    report_lines.append("─" * 20)
    
    spend_comp = format_comparison(yesterday_total["spend"], day_before_total["spend"])
    results_val, results_comp = (yesterday_total["leads"], format_comparison(yesterday_total["leads"], day_before_total["leads"])) if yesterday_total["leads"] > 0 else (yesterday_total["clicks"], format_comparison(yesterday_total["clicks"], day_before_total["clicks"]))
    results_label = "Лиды" if yesterday_total["leads"] > 0 else "Клики"

    cost_str, cost_label = calculate_cpl_cpc(yesterday_total)
    
    report_lines.append("<b>📊 Общий итог по всем кабинетам:</b>")
    report_lines.append(f"● **Расход:** ${yesterday_total['spend']:.2f} {spend_comp}")
    report_lines.append(f"● **{results_label}:** {results_val} {results_comp}")
    report_lines.append(f"● **Средний {cost_label}:** {cost_str}")
    report_lines.append("─" * 20)

    # --- Добавление отчетов по каждому аккаунту ---
    report_lines.extend(active_accounts_reports)

    return "\n".join(report_lines)


async def process_single_account(session, acc, meta_token):
    """Собирает и форматирует данные для одного аккаунта."""
    yesterday_stats = await get_insights_for_period(session, acc["account_id"], "yesterday", meta_token)
    
    # Если вчера не было трат, нет смысла продолжать
    if yesterday_stats["spend"] == 0:
        return None
        
    day_before_stats = await get_insights_for_period(session, acc["account_id"], "last_2d", meta_token)
    # API за last_2d возвращает сумму. Нужно вычесть вчерашний день.
    day_before_stats = {k: day_before_stats[k] - yesterday_stats[k] for k in day_before_stats}

    # --- Форматирование текста для этого аккаунта ---
    cost_str, cost_label = calculate_cpl_cpc(yesterday_stats)
    results_val = yesterday_stats["leads"] if yesterday_stats["leads"] > 0 else yesterday_stats["clicks"]
    results_label = "Лиды" if yesterday_stats["leads"] > 0 else "Клики"

    acc_report_text = (
        f"<b>🏢 Кабинет: <u>{acc['name']}</u></b>\n"
        f"  ● **Расход:** ${yesterday_stats['spend']:.2f}\n"
        f"  ● **{results_label}:** {results_val}\n"
        f"  ● **{cost_label}:** {cost_str}"
    )
    
    return {
        "yesterday_stats": yesterday_stats,
        "day_before_stats": day_before_stats,
        "report_text": acc_report_text
    }
