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

# --- РАБОТА С GOOGLE TABLES ---
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
    return sorted(list(set(str(r.get("Бортовой номер", "")).strip() for r in records if r.get("Бортовой номер"))))

def get_plane_specs(plane_num):
    ws_plan, _, _ = get_sheets()
    return [r for r in ws_plan.get_all_records() if str(r.get("Бортовой номер", "")).strip() == plane_num]

def parse_float(val):
    try:
        if not val:
            return 0.0
        return float(str(val).replace(',', '.').strip())
    except (ValueError, TypeError):
        return 0.0

def add_report(report_date, fio, plane, part, stage, qty, earned_hours, desc, photo_id="—"):
    _, ws_reports, _ = get_sheets()
    ws_reports.append_row([report_date, fio, plane, part, stage, qty, earned_hours, desc, photo_id])

def calculate_daily_hours(fio, report_date):
    _, ws_reports, _ = get_sheets()
    records = ws_reports.get_all_records()
    total = 0.0
    for r in records:
        if str(r.get("ФИО Маляра", "")).strip().lower() == fio.strip().lower():
            row_date = str(r.get("Дата и время", "")).split()[0]
            if row_date == report_date:
                total += parse_float(r.get("Заработано Н/ч", 0))
    return round(total, 2)

def update_part_status(plane, part, new_status):
    ws_plan, _, _ = get_sheets()
    records = ws_plan.get_all_records()
    for idx, r in enumerate(records, start=2):
        part_name = r.get("Деталь / Узел") or r.get("Компонент") or ""
        if str(r.get("Бортовой номер", "")).strip() == plane and str(part_name).strip() == part:
            ws_plan.update_cell(idx, 6, new_status)
            break

def get_user_reports_with_row(fio):
    _, ws_reports, _ = get_sheets()
    rows = ws_reports.get_all_values()
    if len(rows) < 2:
        return []
    
    user_reports = []
    for idx, r in enumerate(rows[1:], start=2):
        if len(r) >= 7:
            row_fio = str(r[1]).strip().lower()
            if row_fio == fio.strip().lower():
                user_reports.append({
                    "row_idx": idx,
                    "date": str(r[0]).split()[0],
                    "fio": r[1],
                    "plane": r[2],
                    "part": r[3],
                    "stage": r[4],
                    "qty": parse_float(r[5]),
                    "earned_nh": parse_float(r[6]),
                    "desc": r[7] if len(r) > 7 else "",
                    "photo": r[8] if len(r) > 8 else "—"
                })
    return user_reports


# --- ПОИСК ТМЦ ---
def search_tmc_items(query):
    sh = get_tmc_sheet()
    results = []
    query_lower = query.lower().strip()

    for ws in sh.worksheets():
        rows = ws.get_all_values()
        if len(rows) < 2:
            continue
        
        header = [str(c).lower().strip() for c in rows[0]]
        pn_col = 0
        for idx, cell in enumerate(header):
            if "p/n" in cell or "партийного" in cell:
                pn_col = idx
                break
        else:
            if ws.title.strip().lower() != "краски":
                pn_col = 1

        name_col = pn_col + 1
        chars_col = pn_col + 2
        qty_col = pn_col + 3
        store_col = pn_col + 4
        comment_col = 11 if pn_col == 0 else 12

        for r in rows[1:]:
            if not any(r): 
                continue
            pn = r[pn_col] if len(r) > pn_col else ""
            if not pn or pn.lower() in ["p/n", "название", "№", "грунты", "лаки", "спирты", "растворители", "структурные эмали"]:
                continue
            
            name = r[name_col] if len(r) > name_col else ""
            chars = r[chars_col] if len(r) > chars_col else ""
            qty = r[qty_col] if len(r) > qty_col else ""
            store = r[store_col] if len(r) > store_col else ""
            comment = r[comment_col] if len(r) > comment_col else ""
            
            full_str = f"{pn} {name} {chars} {store} {comment}".lower()
            
            if query_lower in full_str:
                results.append({
                    "category": ws.title,
                    "pn": pn or "Не указан",
                    "name": name or "—",
                    "chars": chars or "—",
                    "qty": qty or "—",
                    "store": store or "—",
                    "comment": comment or ""
                })

    return results

def get_tmc_categories_list():
    sh = get_tmc_sheet()
    return [ws.title for ws in sh.worksheets()]


# --- FSM СОСТОЯНИЯ ---
class Registration(StatesGroup):
    waiting_for_fio = State()

class ReportForm(StatesGroup):
    select_date = State()
    custom_date = State()
    select_plane = State()
    select_part = State()
    select_stage = State()
    select_qty = State()
    enter_desc = State()
    send_photo = State()
    update_status = State()
    batch_action = State()

class EditReportState(StatesGroup):
    waiting_new_qty = State()

class TMCSearchState(StatesGroup):
    waiting_for_query = State()


# --- КЛАВИАТУРЫ ---
def main_menu_keyboard(is_admin=False):
    kb = [
        [KeyboardButton(text="✈ План и краски по борту"), KeyboardButton(text="📦 Поиск ТМЦ / P/N")],
        [KeyboardButton(text="📝 Сдать отчет"), KeyboardButton(text="📋 Мои отчеты")],
        [KeyboardButton(text="👤 Мой профиль")]
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
async def start_tmc_search(message: Message):
    kb = [
        [InlineKeyboardButton(text="🔎 Текстовый поиск по слову / P/N", callback_data="tmc_mode_text")],
        [InlineKeyboardButton(text="📁 Показать все P/N по категориям", callback_data="tmc_mode_cats")]
    ]
    await message.answer("📦 **Раздел ТМЦ и Расходных материалов**\n\nВыберите способ просмотра:", reply_markup=InlineKeyboardMarkup(inline_keyboard=kb), parse_mode="Markdown")

@router.callback_query(F.data == "tmc_mode_text")
async def tmc_prompt_text(callback: CallbackQuery, state: FSMContext):
    await callback.message.answer("Введите слово для поиска, P/N или марку (например: *cockpit*, *профколор*, *аэрозольная*):")
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
    
    header = f"📋 **Найдено совпадений по запросу «{query}» ({len(results)}):**\n\n"
    current_msg = header
    messages = []
    
    for item in results:
        item_text = (
            f"🏷 **P/N:** `{item['pn']}`\n"
            f"📁 Категория: *{item['category']}*\n"
            f"📦 Наименование: {item['name']}\n"
            f"⚙ Характеристики: `{item['chars']}`\n"
            f"🛒 Магазин: {item['store']}\n"
        )
        if item['comment']:
            item_text += f"💬 Комментарий: {item['comment']}\n"
        item_text += "───────────────\n"
        
        if len(current_msg) + len(item_text) > 3500:
            messages.append(current_msg)
            current_msg = f"📋 **Результаты поиска (продолжение):**\n\n" + item_text
        else:
            current_msg += item_text
            
    if current_msg:
        messages.append(current_msg)
        
    for msg in messages:
        await message.answer(msg, parse_mode="Markdown")

@router.callback_query(F.data == "tmc_mode_cats")
async def tmc_show_categories(callback: CallbackQuery):
    cats = await asyncio.to_thread(get_tmc_categories_list)
    builder = [[InlineKeyboardButton(text=f"📁 {c}", callback_data=f"tmc_cat:{c}")] for c in cats]
    await callback.message.answer("Выберите категорию (вкладку) для вывода всех P/N:", reply_markup=InlineKeyboardMarkup(inline_keyboard=builder))
    await callback.answer()

@router.callback_query(F.data.startswith("tmc_cat:"))
async def tmc_show_category_items(callback: CallbackQuery):
    cat_name = callback.data.split(":", 1)[1]
    
    sh = await asyncio.to_thread(get_tmc_sheet)
    ws = await asyncio.to_thread(sh.worksheet, cat_name)
    rows = await asyncio.to_thread(ws.get_all_values)
    
    if len(rows) < 2:
        await callback.message.answer(f"В категории **{cat_name}** пока нет записей.", parse_mode="Markdown")
        await callback.answer()
        return
    
    header_row = [str(c).lower().strip() for c in rows[0]]
    pn_col = 0
    for idx, cell in enumerate(header_row):
        if "p/n" in cell or "партийного" in cell:
            pn_col = idx
            break
    else:
        if cat_name.strip().lower() != "краски":
            pn_col = 1

    pn_list = []
    for r in rows[1:]:
        if r and len(r) > pn_col:
            val = str(r[pn_col]).strip()
            if val and val.lower() not in ["№", "p/n (название партийного номера)", "p/n", "грунты", "лаки", "спирты", "растворители", "структурные эмали"]:
                pn_list.append(val)
                
    if not pn_list:
        await callback.message.answer(f"В категории **{cat_name}** нет заполненных P/N.", parse_mode="Markdown")
        await callback.answer()
        return
    
    header_text = f"📂 **Все P/N в категории «{cat_name}» (всего {len(pn_list)} шт.):**\n\n"
    current_msg = header_text
    messages = []
    
    for idx, pn in enumerate(pn_list, start=1):
        item_text = f"{idx}. `{pn}`\n"
        
        if len(current_msg) + len(item_text) > 3500:
            messages.append(current_msg)
            current_msg = f"📂 **Все P/N в категории «{cat_name}» (продолжение):**\n\n" + item_text
        else:
            current_msg += item_text
            
    if current_msg:
        messages.append(current_msg)
        
    for msg in messages:
        await callback.message.answer(msg, parse_mode="Markdown")
        
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
        part_name = str(item.get('Деталь / Узел') or item.get('Компонент') or '—').strip()
        paint = str(item.get('Номер / Марка краски') or item.get('p/n краски') or '—').strip()
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
    today = datetime.datetime.now().strftime("%d.%m.%Y")
    yesterday = (datetime.datetime.now() - datetime.timedelta(days=1)).strftime("%d.%m.%Y")
    
    kb = [
        [InlineKeyboardButton(text=f"📅 Сегодня ({today})", callback_data=f"rep_date:{today}")],
        [InlineKeyboardButton(text=f"📅 Вчера ({yesterday})", callback_data=f"rep_date:{yesterday}")],
        [InlineKeyboardButton(text="✏ Ввести дату вручную", callback_data="rep_date_custom")]
    ]
    await message.answer("📆 **За какой день сдается отчет?**", reply_markup=InlineKeyboardMarkup(inline_keyboard=kb), parse_mode="Markdown")
    await state.set_state(ReportForm.select_date)

@router.callback_query(ReportForm.select_date, F.data == "rep_date_custom")
async def prompt_custom_date(callback: CallbackQuery, state: FSMContext):
    await callback.message.answer("Введите дату отчета в формате **ДД.ММ.ГГГГ** (например: `25.09.2026`):", parse_mode="Markdown")
    await state.set_state(ReportForm.custom_date)
    await callback.answer()

@router.message(ReportForm.custom_date)
async def process_custom_date(message: Message, state: FSMContext):
    date_text = message.text.strip()
    try:
        valid_date = datetime.datetime.strptime(date_text, "%d.%m.%Y").strftime("%d.%m.%Y")
        await state.update_data(report_date=valid_date, batch_items=[])
        await ask_plane_for_report(message, state)
    except ValueError:
        await message.answer("❌ Неверный формат даты. Пожалуйста, введите дату в формате **ДД.ММ.ГГГГ** (например: `25.09.2026`):")

@router.callback_query(ReportForm.select_date, F.data.startswith("rep_date:"))
async def process_date_choice(callback: CallbackQuery, state: FSMContext):
    selected_date = callback.data.split(":", 1)[1]
    await state.update_data(report_date=selected_date, batch_items=[])
    await ask_plane_for_report(callback.message, state)
    await callback.answer()

async def ask_plane_for_report(message: Message, state: FSMContext):
    planes = await asyncio.to_thread(get_planes)
    if not planes:
        await message.answer("В плане нет активных самолетов.")
        return
    builder = [[InlineKeyboardButton(text=f"✈ {p}", callback_data=f"rep_plane:{p}")] for p in planes]
    await message.answer("Выберите бортовой номер самолета:", reply_markup=InlineKeyboardMarkup(inline_keyboard=builder))
    await state.set_state(ReportForm.select_plane)

@router.callback_query(ReportForm.select_plane, F.data.startswith("rep_plane:"))
async def process_rep_plane(callback: CallbackQuery, state: FSMContext):
    plane_num = callback.data.split(":")[1]
    specs = await asyncio.to_thread(get_plane_specs, plane_num)
    
    await state.update_data(plane=plane_num, specs=specs)
    
    builder = []
    for i, item in enumerate(specs):
        part_name = item.get('Деталь / Узел') or item.get('Компонент') or f"Деталь #{i+1}"
        builder.append([InlineKeyboardButton(text=f"🔧 {part_name}", callback_data=f"rep_part:{i}")])
    
    await callback.message.answer("Выберите деталь / узел:", reply_markup=InlineKeyboardMarkup(inline_keyboard=builder))
    await state.set_state(ReportForm.select_part)
    await callback.answer()

@router.callback_query(ReportForm.select_part, F.data.startswith("rep_part:"))
async def process_rep_part(callback: CallbackQuery, state: FSMContext):
    part_idx = int(callback.data.split(":")[1])
    user_data = await state.get_data()
    specs = user_data.get("specs", [])
    
    selected_spec = specs[part_idx] if part_idx < len(specs) else {}
    part_name = selected_spec.get('Деталь / Узел') or selected_spec.get('Компонент') or "Деталь"
    
    await state.update_data(part=part_name, selected_spec=selected_spec)
    
    stages = ["Готово", "Структурный ремонт"]
    builder = [[KeyboardButton(text=st)] for st in stages]
    
    await callback.message.answer(
        f"Выбрана деталь: **{part_name}**\nВыберите выполненный этап:", 
        reply_markup=ReplyKeyboardMarkup(keyboard=builder, resize_keyboard=True),
        parse_mode="Markdown"
    )
    await state.set_state(ReportForm.select_stage)
    await callback.answer()

@router.message(ReportForm.select_stage)
async def process_rep_stage(message: Message, state: FSMContext):
    stage = message.text.strip()
    await state.update_data(stage=stage)
    
    user_data = await state.get_data()
    spec = user_data.get("selected_spec", {})
    
    total_in_plan = int(parse_float(spec.get("Кол-во в раб.") or spec.get("Количество") or spec.get("Кол-во") or 24))
    
    builder = [
        [InlineKeyboardButton(text="1 шт", callback_data="qty:1"), InlineKeyboardButton(text="2 шт", callback_data="qty:2"), InlineKeyboardButton(text="5 шт", callback_data="qty:5")],
        [InlineKeyboardButton(text="10 шт", callback_data="qty:10"), InlineKeyboardButton(text="12 шт", callback_data="qty:12"), InlineKeyboardButton(text=f"Все ({total_in_plan} шт)", callback_data=f"qty:{total_in_plan}")]
    ]
    
    await message.answer(
        f"Укажите количество обработанных штук (выберите кнопку или **напишите число текстом**):", 
        reply_markup=InlineKeyboardMarkup(inline_keyboard=builder),
        parse_mode="Markdown"
    )
    await state.set_state(ReportForm.select_qty)

@router.callback_query(ReportForm.select_qty, F.data.startswith("qty:"))
async def process_rep_qty_callback(callback: CallbackQuery, state: FSMContext):
    qty = int(callback.data.split(":")[1])
    await state.update_data(qty=qty)
    await callback.message.answer("Напишите краткое описание выполненных работ:", reply_markup=ReplyKeyboardRemove())
    await state.set_state(ReportForm.enter_desc)
    await callback.answer()

@router.message(ReportForm.select_qty)
async def process_rep_qty_text(message: Message, state: FSMContext):
    text = message.text.strip()
    if not text.isdigit() or int(text) <= 0:
        await message.answer("Пожалуйста, введите целое положительное число штук (например: `12`):", parse_mode="Markdown")
        return
    
    qty = int(text)
    await state.update_data(qty=qty)
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
async def process_add_item_to_batch(callback: CallbackQuery, state: FSMContext):
    new_status = callback.data.split(":")[1]
    data = await state.get_data()
    
    user_data = await asyncio.to_thread(get_user_data, callback.from_user.id)
    user_fio = user_data["fio"] if user_data else f"ID: {callback.from_user.id}"

    spec = data.get("selected_spec", {})
    stage = data.get("stage", "")
    qty = data.get("qty", 1)
    
    struct_nh_1st = parse_float(spec.get("Н/ч Структ. ремонт, за 1 шт.") or spec.get("Н/ч Структ. ремонт"))
    paint_nh_1st = parse_float(spec.get("Н/ч Покраска за 1 шт.") or spec.get("Н/ч Покраска"))
    nh_per_1 = parse_float(spec.get("Н/ч на 1 изд.") or spec.get("Н/ч на 1 шт."))
    
    nh_total = parse_float(spec.get("Н/ч общ.") or spec.get("Н/ч всего"))
    total_qty_in_plan = parse_float(spec.get("Кол-во в раб.") or spec.get("Количество") or spec.get("Кол-во"))

    if nh_per_1 == 0 and nh_total > 0 and total_qty_in_plan > 0:
        nh_per_1 = nh_total / total_qty_in_plan

    if struct_nh_1st == 0:
        struct_nh_1st = nh_per_1 * 0.60
    if paint_nh_1st == 0:
        paint_nh_1st = nh_per_1 * 0.40

    if "структ" in stage.lower():
        earned_nh = round(struct_nh_1st * qty, 2)
    elif "готов" in stage.lower():
        earned_nh = round(nh_per_1 * qty, 2)
    else:
        earned_nh = round(paint_nh_1st * qty, 2)

    report_date = data.get("report_date", datetime.datetime.now().strftime("%d.%m.%Y"))

    await asyncio.to_thread(
        add_report, 
        report_date, 
        user_fio, 
        data['plane'], 
        data['part'], 
        stage, 
        qty, 
        earned_nh, 
        data['desc'], 
        data.get('photo', '—')
    )
    
    if new_status != "Не менять":
        await asyncio.to_thread(update_part_status, data['plane'], data['part'], new_status)
    
    batch = data.get("batch_items", [])
    batch.append({"part": data['part'], "qty": qty, "earned_nh": earned_nh})
    await state.update_data(batch_items=batch)
    
    kb = [
        [InlineKeyboardButton(text="➕ Добавить еще деталь (за эту дату)", callback_data="batch_add_more")],
        [InlineKeyboardButton(text="🏁 Завершить отчет и посчитать часы", callback_data="batch_finish")]
    ]
    
    msg_text = (
        f"✅ **Работа добавлена в отчет!**\n"
        f"🔧 {data['part']} ({qty} шт., {stage}) — **{earned_nh} н/ч**\n\n"
        f"Хотите добавить еще одну работу за дату **{report_date}**?"
    )
    
    await callback.message.answer(msg_text, reply_markup=InlineKeyboardMarkup(inline_keyboard=kb), parse_mode="Markdown")
    await state.set_state(ReportForm.batch_action)
    await callback.answer()

@router.callback_query(ReportForm.batch_action, F.data == "batch_add_more")
async def process_batch_add_more(callback: CallbackQuery, state: FSMContext):
    await ask_plane_for_report(callback.message, state)
    await callback.answer()

@router.callback_query(ReportForm.batch_action, F.data == "batch_finish")
async def process_batch_finish(callback: CallbackQuery, state: FSMContext):
    data = await state.get_data()
    user_data = await asyncio.to_thread(get_user_data, callback.from_user.id)
    user_fio = user_data["fio"] if user_data else f"ID: {callback.from_user.id}"
    report_date = data.get("report_date", datetime.datetime.now().strftime("%d.%m.%Y"))
    
    batch = data.get("batch_items", [])
    batch_total = sum(item["earned_nh"] for item in batch)
    
    total_daily_nh = await asyncio.to_thread(calculate_daily_hours, user_fio, report_date)
    
    is_admin = user_data.get("role") in ["Руководитель", "Админ"] if user_data else False
    await state.clear()
    
    congrat_msg = (
        f"🎉 **Отчет за {report_date} успешно сдан!**\n\n"
        f"📊 Добавлено работ в сессии: **{len(batch)}**\n"
        f"⏱ Начислено за этот отчет: **{round(batch_total, 2)} н/ч**\n\n"
        f"🥳 **Ура, ты молодец!** Всего за **{report_date}** у тебя: **{total_daily_nh} н/ч**! 🏆"
    )
    
    await callback.message.answer(congrat_msg, reply_markup=main_menu_keyboard(is_admin), parse_mode="Markdown")
    await callback.answer()


# --- РАЗДЕЛ "МОИ ОТЧЕТЫ" ---
@router.message(F.text == "📋 Мои отчеты")
async def show_my_reports(message: Message):
    user_data = await asyncio.to_thread(get_user_data, message.from_user.id)
    if not user_data:
        await message.answer("Сначала зарегистрируйтесь в системе.")
        return
    
    fio = user_data["fio"]
    reports = await asyncio.to_thread(get_user_reports_with_row, fio)
    
    if not reports:
        await message.answer("У вас пока нет сданных отчетов.")
        return
    
    await message.answer(f"📋 **Ваши последние отчеты ({len(reports[-10:])} шт.):**", parse_mode="Markdown")
    
    for r in reports[-10:]:
        text = (
            f"📅 Дата: `{r['date']}` | Борт: **{r['plane']}**\n"
            f"🔧 Деталь: {r['part']} ({r['qty']} шт.)\n"
            f"📌 Этап: {r['stage']} | ⏱ Заработано: **{r['earned_nh']} н/ч**\n"
            f"💬 {r['desc']}\n"
        )
        
        kb = [
            [
                InlineKeyboardButton(text="✏ Изменить кол-во", callback_data=f"edit_qty:{r['row_idx']}"),
                InlineKeyboardButton(text="❌ Удалить", callback_data=f"del_rep:{r['row_idx']}")
            ]
        ]
        
        await message.answer(text, reply_markup=InlineKeyboardMarkup(inline_keyboard=kb), parse_mode="Markdown")

@router.callback_query(F.data.startswith("del_rep:"))
async def process_delete_report(callback: CallbackQuery):
    row_idx = int(callback.data.split(":")[1])
    
    _, ws_reports, _ = await asyncio.to_thread(get_sheets)
    await asyncio.to_thread(ws_reports.delete_rows, row_idx)
    
    await callback.message.edit_text("🗑 **Отчет успешно удален из таблицы.**", parse_mode="Markdown")
    await callback.answer()

@router.callback_query(F.data.startswith("edit_qty:"))
async def process_prompt_edit_qty(callback: CallbackQuery, state: FSMContext):
    row_idx = int(callback.data.split(":")[1])
    await state.update_data(edit_row_idx=row_idx)
    
    await callback.message.answer("Введите новое правильное количество штук (например: `12`):", parse_mode="Markdown")
    await state.set_state(EditReportState.waiting_new_qty)
    await callback.answer()

@router.message(EditReportState.waiting_new_qty)
async def process_apply_new_qty(message: Message, state: FSMContext):
    text = message.text.strip()
    if not text.isdigit() or int(text) <= 0:
        await message.answer("Пожалуйста, введите положительное целое число:")
        return
    
    new_qty = int(text)
    data = await state.get_data()
    row_idx = data.get("edit_row_idx")
    
    _, ws_reports, _ = await asyncio.to_thread(get_sheets)
    row_data = await asyncio.to_thread(ws_reports.row_values, row_idx)
    
    if len(row_data) >= 7:
        old_qty = parse_float(row_data[5])
        old_nh = parse_float(row_data[6])
        
        per_item_nh = (old_nh / old_qty) if old_qty > 0 else 0
        new_nh = round(per_item_nh * new_qty, 2)
        
        await asyncio.to_thread(ws_reports.update_cell, row_idx, 6, new_qty)
        await asyncio.to_thread(ws_reports.update_cell, row_idx, 7, new_nh)
        
        await message.answer(f"✅ **Отчет обновлен!** Новое кол-во: **{new_qty} шт.**, зачислено: **{new_nh} н/ч**.", parse_mode="Markdown")
    else:
        await message.answer("Ошибка при обновлении строки отчета.")
        
    await state.clear()


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
    await callback.message.answer("Выберите сотрудника для просмотра отчета:", reply_markup=InlineKeyboardMarkup(inline_keyboard=builder))
    await callback.answer()

@router.callback_query(F.data.startswith("adm_usr:"))
async def admin_select_period(callback: CallbackQuery):
    fio = callback.data.split(":")[1]
    
    kb = [
        [InlineKeyboardButton(text="📅 За сегодня", callback_data=f"adm_p:today:{fio}")],
        [InlineKeyboardButton(text="🗓 За 7 дней", callback_data=f"adm_p:7days:{fio}")],
        [InlineKeyboardButton(text="📊 За этот месяц", callback_data=f"adm_p:month:{fio}")],
        [InlineKeyboardButton(text="📁 Все отчеты", callback_data=f"adm_p:all:{fio}")]
    ]
    await callback.message.answer(f"Выберите период для **{fio}**:", reply_markup=InlineKeyboardMarkup(inline_keyboard=kb), parse_mode="Markdown")
    await callback.answer()

@router.callback_query(F.data.startswith("adm_p:"))
async def admin_show_user_summary(callback: CallbackQuery):
    parts = callback.data.split(":", 2)
    period_type = parts[1]
    fio = parts[2]
    
    all_reports = await asyncio.to_thread(get_user_reports_with_row, fio)
    
    if not all_reports:
        await callback.message.answer(f"У сотрудника **{fio}** нет сданных отчетов.", parse_mode="Markdown")
        await callback.answer()
        return

    today_dt = datetime.datetime.now().date()
    filtered_reports = []

    for r in all_reports:
        try:
            r_dt = datetime.datetime.strptime(r['date'], "%d.%m.%Y").date()
        except ValueError:
            r_dt = today_dt

        if period_type == "today" and r_dt == today_dt:
            filtered_reports.append(r)
        elif period_type == "7days" and (today_dt - r_dt).days <= 7:
            filtered_reports.append(r)
        elif period_type == "month" and r_dt.month == today_dt.month and r_dt.year == today_dt.year:
            filtered_reports.append(r)
        elif period_type == "all":
            filtered_reports.append(r)

    if not filtered_reports:
        await callback.message.answer(f"У сотрудника **{fio}** нет отчетов за выбранный период.", parse_mode="Markdown")
        await callback.answer()
        return

    total_nh = round(sum(r['earned_nh'] for r in filtered_reports), 2)
    total_qty = int(sum(r['qty'] for r in filtered_reports))
    
    grouped = {}
    for r in filtered_reports:
        d = r['date']
        if d not in grouped:
            grouped[d] = []
        grouped[d].append(r)

    header = (
        f"📊 **СВОДНЫЙ ОТЧЕТ СОТРУДНИКА**\n"
        f"👤 **{fio}**\n\n"
        f"⏱ Всего наработано: **{total_nh} н/ч**\n"
        f"📦 Сдано элементов: **{total_qty} шт.**\n"
        f"───────────────────\n\n"
    )

    current_msg = header
    messages = []
    photos_list = []

    for d, items in grouped.items():
        day_nh = round(sum(i['earned_nh'] for i in items), 2)
        day_block = f"📅 **{d}** *(итого {day_nh} н/ч)*:\n"
        
        for item in items:
            day_block += f"• ✈ **{item['plane']}** | {item['part']} (`{int(item['qty'])} шт.` | *{item['stage']}*) — **{item['earned_nh']} н/ч**\n"
            if item['photo'] and item['photo'] != "—":
                photos_list.append((d, item['part'], item['photo']))
        
        day_block += "\n"

        if len(current_msg) + len(day_block) > 3500:
            messages.append(current_msg)
            current_msg = f"📊 **Сводный отчет {fio} (продолжение):**\n\n" + day_block
        else:
            current_msg += day_block

    if current_msg:
        messages.append(current_msg)

    for idx, msg in enumerate(messages):
        kb = None
        if idx == len(messages) - 1 and photos_list:
            kb = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text=f"📸 Показать фото ({len(photos_list)} шт.)", callback_data=f"show_ph:{fio}:{period_type}")]])
        await callback.message.answer(msg, reply_markup=kb, parse_mode="Markdown")

    await callback.answer()

@router.callback_query(F.data.startswith("show_ph:"))
async def admin_show_photos(callback: CallbackQuery):
    parts = callback.data.split(":", 2)
    period_type = parts[1]
    fio = parts[2]
    
    all_reports = await asyncio.to_thread(get_user_reports_with_row, fio)
    today_dt = datetime.datetime.now().date()
    
    await callback.message.answer(f"📸 **Загружаю фотографии отчетов {fio}...**", parse_mode="Markdown")
    
    for r in all_reports:
        try:
            r_dt = datetime.datetime.strptime(r['date'], "%d.%m.%Y").date()
        except ValueError:
            r_dt = today_dt

        match = False
        if period_type == "today" and r_dt == today_dt: match = True
        elif period_type == "7days" and (today_dt - r_dt).days <= 7: match = True
        elif period_type == "month" and r_dt.month == today_dt.month and r_dt.year == today_dt.year: match = True
        elif period_type == "all": match = True

        if match and r['photo'] and r['photo'] != "—":
            try:
                caption = f"📅 `{r['date']}` | Борт **{r['plane']}**\n🔧 {r['part']} ({int(r['qty'])} шт.)"
                await callback.message.answer_photo(r['photo'], caption=caption, parse_mode="Markdown")
            except Exception:
                pass

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
