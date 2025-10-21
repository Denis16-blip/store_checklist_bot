# app.py
import os
import json
import threading
import asyncio
from datetime import datetime

from flask import Flask, request, Response
from dotenv import load_dotenv

from telegram import Update, InlineKeyboardMarkup, InlineKeyboardButton
from telegram.ext import (
    Application, CommandHandler, CallbackQueryHandler, ContextTypes,
    MessageHandler, filters,
)
from telegram.error import BadRequest  # ← для безопасного edit

import httpx  # для прямых вызовов Telegram API (диагностика)

# ──────────────────────────────────────────────────────────────────────────────
# env & globals
# ──────────────────────────────────────────────────────────────────────────────
load_dotenv()

BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()
ADMIN_ID = int(os.getenv("TELEGRAM_ADMIN_ID", "0") or 0)
BASE_URL = os.getenv("BASE_URL", "").strip()

assert BOT_TOKEN, "BOT_TOKEN is required"

app = Flask(__name__)

# флаги/объекты фонового PTB
_ptb_thread: threading.Thread | None = None
_loop: asyncio.AbstractEventLoop | None = None
_app: Application | None = None
_loop_alive = False         # поток с loop создан
_ptb_ready = False          # Application.initialize() прошла

def log(msg: str):
    print(f"[{datetime.utcnow().isoformat(timespec='seconds')}Z] {msg}", flush=True)

# ──────────────────────────────────────────────────────────────────────────────
# ШАГ 1. МАГАЗИНЫ + РОЛИ (минимум)
# ──────────────────────────────────────────────────────────────────────────────

# Каталог магазинов: код -> человекочитаемое имя
STORE_CATALOG: dict[str, str] = {
    "C0TQ": "RU_MOSCOW_VegasKuncevo_SPORT",
    "C0SL": "RU_MOSCOW_Afimall_SPORT",
    "C022": "RU_MOSCOW_OkhotnyRyad_URBAN",
    "C0VU": "RU_MOSCOW_Metropolis_SPORT",
    "C0OI": "RU_MOSCOW_Kolumbus_SPORT",
    "C0GN": "RU_MOSCOW_MegaBelayaDacha_SPORT",
    "C0GJ": "RU_MOSCOW_MegaBelayaDacha_URBAN",
    "C047": "RU_MOSCOW_Vegas_SPORT",
    "C0VT": "RU_MOSCOW_Evropolis_SPORT",
    "C0TY": "RU_MOSCOW_KashirskayaPlaza_SPORT",
    "C0IZ": "RU_MYTISHCHI_MytishchiKrasnykit_SPORT",
    "C0DY": "RU_OBNINSK_TriumfPlaza_SPORT",
    "C0SM": "RU_TULA_Maksi_SPORT",
    "C09Z": "RU_KALUGA_RIO_SPORT",
    "C0NJ": "RU_MOSCOW_VegasSiti_SPORT",
    "C03F": "RU_IZHEVSK_Pushkinskaya_SPORT",
    "C0KH": "RU_YAROSLAVL_Aura_SPORT",
    "C0RG": "RU_ARKHANGELSK_TitanArena_SPORT",
    "C0OQ": "RU_SAINT-PETERSBURG_Leto_SPORT",
    "C08E": "RU_SAINT-PETERSBURG_Galereya_SPORT",
    "C0WF": "RU_PERM_Planeta_SPORT",
    "C0VB": "RU_OMSK_Mega_SPORT",
    "C00X": "RU_ABAKAN_Ametist_SPORT",
    "C0JP": "RU_IRKUTSK_ModnyKvartal_SPORT",
    "C00K": "RU_NOVOSIBIRSK_TTSAura_SPORT",
    "C0EI": "RU_SURGUT_Aura_SPORT",
    "C002": "RU_YUZHNO-SAKHALINSK_SitiMoll_SPORT",
    "C082": "RU_GELENDZHIK_Lenina_SPORT",
    "C0JN": "RU_KRASNODAR_Galereya_SPORT",
    "C0BW": "RU_KRASNODAR_OzMoll_SPORT",
    "C0VN": "RU_NOVOROSSIYSK_KrasnayaPloshchad_SPORT",
    "C081": "RU_SARATOV_TriumfMoll_SPORT",
    "C0WE": "RU_SOCHI_MoreMoll_SPORT",
    "C085": "RU_VORONEZH_GalereyaChizhova_SPORT",
    "C0WD": "RU_MOSCOW_PaveletskayaPlaza_SPORT",
    "C0VY": "RU_MOSCOW_KM7_SPORT",
    "C0LU": "RU_MOSCOW_Aviapark_SPORT",
    "C024": "RU_MOSCOW_KrasnayaPresnya_SPORT",
    "C25Q": "RU_MOSCOW_Salaris_Sport",
}

# Роли: auditor – заполняет чек-лист; viewer – только смотрит/получает отчёты
# Простейший in-memory реестр пользователей.
STAFF: dict[int, dict] = {
    # пример предзаписи для администратора:
    # ADMIN_ID: {"role": "auditor", "stores": list(STORE_CATALOG.keys()), "current_store": None}
}

def is_admin(user_id: int) -> bool:
    return ADMIN_ID and user_id == ADMIN_ID

def get_profile(user_id: int) -> dict:
    prof = STAFF.get(user_id)
    if not prof:
        prof = {"role": "viewer", "stores": [], "current_store": None}
        STAFF[user_id] = prof
    return prof

def must_have_store(update: Update, prof: dict) -> str | None:
    """Вернёт текст ошибки, если магазин не выбран/не разрешён."""
    if not prof.get("current_store"):
        return "Сначала выбери магазин: /stores → /setstore <КОД>"
    cur = prof["current_store"]
    if prof["stores"] and cur not in prof["stores"]:
        return "Текущий магазин не входит в твой список. Выбери другой: /setstore <КОД>"
    return None

# ──────────────────────────────────────────────────────────────────────────────
# ДАННЫЕ ЧЕК-ЛИСТА (из PPTX)
# ──────────────────────────────────────────────────────────────────────────────
CHECKLIST = [
    {
        "title": "1. ОБЩЕЕ РАЗМЕЩЕНИЕ АССОРТИМЕНТА",
        "items": [
            "Категории выстроены согласно утверждённому зонированию магазина.",
            "Коллекции внутри категории разделены по брендам и направлениям.",
            "Зоны перехода между коллекциями оформлены нейтрально, без визуального конфликта брендов.",
            "Планограммы актуальны и соответствуют наполнению.",
            "Баланс верхов и низов соблюдён.",
            "Развеска начинается с верхов; соблюдена комплектность при чередовании продукт-типов (длинный рукав / низы / короткий рукав).",
            "POSM размещены корректно: хедеры категорий и логотипы бренда установлены.",
            "Графика соответствует текущей кампании.",
            "Отсутствующий POSM — в заявке на заказ или замену.",
            "Устаревшие и повреждённые материалы удаляются немедленно.",
        ],
    },
    {
        "title": "2. КРОСС-МЕРЧАНДАЙЗИНГ И СТАЙЛИНГ",
        "items": [
            "Кросс-мерч (обувь, сумки, рюкзаки, шапки, кепки, фитнес-аксессуары) размещён корректно: соответствует бренду, категории и цвету.",
            "Кросс-мерч не перегружает стену или гондолу.",
            "На торцах гондол по потоку покупателей размещены актуальные аксессуары, соответствующие сезону и тематике зоны (спорт-зона/футбол/спортстиль-лайфстайл).",
            "Каждый второй фронт поддержан стайлингом или многослойным образом.",
            "Все ярлыки спрятаны.",
            "Крючки направлены по правилу «правой руки».",
            "Витрины и залы визуально сбалансированы по высоте, цвету и плотности экспозиции.",
        ],
    },
    {
        "title": "3. НАПОЛНЕННОСТЬ И ПОПОЛНЕНИЕ",
        "items": [
            "Текстиль размещён от меньшего размера к большему.",
            "Наполнение: текстиль — 6 ед. на артикул; верхняя одежда — 4 ед.; KM7 — текстиль 4 ед.; куртки — 2 ед.",
            "Лишние запасы не выносятся на зал.",
            "Обувь размещена от большей цены к меньшей (сверху вниз).",
            "Бренды чётко разделены по VM-инструкциям.",
            "Присутствуют протокольные размеры: женские — 5–6 UK; мужские — 8–9 UK.",
            "Все пары чистые, шнурки заправлены, ценники выровнены.",
        ],
    },
    {
        "title": "4. МАНЕКЕНЫ",
        "items": [
            "Луки соответствуют погодным условиям региона.",
            "Присутствует многослойность и цветовые акценты.",
            "Обувь соответствует сезону и спросу, выделены актуальные тренды.",
            "Каждый манекен закреплён за своей категорией.",
            "Все товары с манекенов доступны в зале в полной размерной горке.",
            "В луках есть бестселлеры магазина или региона.",
            "Образ завершён — аксессуары, кросс-мерч и цветовая логика соблюдены.",
        ],
    },
    {
        "title": "5. ВИТРИНА",
        "items": [
            "Концепт витрины соответствует актуальной кампании бренда.",
            "Витрина чистая, стекло без следов, декор не пыльный.",
            "POSM установлен по стандартам и маркетинговым активностям.",
            "Освещение акцентирует на графику и доп. инсталляции; нет пересветов, бликов и теней на стекле.",
            "При необходимости корректировки света — заявка в JIRA.",
        ],
    },
    {
        "title": "6. ЧИСТАЯ КАССОВАЯ ЗОНА",
        "items": [
            "На кассовом столе и в шкафу — только актуальные листовки и POS-материалы.",
            "Из товара на кассе — только бренд SOLMATE, без ценников на лицевой стороне.",
            "Аксессуарная зона соответствует сезону и спросу.",
            "Рюкзаки и сумки аккуратно набиты бумагой/наполнителем.",
        ],
    },
    {
        "title": "7. ОСВЕЩЕНИЕ",
        "items": [
            "Все лампы исправны и направлены корректно.",
            "Фокусные точки: входная экспозиция; фронты; каждое островное оборудование; крупные POSM; манекены.",
            "Проверка освещения — раз в неделю; при необходимости нацеливания — создать заявку в JIRA.",
        ],
    },
]

# ──────────────────────────────────────────────────────────────────────────────
# Примеры-фото для разделов (зашитые file_id)
# 0 — Общее размещение, 1 — Кросс-мерч, 2 — Наполненность, 3 — Манекены,
# 4 — Витрина (пока нет), 5 — Чистая касса, 6 — Освещение.
# ──────────────────────────────────────────────────────────────────────────────
EXAMPLE_PHOTOS: dict[int, list[str]] = {
    0: ["AgACAgIAAxkBAAN-aPc9fUdYqxNInDdLrh01UHckFW0AApL-MRvGH7hLzIOseULYaQ0BAAMCAAN4AAM2BA"],
    1: ["AgACAgIAAxkBAAN7aPc9WeexQm229VrzIW07tL18TccAAo3-MRvGH7hLuY3p8Zmreq8BAAMCAAN4AAM2BA"],
    2: ["AgACAgIAAxkBAAN9aPc9dabPgwhMuqDyMuCP52xNiZoAApH-MRvGH7hLayPbIRcX4O0BAAMCAAN4AAM2BA"],
    3: ["AgACAgIAAxkBAAN8aPc9bcea5a-h24wkS-zxpUBbdH4AApD-MRvGH7hLc0mtlweQiY4BAAMCAAN4AAM2BA"],
    5: ["AgACAgIAAxkBAAOAaPc9jBeS7KupdZWKttfeHrjT0YAAApT-MRvGH7hLYmedyzrqAAHaAQADAgADeAADNgQ"],
    6: ["AgACAgIAAxkBAAN_aPc9hXcYmK--YdH5wyJGthZp7kIAApP-MRvGH7hLalo9O7bUB34BAAMCAAN4AAM2BA"],
}

# ──────────────────────────────────────────────────────────────────────────────
# Состояние чек-листа
# ──────────────────────────────────────────────────────────────────────────────
_cl_state = {}  # chat_id -> {"sec": int, "marks": {sec: {item_index: bool|None}}}

def _cl_get(chat_id: int):
    st = _cl_state.get(chat_id)
    if not st:
        st = {"sec": 0, "marks": {}}
        _cl_state[chat_id] = st
    return st

def _human_sec_progress(st) -> tuple[int, int]:
    done = 0
    total = 0
    for si, sec in enumerate(CHECKLIST):
        total += len(sec["items"])
        sec_marks = st["marks"].get(si, {})
        for ii in range(len(sec["items"])):
            v = sec_marks.get(ii, None)
            if v is True:
                done += 1
    return done, total

def _fmt_section_text(si: int, st) -> str:
    sec = CHECKLIST[si]
    sec_marks = st["marks"].get(si, {})
    lines = [f"*{sec['title']}*"]
    for ii, text in enumerate(sec["items"]):
        mark = sec_marks.get(ii, None)
        sym = "✅" if mark is True else ("❌" if mark is False else "⬜️")
        lines.append(f"{ii+1}. {sym} {text}")
    done, total = _human_sec_progress(st)
    pct = int(round(100 * done / total)) if total else 0
    lines.append("")
    lines.append(f"Прогресс: *{done}/{total}* ({pct}%)")
    lines.append("_Нажимай на кнопки с номерами, чтобы переключать ⬜️→✅→❌._")
    return "\n".join(lines)

def _kb_section(si: int, st):
    sec = CHECKLIST[si]
    sec_marks = st["marks"].get(si, {})
    rows = []
    for ii in range(len(sec["items"])):
        v = sec_marks.get(ii, None)
        sym = "✅" if v is True else ("❌" if v is False else "⬜️")
        label = f"{ii+1} {sym}"
        rows.append([InlineKeyboardButton(label, callback_data=f"cl:toggle:{ii}")])

    controls = [
        InlineKeyboardButton("➡ Далее", callback_data="cl:next"),
        InlineKeyboardButton("↩ Пропустить секцию", callback_data="cl:skip"),
    ]
    rows.append(controls)

    extras = [InlineKeyboardButton("📋 Прогресс", callback_data="cl:progress")]
    if si in EXAMPLE_PHOTOS:
        extras.insert(0, InlineKeyboardButton("📷 Пример", callback_data="cl:photo"))
    rows.append(extras)

    rows.append([InlineKeyboardButton("♻️ Сброс секции", callback_data="cl:resetsec")])
    return InlineKeyboardMarkup(rows)

def _fmt_progress_text(st) -> str:
    done, total = _human_sec_progress(st)
    pct = int(round(100 * done / total)) if total else 0
    lines = [f"Готово: *{done}/{total}* ({pct}%)"]
    for si, sec in enumerate(CHECKLIST):
        sec_total = len(sec["items"])
        sec_done = sum(1 for ii in range(sec_total) if st["marks"].get(si, {}).get(ii) is True)
        tick = "✅" if sec_done == sec_total and sec_total > 0 else ("➖" if sec_done else "⬜️")
        lines.append(f"{tick} {sec['title']} — {sec_done}/{sec_total}")
    return "\n".join(lines)

# ──────────────────────────────────────────────────────────────────────────────
# Безопасное редактирование сообщения (игнорируем 'Message is not modified')
# ──────────────────────────────────────────────────────────────────────────────
async def _safe_edit(q, text: str, reply_markup=None, parse_mode: str | None = "Markdown"):
    try:
        await q.edit_message_text(text=text, reply_markup=reply_markup, parse_mode=parse_mode)
    except BadRequest as e:
        if "Message is not modified" in str(e):
            try:
                await q.answer("Без изменений")
            except Exception:
                pass
            return
        raise

# ──────────────────────────────────────────────────────────────────────────────
# Команды: роли и магазины
# ──────────────────────────────────────────────────────────────────────────────
async def cmd_whoami(update: Update, context: ContextTypes.DEFAULT_TYPE):
    u = update.effective_user
    prof = get_profile(u.id)
    cur = prof.get("current_store")
    cur_name = STORE_CATALOG.get(cur, "—") if cur else "—"
    await update.effective_chat.send_message(
        f"🧾 Профиль\n"
        f"ID: `{u.id}`\n"
        f"Роль: *{prof['role']}*\n"
        f"Магазин: *{cur or '—'}* — {cur_name}\n"
        f"Доступные магазины: {', '.join(prof['stores']) if prof['stores'] else 'не ограничено'}",
        parse_mode="Markdown",
    )

async def cmd_stores(update: Update, context: ContextTypes.DEFAULT_TYPE):
    lines = ["*Коды магазинов:*"]
    for code, name in sorted(STORE_CATALOG.items()):
        lines.append(f"`{code}` — {name}")
    await update.effective_chat.send_message("\n".join(lines), parse_mode="Markdown")

async def cmd_setstore(update: Update, context: ContextTypes.DEFAULT_TYPE):
    u = update.effective_user
    prof = get_profile(u.id)
    if not context.args:
        await update.effective_chat.send_message("Используй: /setstore <КОД> (см. /stores)")
        return
    code = context.args[0].strip().upper()
    if code not in STORE_CATALOG:
        await update.effective_chat.send_message("Неизвестный код магазина. Список: /stores")
        return
    # если список магазинов ограничен, проверим доступ
    if prof["stores"] and code not in prof["stores"]:
        await update.effective_chat.send_message("Этот магазин тебе не назначен. Обратись к администратору.")
        return
    prof["current_store"] = code
    await update.effective_chat.send_message(f"Ок! Текущий магазин: *{code}* — {STORE_CATALOG[code]}", parse_mode="Markdown")

async def cmd_setrole(update: Update, context: ContextTypes.DEFAULT_TYPE):
    u = update.effective_user
    if not is_admin(u.id):
        await update.effective_chat.send_message("Команда только для администратора.")
        return
    if not context.args:
        await update.effective_chat.send_message("Используй: /setrole <auditor|viewer> [@username|user_id]")
        return
    role = context.args[0].lower()
    if role not in ("auditor", "viewer"):
        await update.effective_chat.send_message("Роль должна быть auditor или viewer.")
        return
    target_id = u.id
    if len(context.args) >= 2:
        # простая попытка распарсить user_id
        try:
            target_id = int(context.args[1].replace("@", ""))
        except Exception:
            await update.effective_chat.send_message("Укажи numeric user_id (пока без @username).")
            return
    prof = get_profile(target_id)
    prof["role"] = role
    await update.effective_chat.send_message(f"Роль пользователя {target_id} установлена: *{role}*", parse_mode="Markdown")

# ──────────────────────────────────────────────────────────────────────────────
# Бизнес-логика бота
# ──────────────────────────────────────────────────────────────────────────────
async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    kb = [[
        InlineKeyboardButton("Проверка", callback_data="ping"),
        InlineKeyboardButton("Чек-лист", callback_data="cl:start"),
    ]]
    await update.effective_chat.send_message(
        "Привет! Выбери магазин командой /setstore <КОД> (список: /stores). "
        "Админ выдаёт роль `/setrole auditor <user_id>`.\nЗатем жми «Чек-лист».",
        reply_markup=InlineKeyboardMarkup(kb),
        parse_mode="Markdown",
    )

async def on_button(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    if not q or not q.data:
        return
    if q.data == "ping":
        await q.answer("pong")
        try:
            await q.edit_message_text("Кнопка работает ✅")
        except BadRequest as e:
            if "Message is not modified" in str(e):
                await q.answer("Без изменений")
            else:
                raise
        return
    if q.data.startswith("cl:"):
        await cl_callback(update, context)
        return

# ───────────── Чек-лист блочно ─────────────
async def cmd_checklist(update: Update, context: ContextTypes.DEFAULT_TYPE):
    # ручной вход (если кто-то вызовет /checklist)
    u = update.effective_user
    prof = get_profile(u.id)
    if prof["role"] != "auditor":
        await update.effective_chat.send_message("Твоя роль — viewer. Для прохождения чек-листа нужна роль auditor.")
        return
    err = must_have_store(update, prof)
    if err:
        await update.effective_chat.send_message(err)
        return

    chat_id = update.effective_chat.id
    st = _cl_get(chat_id)
    si = st["sec"]
    await update.effective_chat.send_message(
        _fmt_section_text(si, st),
        reply_markup=_kb_section(si, st),
        parse_mode="Markdown",
    )

async def cl_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    u = q.from_user
    prof = get_profile(u.id)

    # доступ к чек-листу только для auditor и с выбранным магазином
    if prof["role"] != "auditor":
        await q.answer("Недостаточно прав", show_alert=True)
        return
    err = must_have_store(update, prof)
    if err:
        await q.answer(err, show_alert=True)
        return

    chat_id = q.message.chat_id
    st = _cl_get(chat_id)

    action = q.data.split(":", 1)[1]  # всё после "cl:"
    si = st["sec"]

    if action == "start":
        st["sec"] = 0
        st["marks"] = {}
        si = 0
        await q.answer(f"Поехали! Магазин: {prof.get('current_store')}")
        await _safe_edit(q, _fmt_section_text(si, st), reply_markup=_kb_section(si, st))
        return

    if action == "photo":
        files = EXAMPLE_PHOTOS.get(si)
        if files:
            try:
                await q.message.chat.send_photo(
                    photo=files[0],
                    caption=f"Пример для секции: {CHECKLIST[si]['title']}"
                )
            except Exception as e:
                log(f"send_photo error: {e}")
                await q.answer("Не удалось отправить фото", show_alert=True)
                return
            await q.answer("Пример отправлен")
        else:
            await q.answer("Для этой секции пока нет примера", show_alert=True)
        return

    if action.startswith("toggle:"):
        ii = int(action.split(":")[1])
        sec_marks = st["marks"].setdefault(si, {})
        cur = sec_marks.get(ii, None)
        nxt = True if cur is None else (False if cur is True else None)
        sec_marks[ii] = nxt
        await q.answer("Обновлено")
        await _safe_edit(q, _fmt_section_text(si, st), reply_markup=_kb_section(si, st))
        return

    if action == "resetsec":
        st["marks"][si] = {}
        await q.answer("Секция сброшена")
        await _safe_edit(q, _fmt_section_text(si, st), reply_markup=_kb_section(si, st))
        return

    if action == "progress":
        await q.answer("Прогресс")
        await _safe_edit(q, _fmt_progress_text(st) + "\n\nНажми «➡ Далее», чтобы продолжить.",
                         reply_markup=_kb_section(si, st))
        return

    if action == "skip":
        st["sec"] = min(st["sec"] + 1, len(CHECKLIST) - 1)
        si = st["sec"]

    if action == "next":
        if si >= len(CHECKLIST) - 1:
            text = "🎉 Чек-лист завершён!\n\n" + _fmt_progress_text(st)
            await _safe_edit(q, text)
            return
        st["sec"] += 1
        si = st["sec"]

    await _safe_edit(q, _fmt_section_text(si, st), reply_markup=_kb_section(si, st))

# ──────────────────────────────────────────────────────────────────────────────
# Регистрация хэндлеров
# ──────────────────────────────────────────────────────────────────────────────
def build_application() -> Application:
    app_ = Application.builder().token(BOT_TOKEN).build()

    # Команды
    app_.add_handler(CommandHandler("start", cmd_start))
    app_.add_handler(CommandHandler("checklist", cmd_checklist))
    app_.add_handler(CommandHandler("whoami", cmd_whoami))
    app_.add_handler(CommandHandler("stores", cmd_stores))
    app_.add_handler(CommandHandler("setstore", cmd_setstore))
    app_.add_handler(CommandHandler("setrole", cmd_setrole))

    # Кнопки
    app_.add_handler(CallbackQueryHandler(on_button))
    app_.add_handler(CallbackQueryHandler(cl_callback, pattern=r"^cl:"))

    return app_

# ──────────────────────────────────────────────────────────────────────────────
# Фоновый поток с отдельным asyncio-loop
# ──────────────────────────────────────────────────────────────────────────────
async def _ptb_init_async():
    """Создать Application и выполнить initialize()."""
    global _app, _ptb_ready
    log("PTB: build application…")
    _app = build_application()
    log("PTB: application.initialize()…")
    await _app.initialize()
    _ptb_ready = True
    log("PTB: READY")

def _ptb_thread_main():
    """Запустить свой event loop и инициализировать PTB внутри него."""
    global _loop, _loop_alive
    _loop = asyncio.new_event_loop()
    asyncio.set_event_loop(_loop)
    _loop_alive = True
    log("PTB thread: loop created, initializing…")
    try:
        _loop.run_until_complete(_ptb_init_async())
        _loop.run_forever()
    except Exception as e:
        log(f"PTB thread ERROR: {e}")
    finally:
        _loop_alive = False
        log("PTB thread: exit")

def ensure_ptb_started():
    global _ptb_thread
    if _ptb_thread and _ptb_thread.is_alive():
        return
    _ptb_thread = threading.Thread(target=_ptb_thread_main, name="ptb-thread", daemon=True)
    _ptb_thread.start()
    log("PTB thread: started")

# ──────────────────────────────────────────────────────────────────────────────
# Flask routes
# ──────────────────────────────────────────────────────────────────────────────
@app.route("/health")
def health():
    return "ok", 200

@app.route("/_loop")
def loop_state():
    return f"loop_alive={_loop_alive}, is_running={bool(_loop and _loop.is_running())}", 200

@app.route("/diag")
def diag():
    total = sum(len(s["items"]) for s in CHECKLIST)
    info = {
        "loop_alive": _loop_alive,
        "loop_is_running": bool(_loop and _loop.is_running()),
        "ptb_ready": _ptb_ready,
        "has_application": _app is not None,
        "now": datetime.utcnow().isoformat(timespec="seconds") + "Z",
        "checklist_total_items": total,
        "sections": len(CHECKLIST),
        "stores": len(STORE_CATALOG),
        "staff_records": len(STAFF),
    }
    return app.response_class(json.dumps(info, ensure_ascii=False, indent=2), mimetype="application/json")

@app.route("/getwebhookinfo_raw")
def getwebhookinfo_raw():
    try:
        r = httpx.get(f"https://api.telegram.org/bot{BOT_TOKEN}/getWebhookInfo", timeout=10)
        return app.response_class(r.text, mimetype="application/json", status=r.status_code)
    except Exception as e:
        return f"error: {e}", 500

@app.route("/set-webhook")
def set_webhook():
    target = BASE_URL.rstrip("/") + "/"
    try:
        r = httpx.get(
            f"https://api.telegram.org/bot{BOT_TOKEN}/setWebhook",
            params={"url": target},
            timeout=15,
        )
        log(f"setWebhook → {r.status_code} {r.text[:200]}")
        return f"Webhook set to {target}", 200
    except Exception as e:
        log(f"setWebhook ERROR: {e}")
        return f"error: {e}", 500

@app.post("/")
def telegram_webhook():
    if not (_loop_alive and _ptb_ready and _app and _loop):
        log("webhook → loop not ready (503)")
        return Response("loop not ready", status=503)

    try:
        data = request.get_json(force=True, silent=False)
        upd = Update.de_json(data, _app.bot)
        fut = asyncio.run_coroutine_threadsafe(_app.process_update(upd), _loop)
        fut.add_done_callback(lambda f: None)
        return "ok", 200
    except Exception as e:
        log(f"webhook ERROR: {e}")
        return Response("internal error", status=500)

# ──────────────────────────────────────────────────────────────────────────────
# App startup
# ──────────────────────────────────────────────────────────────────────────────
@app.before_request
def _before_any():
    ensure_ptb_started()

if __name__ == "__main__":
    ensure_ptb_started()
    app.run(host="0.0.0.0", port=int(os.getenv("PORT", "5000")))

