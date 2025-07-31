import os
import asyncio
import aiohttp
import json
from datetime import datetime
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

sent_messages_by_chat = {}

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

# ### ИЗМЕНЕНИЕ: Новая функция для получения объявлений с креативами
async def get_all_ads_with_creatives(session: aiohttp.ClientSession, account_id: str, active_adset_ids: list):
    """Получает все активные объявления для указанных групп с их креативами."""
    url = f"https://graph.facebook.com/{API_VERSION}/act_{account_id}/ads"
    filtering = [
        {'field': 'adset.id', 'operator': 'IN', 'value': active_adset_ids},
        {'field': 'effective_status', 'operator': 'IN', 'value': ['ACTIVE']}
    ]
    params = {
        "fields": "id,name,adset_id,campaign_id,creative{thumbnail_url}",
        "filtering": json.dumps(filtering),
        "limit": 1000
    }
    data = await fb_get(session, url, params)
    return data.get("data", [])

# ### ИЗМЕНЕНИЕ: Функция для получения статистики на уровне объявлений
async def get_ad_level_insights(session: aiohttp.ClientSession, account_id: str, ad_ids: list):
    """Получает статистику для конкретных объявлений."""
    start_date = "2025-06-01"
    end_date = datetime.now().strftime("%Y-%m-%d")
    ad_ids_json_string = json.dumps(ad_ids)
    url = f"https://graph.facebook.com/{API_VERSION}/act_{account_id}/insights"
    params = {
        "fields": "ad_id,spend,actions,ctr",
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
def cpl_label(cpl: float) -> str:
    if cpl <= 1: return "🟢 Дешёвый"
    if cpl <= 3: return "🟡 Средний"
    return "🔴 Дорогой"

async def send_and_store(message: Message | CallbackQuery, text: str, *, is_persistent: bool = False, **kwargs):
    msg_obj = message.message if isinstance(message, CallbackQuery) else message
    # Отключаем предпросмотр ссылок, чтобы не загромождать чат
    kwargs.setdefault('disable_web_page_preview', True)
    msg = await msg_obj.answer(text, **kwargs)
    chat_id = msg.chat.id
    if chat_id not in sent_messages_by_chat:
        sent_messages_by_chat[chat_id] = []
    sent_messages_by_chat[chat_id].append({"id": msg.message_id, "persistent": is_persistent})
    return msg

# ============================
# ===         Меню         ===
# ============================
async def set_bot_commands(bot: Bot):
    commands = [
        BotCommand(command="start", description="🚀 Запустить бота / Показать меню"),
        BotCommand(command="report", description="📊 Отчёт по активным кампаниям"),
        BotCommand(command="clear", description="🧹 Очистить временные сообщения"),
        BotCommand(command="help", description="ℹ️ Помощь"),
    ]
    await bot.set_my_commands(commands, BotCommandScopeDefault())

def inline_main_menu():
    kb = InlineKeyboardBuilder()
    kb.button(text="📊 Отчёт: Активные кампании", callback_data="build_report")
    kb.button(text="🧹 Очистить временные сообщения", callback_data="clear_chat")
    kb.button(text="ℹ️ Помощь", callback_data="help")
    kb.adjust(1)
    return kb.as_markup()

# ============================
# ===       Хендлеры       ===
# ============================
@router.message(Command("start", "restart"))
async def start_handler(msg: Message):
    await send_and_store(msg, "👋 Привет! Я твой бот для управления рекламой.\n\nВыберите действие:", is_persistent=True, reply_markup=inline_main_menu())

@router.callback_query(F.data == "show_menu")
async def show_menu_handler(call: CallbackQuery):
    await call.message.edit_text("👋 Привет! Я твой бот для управления рекламой.\n\nВыберите действие:", reply_markup=inline_main_menu())
    chat_id = call.message.chat.id
    if chat_id in sent_messages_by_chat:
        for msg_info in sent_messages_by_chat[chat_id]:
            if msg_info["id"] == call.message.message_id:
                msg_info["persistent"] = True
                break

@router.message(Command("help"))
@router.callback_query(F.data == "help")
async def help_handler(event: Message | CallbackQuery):
    help_text = (
        "<b>ℹ️ Справка по командам:</b>\n\n"
        "<b>/start</b> - Показать главное меню.\n"
        "<b>/report</b> - Сформировать отчёт по активным кампаниям.\n"
        "<b>/clear</b> - Удалить временные сообщения (отчёты, статусы загрузки), оставив меню и важные уведомления.\n\n"
        "Бот использует API Facebook для получения данных в реальном времени."
    )
    await send_and_store(event, help_text, is_persistent=True, reply_markup=inline_main_menu())

@router.message(Command("clear"))
@router.callback_query(F.data == "clear_chat")
async def clear_chat_handler(event: Message | CallbackQuery):
    message = event.message if isinstance(event, CallbackQuery) else event
    chat_id = message.chat.id
    if chat_id in sent_messages_by_chat and sent_messages_by_chat[chat_id]:
        messages_to_delete = [msg_info["id"] for msg_info in sent_messages_by_chat[chat_id] if not msg_info.get("persistent", False)]
        sent_messages_by_chat[chat_id] = [msg_info for msg_info in sent_messages_by_chat[chat_id] if msg_info.get("persistent", False)]
        count = 0
        for msg_id in messages_to_delete:
            try:
                await bot.delete_message(chat_id, msg_id)
                count += 1
            except TelegramBadRequest:
                pass
        await message.answer(f"✅ Готово! Удалил {count} временных сообщений.")
    else:
        await message.answer("ℹ️ Сообщений для удаления нет.")
    if isinstance(event, CallbackQuery):
        await start_handler(message)

# ============ Отчёт с лоадером ============
@router.message(Command("report"))
@router.callback_query(F.data == "build_report")
async def build_report(event: Message | CallbackQuery):
    message = event.message if isinstance(event, CallbackQuery) else event
    status_msg = await send_and_store(message, "⏳ Начинаю сбор данных...")
    all_accounts_data = {}
    
    timeout = aiohttp.ClientTimeout(total=120)
    
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
                        leads = sum(int(a["value"]) for a in row.get("actions", []) if a.get("action_type") == LEAD_ACTION_TYPE)
                        ctr = float(row.get("ctr", 0))
                        insights_map[ad_id] = {"spend": spend, "leads": leads, "ctr": ctr}

                    # Структурирование данных
                    account_data = {}
                    for ad in ads:
                        ad_id = ad['id']
                        adset_id = ad['adset_id']
                        campaign_id = ad.get('campaign_id')

                        if adset_id not in adsets_map or campaign_id not in campaigns_map:
                            continue

                        stats = insights_map.get(ad_id)
                        if not stats or (stats['spend'] == 0 and stats['leads'] == 0):
                            continue
                        
                        cpl = (stats['spend'] / stats['leads']) if stats['leads'] > 0 else 0
                        
                        if campaign_id not in account_data:
                            campaign_obj = campaigns_map[campaign_id]
                            objective_clean = campaign_obj.get("objective", "N/A").replace('OUTCOME_', '').replace('_', ' ').capitalize()
                            account_data[campaign_id] = {
                                "name": campaign_obj['name'],
                                "objective": objective_clean,
                                "adsets": {}
                            }
                        
                        if adset_id not in account_data[campaign_id]['adsets']:
                            adset_obj = adsets_map[adset_id]
                            account_data[campaign_id]['adsets'][adset_id] = {
                                "name": adset_obj['name'],
                                "ads": []
                            }
                        
                        ad_info = {
                            "name": ad['name'],
                            "thumbnail_url": ad.get('creative', {}).get('thumbnail_url'),
                            "cpl": cpl,
                            "ctr": stats['ctr'],
                            "leads": stats['leads'],
                            "spend": stats['spend']
                        }
                        account_data[campaign_id]['adsets'][adset_id]['ads'].append(ad_info)

                    if account_data:
                        all_accounts_data[acc['name']] = account_data

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
    
    try: await bot.delete_message(status_msg.chat.id, status_msg.message_id)
    except TelegramBadRequest: pass

    # ### ИЗМЕНЕНИЕ: Полностью переработанный блок форматирования вывода
    for acc_name, campaigns_data in all_accounts_data.items():
        active_campaign_count = len(campaigns_data)
        msg_lines = [
            f"<b>🏢 Рекламный кабинет:</b> <u>{acc_name}</u>",
            f"<b>📈 Активных кампаний:</b> {active_campaign_count}",
            "─" * 20
        ]
        
        for camp_id, camp_data in campaigns_data.items():
            msg_lines.append(f"\n<b>🎯 Кампания:</b> {camp_data['name']}")
            
            for adset_id, adset_data in camp_data['adsets'].items():
                # Считаем общую статистику для группы
                total_leads = sum(ad['leads'] for ad in adset_data['ads'])
                total_spend = sum(ad['spend'] for ad in adset_data['ads'])
                total_cpl = (total_spend / total_leads) if total_leads > 0 else 0
                
                adset_block = [
                    f"  <b>↳ Группа:</b> <code>{adset_data['name']}</code>",
                    f"    - <b>Цель:</b> {camp_data['objective']}",
                    f"    - <b>Лиды:</b> {total_leads}",
                    f"    - <b>Расход:</b> ${total_spend:.2f}",
                    f"    - <b>CPL:</b> ${total_cpl:.2f} {cpl_label(total_cpl)}"
                ]
                msg_lines.extend(adset_block)
                
                if adset_data['ads']:
                    msg_lines.append("  <b>↳ Объявления:</b>")
                    for ad in sorted(adset_data['ads'], key=lambda x: x['cpl']):
                        thumb_url = ad.get('thumbnail_url', '#')
                        ad_line = f'    <a href="{thumb_url}">🖼️</a> <b>{ad["name"]}</b> | CPL: ${ad["cpl"]:.2f} | CTR: {ad["ctr"]:.2f}%'
                        msg_lines.append(ad_line)

        await send_and_store(message, "\n".join(msg_lines))

    await send_and_store(message, "✅ Отчёт завершён.", is_persistent=True, reply_markup=inline_main_menu())

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
