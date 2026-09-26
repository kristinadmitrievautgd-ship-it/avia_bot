import asyncio
import datetime
import logging
import os
from aiohttp import web
from aiogram import Bot, Dispatcher, F, Router
from aiogram.filters import CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import (
    Message, ReplyKeyboardMarkup, KeyboardButton, 
    ReplyKeyboardRemove, InlineKeyboardMarkup, InlineKeyboardButton, CallbackQuery
)
import gspread
from google.oauth2.service_account import Credentials

BOT_TOKEN = "8942946995:AAEhCSsTK0z7x7XZlkGBZla3D1Afw-YxwMU"
SPREADSHEET_NAME = "Авиация_Учет_Рабочих_Процессов"
TMC_SPREADSHEET_NAME = "ТМЦ Реставрация"
CREDENTIALS_FILE = "credentials.json"

logging.basicConfig(level=logging.INFO)
bot = Bot(token=BOT_TOKEN)
dp = Dispatcher(storage=MemoryStorage())
router = Router()
dp.include_router(router)

def get_sheets():
    scopes = ["https://www.googleapis.com/auth/spreadsheets", "https://www.googleapis.com/auth/drive"]
    creds = Credentials.from_service_account_file(CREDENTIALS_FILE, scopes=scopes)
    gc = gspread.authorize(creds)
    sh = gc.open(SPREADSHEET_NAME)
    return sh.worksheet("План_и_Спецификации"), sh.worksheet("Отчеты"), sh.worksheet("Сотрудники")

def get_tmc_sheet():
    scopes = ["https://www.googleapis.com/auth/spreadsheets", "https://www.googleapis.com/auth/drive"]
    creds = Credentials.from_service_account_file(CREDENTIALS_FILE, scopes=scopes)
    gc = gspread.authorize(creds)
    return gc.open(TMC_SPREADSHEET_NAME)

def get_user_data(telegram_id):
    _, _, ws_users = get_sheets()
    records = ws_users.get_all_records()
    for row in records:
        if str(row.get("Telegram ID")) == str(telegram_id):
            return {"fio": row.get("ФИО"), "role": row.get("Роль")}
    return None

def register_user(telegram_id, fio, role="Маляр / Реставратор"):
    _, _, ws_users = get_sheets()
    ws_users.append_row([str(telegram_id), fio, role])

def get_planes():
    ws_plan, _, _ = get_sheets()
    records = ws_plan.get_all_records()
    return sorted(list(set(str(r["Бортовой номер"]).strip() for r in records if r.get("Бортовой номер"))))

def get_plane_specs(plane_num):
    ws_plan, _, _ = get_sheets()
    return [r for r in ws_plan.get_all_records() if str(r.get("Бортовой номер")).strip() == plane_num]

def get_parts_for_plane(plane_num):
    specs = get_plane_specs(plane_num)
    return [str(r.get("Деталь / Узел")).strip() for r in specs if r.get("Деталь / Узел")]

def add_report(fio, plane, part, stage, desc, photo_id="—"):
    _, ws_reports, _ = get_sheets()
    now_str = datetime.datetime.now().strftime("%d.%m.%Y %H:%M")
    ws_reports.append_row([now_str, fio, plane, part, stage, desc, photo_id])

def update_part_status(plane, part, new_status):
    ws_plan, _, _ = get_sheets()
    records = ws_plan.get_all_records()
    for idx, r in enumerate(records, start=2):
        if str(r.get("Бортовой номер")).strip() == plane and str(r.get("Деталь / Узел")).strip() == part:
            ws_plan.update_cell(idx, 6, new_status)
            break

def get_reports_by_user(fio):
    _, ws_reports, _ = get_sheets()
    records = ws_reports.get_all_records()
    return [r for r in records if str(r.get("ФИО Маляра")).strip().lower() == fio.strip().lower()]

# --- ФУНКЦИИ ПОИСКА ТМЦ ---
def search_tmc_items(query, category_name=None):
    sh = get_tmc_sheet()
    results = []
    query_lower = query.lower().strip() if query else ""
    
    if category_name:
        worksheets = [sh.worksheet(category_name)]
    else:
        worksheets = sh.worksheets()

    for ws in worksheets:
        rows = ws.get_all_values()
        if len(rows) < 2:
            continue
        
        for r in rows[1:]:
            if not any(r): 
                continue
            pn = r[0] if len(r) > 0 else ""
            name = r[1] if len(r) > 1 else ""
            chars = r[2] if len(r) > 2 else ""
            qty = r[3] if len(r) > 3 else ""
            store = r[4] if len(r) > 4 else ""
            comment = r[11] if len(r) > 11 else ""
            
            full_str = f"{pn} {name} {chars} {store} {comment}".lower()
            
            if not query_lower or query_lower in full_str:
                results.append({
                    "category": ws.title,
                    "pn": pn or "Не указан",
                    "name": name or "—",
                    "chars": chars or "—",
                    "qty": qty or "—",
                    "store": store or "—",
                    "comment": comment or ""
                })
                if len(results) >= 15 and query_lower:
                    break
        if len(results) >= 15 and query_lower:
            break

    return results

def get_tmc_categories_list():
    sh = get_tmc_sheet()
    return [ws.title for ws in sh.worksheets()]


# --- FSM СОСТОЯНИЯ ---
class Registration(StatesGroup):
    waiting_for_fio = State()

class ReportForm(StatesGroup):
    select_plane = State()
    select_part = State()
    select_stage = State()
    enter_desc = State()
    send_photo = State()
    update_status = State()

class TMCSearchState(StatesGroup):
    waiting_for_query = State()


# --- КЛАВИАТУРЫ ---
def main_menu_keyboard(is_admin=False):
    kb = [
        [KeyboardButton(text="✈ План и краски по борту"), KeyboardButton(text="📦 Поиск ТМЦ / P/N")],
        [KeyboardButton(text="📝 Сдать отчет"), KeyboardButton(text="👤 Мой профиль")]
    ]
    if is_admin:
        kb.append([KeyboardButton(text="👔 Панель руководителя")])
    return ReplyKeyboardMarkup(keyboard=kb, resize_keyboard=True)


# --- ОБРАБОТЧИКИ СООБЩЕНИЙ ---
@router.message(CommandStart())
async def cmd_start(message: Message, state: FSMContext):
    user_data = await asyncio.to_thread(get_user_data, message.from_user.id)
    if user_data:
        is_admin = user_data.get("role") in ["Руководитель", "Админ"]
        await message.answer(f"Приветствуем, {user_data['fio']}!", reply_markup=main_menu_keyboard(is_admin))
    else:
        await message.answer("Добро пожаловать! Введите ваше **ФИО** (например: *Иванов Иван Иванович*):", parse_mode="Markdown")
        await state.set_state(Registration.waiting_for_fio)

@router.message(Registration.waiting_for_fio)
async def process_fio(message: Message, state: FSMContext):
    fio = message.text.strip()
    if len(fio.split()) < 2:
        await message.answer("Введите Фамилию и Имя через пробел:")
        return
    await asyncio.to_thread(register_user, message.from_user.id, fio)
    await state.clear()
    await message.answer(f"Отлично, {fio}! Вы успешно зарегистрированы.", reply_markup=main_menu_keyboard())

@router.message(F.text == "👤 Мой профиль")
async def show_profile(message: Message):
    user_data = await asyncio.to_thread(get_user_data, message.from_user.id)
    if user_data:
        await message.answer(f"👤 **ФИО:** {user_data['fio']}\n🎭 **Роль:** {user_data['role']}\n🆔 **ID:** `{message.from_user.id}`", parse_mode="Markdown")


# --- РАЗДЕЛ ПОИСКА ТМЦ ---
@router.message(F.text == "📦 Поиск ТМЦ / P/N")
async def start_tmc_search(message: Message, state: FSMContext):
    kb = [
        [InlineKeyboardButton(text="🔎 Текстовый поиск / по P/N", callback_data="tmc_mode_text")],
        [InlineKeyboardButton(text="📁 Обзор по категориям (вкладкам)", callback_data="tmc_mode_cats")]
    ]
    await message.answer("📦 **Раздел ТМЦ и Расходных материалов**\n\nКак вы хотите выполнить поиск?", reply_markup=InlineKeyboardMarkup(inline_keyboard=kb), parse_mode="Markdown")

@router.callback_query(F.data == "tmc_mode_text")
async def tmc_prompt_text(callback: CallbackQuery, state: FSMContext):
    await callback.message.answer("Введите название материала, партийный номер (P/N) или марку (например: *cockpit*, *профколор*, *аэрозольная*):")
    await state.set_state(TMCSearchState.waiting_for_query)
    await callback.answer()

@router.message(TMCSearchState.waiting_for_query)
async def process_tmc_text_query(message: Message, state: FSMContext):
    query = message.text.strip()
    await message.answer(f"🔍 Ищу *«{query}»* в ТМЦ...", parse_mode="Markdown")
    
    results = await asyncio.to_thread(search_tmc_items, query)
    await state.clear()
    
    if not results:
        await message.answer("❌ Ничего не найдено по вашему запросу.")
        return
    
    text = f"📋 **Найдено совпадений ({len(results)}):**\n\n"
    for item in results:
        text += (
            f"🏷 **P/N:** `{item['pn']}`\n"
            f"📁 Категория: *{item['category']}*\n"
            f"📦 Наименование: {item['name']}\n"
            f"⚙ Характеристики: `{item['chars']}`\n"
            f"🛒 Магазин/Ссылка: {item['store']}\n"
        )
        if item['comment']:
            text += f"💬 Комментарий: {item['comment']}\n"
        text += "───────────────\n"
        
    await message.answer(text, parse_mode="Markdown")

@router.callback_query(F.data == "tmc_mode_cats")
async def tmc_show_categories(callback: CallbackQuery):
    cats = await asyncio.to_thread(get_tmc_categories_list)
    builder = [[InlineKeyboardButton(text=f"📁 {c}", callback_data=f"tmc_cat:{c}")] for c in cats]
    await callback.message.answer("Выберите категорию ТМЦ:", reply_markup=InlineKeyboardMarkup(inline_keyboard=builder))
    await callback.answer()

@router.callback_query(F.data.startswith("tmc_cat:"))
async def tmc_show_category_items(callback: CallbackQuery):
    cat_name = callback.data.split(":", 1)[1]
    results = await asyncio.to_thread(search_tmc_items, None, cat_name)
    
    if not results:
        await callback.message.answer(f"В категории **{cat_name}** пока нет записей.", parse_mode="Markdown")
        await callback.answer()
        return
    
    text = f"📂 **Материалы в категории «{cat_name}» (показаны первые {min(len(results), 15)}):**\n\n"
    for item in results[:15]:
        text += (
            f"🏷 **P/N:** `{item['pn']}`\n"
            f"📦 Наименование: {item['name']}\n"
            f"⚙ Характеристики: `{item['chars']}`\n"
            f"🛒 Магазин: {item['store']}\n"
            f"───────────────\n"
        )
    
    await callback.message.answer(text, parse_mode="Markdown")
    await callback.answer()


# --- ПАНЕЛЬ РУКОВОДИТЕЛЯ ---
@router.message(F.text == "👔 Панель руководителя")
async def admin_panel(message: Message):
    user_data = await asyncio.to_thread(get_user_data, message.from_user.id)
    if not user_data or user_data.get("role") not in ["Руководитель", "Админ"]:
        await message.answer("У вас нет доступа к панели руководителя.")
        return
    
    kb = [
        [InlineKeyboardButton(text="🔍 Найти отчеты по сотруднику", callback_data="admin_search_user")],
        [InlineKeyboardButton(text="📊 Сводка по всем бортам", callback_data="admin_summary_planes")]
    ]
    await message.answer("👔 **Панель управления руководителя:**", reply_markup=InlineKeyboardMarkup(inline_keyboard=kb), parse_mode="Markdown")

@router.callback_query(F.data == "admin_search_user")
async def admin_select_user_prompt(callback: CallbackQuery):
    _, _, ws_users = await asyncio.to_thread(get_sheets)
    users = [r.get("ФИО") for r in ws_users.get_all_records() if r.get("ФИО")]
    
    builder = [[InlineKeyboardButton(text=u, callback_data=f"adm_usr:{u}")] for u in users]
    await callback.message.answer("Выберите сотрудника для просмотра истории отчетов:", reply_markup=InlineKeyboardMarkup(inline_keyboard=builder))
    await callback.answer()

@router.callback_query(F.data.startswith("adm_usr:"))
async def admin_show_user_reports(callback: CallbackQuery):
    fio = callback.data.split(":")[1]
    reports = await asyncio.to_thread(get_reports_by_user, fio)
    
    if not reports:
        await callback.message.answer(f"У сотрудника {fio} пока нет сданных отчетов.")
        await callback.answer()
        return
    
    text = f"📋 **Последние отчеты сотрудника {fio}:**\n\n"
    for r in reports[-10:]:
        text += f"📅 `{r.get('Дата и время')}` | Борт: **{r.get('Бортовой номер')}**\n⚙ Деталь: {r.get('Деталь / Узел')}\n📌 Этап: {r.get('Выполненный этап')}\n💬 {r.get('Описание работ')}\n\n"
    
    await callback.message.answer(text, parse_mode="Markdown")
    await callback.answer()

@router.callback_query(F.data == "admin_summary_planes")
async def admin_summary_planes(callback: CallbackQuery):
    planes = await asyncio.to_thread(get_planes)
    text = "📊 **Общий статус по бортам:**\n\n"
    for p in planes:
        specs = await asyncio.to_thread(get_plane_specs, p)
        total = len(specs)
        done = sum(1 for s in specs if s.get("Текущий статус") == "Готово")
        in_prog = sum(1 for s in specs if s.get("Текущий статус") in ["В работе", "На сушке"])
        text += f"✈ **Борт {p}:** всего деталей: {total} | ✅ Готово: {done} | 🔄 В работе: {in_prog}\n"
    
    await callback.message.answer(text, parse_mode="Markdown")
    await callback.answer()


# --- РАЗДЕЛ СПЕЦИФИКАЦИЙ ПО БОРТУ ---
@router.message(F.text == "✈ План и краски по борту")
async def show_planes_list(message: Message):
    planes = await asyncio.to_thread(get_planes)
    if not planes:
        await message.answer("В таблице `План_и_Спецификации` нет занесенных самолетов.")
        return
    builder = [[InlineKeyboardButton(text=f"✈ Борт {p}", callback_data=f"info_plane:{p}")] for p in planes]
    await message.answer("Выберите бортовой номер самолета:", reply_markup=InlineKeyboardMarkup(inline_keyboard=builder))

@router.callback_query(F.data.startswith("info_plane:"))
async def process_plane_info(callback: CallbackQuery):
    plane_num = callback.data.split(":")[1]
    specs = await asyncio.to_thread(get_plane_specs, plane_num)
    
    if not specs:
        await callback.message.answer(f"По борту **{plane_num}** нет сохраненных деталей.", parse_mode="Markdown")
        await callback.answer()
        return

    header = f"✈ **Спецификации по борту {plane_num}:**\n\n"
    current_msg = header
    messages = []
    
    for idx, item in enumerate(specs, start=1):
        part_name = str(item.get('Деталь / Узел') or '—').strip()
        paint = str(item.get('Номер / Марка краски') or '—').strip()
        primer = str(item.get('Грунт / Подготовка') or '—').strip()
        deadline = str(item.get('Срок сдачи (Дедлайн)') or '—').strip()
        status = str(item.get('Текущий статус') or 'Не указан').strip()

        item_text = (
            f"**{idx}. {part_name}**\n"
            f"🎨 Краска: `{paint}`\n"
            f"🧪 Грунт: `{primer}`\n"
            f"📅 Сдать до: **{deadline}**\n"
            f"📌 Статус: *{status}*\n\n"
        )
        
        if len(current_msg) + len(item_text) > 3500:
            messages.append(current_msg)
            current_msg = f"✈ **Спецификации по борту {plane_num} (продолжение):**\n\n" + item_text
        else:
            current_msg += item_text
            
    if current_msg:
        messages.append(current_msg)
        
    for msg in messages:
        await callback.message.answer(msg, parse_mode="Markdown")
        
    await callback.answer()


# --- РАЗДЕЛ СДАЧИ ОТЧЕТОВ ---
@router.message(F.text == "📝 Сдать отчет")
async def start_report(message: Message, state: FSMContext):
    planes = await asyncio.to_thread(get_planes)
    if not planes:
        await message.answer("В плане нет активных самолетов.")
        return
    builder = [[InlineKeyboardButton(text=f"✈ {p}", callback_data=f"rep_plane:{p}")] for p in planes]
    await message.answer("Выберите бортовой номер:", reply_markup=InlineKeyboardMarkup(inline_keyboard=builder))
    await state.set_state(ReportForm.select_plane)

@router.callback_query(ReportForm.select_plane, F.data.startswith("rep_plane:"))
async def process_rep_plane(callback: CallbackQuery, state: FSMContext):
    plane_num = callback.data.split(":")[1]
    
    parts = await asyncio.to_thread(get_parts_for_plane, plane_num)
    await state.update_data(plane=plane_num, parts_list=parts)
    
    builder = [
        [InlineKeyboardButton(text=f"🔧 {part}", callback_data=f"rep_part:{i}")] 
        for i, part in enumerate(parts)
    ]
    
    await callback.message.answer(
        "Выберите деталь / узел:", 
        reply_markup=InlineKeyboardMarkup(inline_keyboard=builder)
    )
    await state.set_state(ReportForm.select_part)
    await callback.answer()

@router.callback_query(ReportForm.select_part, F.data.startswith("rep_part:"))
async def process_rep_part(callback: CallbackQuery, state: FSMContext):
    part_idx = int(callback.data.split(":")[1])
    
    user_data = await state.get_data()
    parts_list = user_data.get("parts_list", [])
    selected_part = parts_list[part_idx] if part_idx < len(parts_list) else "Деталь"
    
    await state.update_data(part=selected_part)
    
    stages = ["Готово", "Структурный ремонт"]
    builder = [[KeyboardButton(text=st)] for st in stages]
    await callback.message.answer(
        f"Выбрана деталь: {selected_part}\nВыберите выполненный этап:", 
        reply_markup=ReplyKeyboardMarkup(keyboard=builder, resize_keyboard=True)
    )
    await state.set_state(ReportForm.select_stage)
    await callback.answer()

@router.message(ReportForm.select_stage)
async def process_rep_stage(message: Message, state: FSMContext):
    await state.update_data(stage=message.text.strip())
    await message.answer("Напишите краткое описание выполненных работ:", reply_markup=ReplyKeyboardRemove())
    await state.set_state(ReportForm.enter_desc)

@router.message(ReportForm.enter_desc)
async def process_rep_desc(message: Message, state: FSMContext):
    await state.update_data(desc=message.text.strip())
    kb = [[InlineKeyboardButton(text="Пропустить фото ⏩", callback_data="skip_photo")]]
    await message.answer("Прикрепите фото работы (или нажмите 'Пропустить'):", reply_markup=InlineKeyboardMarkup(inline_keyboard=kb))
    await state.set_state(ReportForm.send_photo)

@router.callback_query(ReportForm.send_photo, F.data == "skip_photo")
async def process_skip_photo(callback: CallbackQuery, state: FSMContext):
    await state.update_data(photo="—")
    await ask_status_update(callback.message, state)
    await callback.answer()

@router.message(ReportForm.send_photo, F.photo)
async def process_rep_photo(message: Message, state: FSMContext):
    await state.update_data(photo=message.photo[-1].file_id)
    await ask_status_update(message, state)

async def ask_status_update(message: Message, state: FSMContext):
    statuses = ["В работе", "На сушке", "Готово", "Не менять"]
    builder = [[InlineKeyboardButton(text=s, callback_data=f"set_st:{s}")] for s in statuses]
    await message.answer("Изменить текущий статус детали в плане?", reply_markup=InlineKeyboardMarkup(inline_keyboard=builder))
    await state.set_state(ReportForm.update_status)

@router.callback_query(ReportForm.update_status, F.data.startswith("set_st:"))
async def process_finish_report(callback: CallbackQuery, state: FSMContext):
    new_status = callback.data.split(":")[1]
    data = await state.get_data()
    user_data = await asyncio.to_thread(get_user_data, callback.from_user.id)
    user_fio = user_data["fio"] if user_data else f"ID: {callback.from_user.id}"

    await asyncio.to_thread(add_report, user_fio, data['plane'], data['part'], data['stage'], data['desc'], data.get('photo', '—'))
    if new_status != "Не менять":
        await asyncio.to_thread(update_part_status, data['plane'], data['part'], new_status)
    
    is_admin = user_data.get("role") in ["Руководитель", "Админ"] if user_data else False
    await state.clear()
    await callback.message.answer("✅ **Отчет успешно сохранен!**", reply_markup=main_menu_keyboard(is_admin), parse_mode="Markdown")
    await callback.answer()


# --- ВЕБ-СЕРВЕР ДЛЯ РЕНДЕРА И ВЕБХУКОВ ---
async def handle_ping(request):
    return web.Response(text="Bot is running")

async def main():
    app = web.Application()
    app.router.add_get("/", handle_ping)
    runner = web.AppRunner(app)
    await runner.setup()
    port = int(os.environ.get("PORT", 8080))
    site = web.TCPSite(runner, "0.0.0.0", port)
    await site.start()
    
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())
