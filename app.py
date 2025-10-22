# app.py — чек-лист + саморегистрация с модерацией админом
import os
import json
import threading
import asyncio
from datetime import datetime
from pathlib import Path
import html  # для экранирования в HTML

from flask import Flask, request, Response
from dotenv import load_dotenv

from telegram import Update, InlineKeyboardMarkup, InlineKeyboardButton, BotCommand, BotCommandScopeChat
from telegram.ext import (
    Application, CommandHandler, CallbackQueryHandler, ContextTypes,
)
from telegram.error import BadRequest
import httpx

# ──────────────────────────────────────────────────────────────────────────────
# env & globals
# ──────────────────────────────────────────────────────────────────────────────
load_dotenv()

BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()
ADMIN_ID = int(os.getenv("TELEGRAM_ADMIN_ID", "0") or 0)
BASE_URL = os.getenv("BASE_URL", "").strip()

AUDITOR_SECRET = os.getenv("AUDITOR_SECRET", "").strip()
VIEWER_SECRET  = os.getenv("VIEWER_SECRET", "").strip()

assert BOT_TOKEN, "BOT_TOKEN is required"

app = Flask(__name__)

# Фон для PTB
_ptb_thread: threading.Thread | None = None
_loop: asyncio.AbstractEventLoop | None = None
_app: Application | None = None
_loop_alive = False
_ptb_ready = False
BOT_USERNAME = None  # подхватим в init()

def log(msg: str):
    print(f"[{datetime.utcnow().isoformat(timespec='seconds')}Z] {msg}", flush=True)

# ──────────────────────────────────────────────────────────────────────────────
# Магазины + роли + персист
# ──────────────────────────────────────────────────────────────────────────────
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
    "C00K": "RU_NOVОSIBIRSK_TTSAura_SPORT",
    "C0EI": "RU_SURGUT_Aura_SPORT",
    "C002": "RU_YUZHNO-SAKHALINSK_SitiMoll_SPORT",
    "C082": "RU_GELENDZHIK_Lenina_SPORT",
    "C0JN": "RU_KRASNODAR_Galereya_SPORT",
    "C0BW": "RU_KRASNODАР_OzMoll_SPORT",
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

DATA_DIR = Path(os.getenv("DATA_DIR", "data"))
STAFF_FILE = DATA_DIR / "staff.json"
PENDING_FILE = DATA_DIR / "pending.json"

def _read_json(path: Path, default):
    try:
        if path.exists():
            return json.loads(path.read_text(encoding="utf-8"))
    except Exception as e:
        log(f"read {path.name} error: {e}")
    return default

def _write_json(path: Path, data):
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    except Exception as e:
        log(f"write {path.name} error: {e}")

# staff: {user_id: {role, stores, current_store, username, name}}
STAFF: dict[int, dict] = {int(k): v for k, v in _read_json(STAFF_FILE, {}).items()}
# pending: {req_id: {user_id, store, role, username, name, ts}}
PENDING: dict[str, dict] = _read_json(PENDING_FILE, {})

def _save_staff(): _write_json(STAFF_FILE, {str(k): v for k, v in STAFF.items()})
def _save_pending(): _write_json(PENDING_FILE, PENDING)

def is_admin(uid: int) -> bool: return ADMIN_ID and uid == ADMIN_ID

def get_profile(uid: int) -> dict:
    prof = STAFF.get(uid)
    if not prof:
        prof = {"role": "viewer", "stores": [], "current_store": None, "username": "", "name": ""}
        STAFF[uid] = prof
        _save_staff()
    return prof

def _upd_from_user(user, prof):
    prof["username"] = user.username or ""
    prof["name"] = f"{user.first_name or ''} {user.last_name or ''}".strip()

def must_have_store(update: Update, prof: dict) -> str | None:
    if not prof.get("current_store"):
        return "Сначала выбери магазин: /stores → /setstore <КОД> или /register <КОД> <СЕКРЕТ>"
    cur = prof["current_store"]
    if prof["stores"] and cur not in prof["stores"]:
        return "Текущий магазин не входит в твой список. Выбери другой: /setstore <КОД>"
    return None

# ──────────────────────────────────────────────────────────────────────────────
# Меню команд по ролям (Bot Menu) + helper для обновления
# ──────────────────────────────────────────────────────────────────────────────
ROLE_COMMANDS: dict[str, list[BotCommand]] = {
    "viewer": [
        BotCommand("start", "начать"),
        BotCommand("register", "запросить доступ (код+секрет)"),
        BotCommand("whoami", "профиль"),
        BotCommand("stores", "список магазинов"),
        BotCommand("setstore", "выбрать магазин"),
        BotCommand("viewer", "что может viewer"),
    ],
    "auditor": [
        BotCommand("start", "начать"),
        BotCommand("register", "запросить доступ (код+секрет)"),
        BotCommand("whoami", "профиль"),
        BotCommand("stores", "список магазинов"),
        BotCommand("setstore", "выбрать магазин"),
        BotCommand("checklist", "чек-лист"),
        BotCommand("auditor", "что может auditor"),  # ← заменили viewer→auditor
    ],
    "admin": [
        BotCommand("start", "начать"),
        BotCommand("register", "запросить доступ (код+секрет)"),
        BotCommand("whoami", "профиль"),
        BotCommand("stores", "список магазинов"),
        BotCommand("setstore", "выбрать магазин"),
        BotCommand("checklist", "чек-лист"),
        BotCommand("pending", "заявки на модерацию"),
        BotCommand("setrole", "назначить роль"),
        BotCommand("bindings", "кто за что"),        # ← новая команда
        BotCommand("admin", "что может admin"),
    ],
}

def _role_for_display(uid: int, prof: dict) -> str:
    return "admin" if is_admin(uid) else prof.get("role", "viewer")

async def refresh_chat_commands(bot, chat_id: int, user_id: int):
    """Обновляет список команд (над клавиатурой) под роль пользователя в данном чате."""
    prof = get_profile(user_id)
    role = _role_for_display(user_id, prof)
    commands = ROLE_COMMANDS.get(role, ROLE_COMMANDS["viewer"])
    try:
        await bot.set_my_commands(commands=commands, scope=BotCommandScopeChat(chat_id))
    except Exception as e:
        log(f"set_my_commands error for chat {chat_id}: {e}")

# ──────────────────────────────────────────────────────────────────────────────
# Чек-лист и фото
# ──────────────────────────────────────────────────────────────────────────────
CHECKLIST = [
    {"title": "1. ОБЩЕЕ РАЗМЕЩЕНИЕ АССОРТИМЕНТА", "items": [
        "Категории выстроены согласно утверждённому зонированию магазина.",
        "Коллекции внутри категории разделены по брендам и направлениям.",
        "Зоны перехода между коллекциями оформлены нейтрально, без визуального конфликта брендов.",
        "Планограммы актуальны и соответствуют наполнению.",
        "Баланс верхов и низов соблюдён.",
        "Развеска начинается с верхов; соблюдена комплектность при чередовании продукт-типов.",
        "POSM размещены корректно, графика соответствует текущей кампании.",
        "Отсутствующий/устаревший POSM — в заявке.",
    ]},
    {"title": "2. КРОСС-МЕРЧАНДАЙЗИНГ И СТАЙЛИНГ", "items": [
        "Кросс-мерч соответствует бренду/категории/цвету и не перегружает гондолу.",
        "На торцах — актуальные аксессуары по сезону/зонам.",
        "Поддержан стайлинг/многослойность, ярлыки спрятаны.",
        "Крючки по правилу «правой руки».",
        "Экспозиция сбалансирована по высоте/цвету/плотности.",
    ]},
    {"title": "3. НАПОЛНЕННОСТЬ И ПОПОЛНЕНИЕ", "items": [
        "Текстиль от меньшего размера к большему; нормативы по единицам соблюдены.",
        "Лишние запасы не на зале.",
        "Обувь сверху вниз — от большей цены к меньшей, протокольные размеры присутствуют.",
        "Пары чистые, шнурки заправлены, ценники выровнены.",
    ]},
    {"title": "4. МАНЕКЕНЫ", "items": [
        "Луки по погоде, есть цветовые акценты/многослойность.",
        "Обувь по сезону/спросу, выделены тренды.",
        "Манекены закреплены за категориями, товары доступны в зале, есть бестселлеры.",
        "Образ завершён — аксессуары/кросс-мерч/цветовая логика.",
    ]},
    {"title": "5. ВИТРИНА", "items": [
        "Концепт соответствует кампании, стекло чистое, декор не пыльный.",
        "POSM по стандартам, свет без пересветов/бликов/теней.",
        "Корректировка света — заявка в JIRA.",
    ]},
    {"title": "6. ЧИСТАЯ КАССОВАЯ ЗОНА", "items": [
        "На кассе только актуальные POS-материалы; товар — только SOLMATE.",
        "Аксессуарная зона по сезону/спросу; рюкзаки/сумки набиты.",
    ]},
    {"title": "7. ОСВЕЩЕНИЕ", "items": [
        "Лампы исправны и направлены корректно.",
        "Фокус: вход, фронты, острова, крупные POSM, манекены.",
        "Проверка света — раз в неделю; при необходимости — заявка в JIRA.",
    ]},
]

EXAMPLE_PHOTOS = {
    0: ["AgACAgIAAxkBAAN-aPc9fUdYqxNInDdLrh01UHckFW0AApL-MRvGH7hLzIOseULYaQ0BAAMCAAN4AAM2BA"],  # Общее размещение
    1: ["AgACAgIAAxkBAAN7aPc9WeexQm229VrzIW07tL18TccAAo3-MRvGH7hLuY3p8Zmreq8BAAMCAAN4AAM2BA"],  # Кросс-мерч
    2: ["AgACAgIAAxkBAAN9aPc9dabPgwhMuqDyMuCP52xNiZoAApH-MRvGH7hLayPbIRcX4O0BAAMCAAN4AAM2BA"],  # Наполненность
    3: ["AgACAgIAAxkBAAN8aPc9bcea5a-h24wkS-zxpUBbdH4AApD-MRvGH7hLc0mtlweQiY4BAAMCAAN4AAM2BA"],  # Манекены
    5: ["AgACAgIAAxkBAAOAaPc9jBeS7KupdZWKttfeHrjT0YAAApT-MRvGH7hLYmedyzrqAAHaAQADAgADeAADNgQ"],  # Касса
    6: ["AgACAgIAAxkBAAN_aPc9hXcYmK--YdH5wyJGthZp7kIAApP-MRvGH7hLalo9O7bUB34BAAMCAAN4AAM2BA"],  # Освещение
}

_cl_state = {}  # chat_id -> {"sec": int, "marks": {sec: {item: bool|None}}}

def _cl_get(cid: int):
    st = _cl_state.get(cid)
    if not st:
        st = {"sec": 0, "marks": {}}
        _cl_state[cid] = st
    return st

def _human_sec_progress(st) -> tuple[int, int]:
    done = 0
    total = 0
    for si, sec in enumerate(CHECKLIST):
        total += len(sec["items"])
        sec_marks = st["marks"].get(si, {})
        for ii in range(len(sec["items"])):
            if sec_marks.get(ii) is True:
                done += 1
    return done, total

def _fmt_section_text(si: int, st) -> str:
    sec = CHECKLIST[si]
    sec_marks = st["marks"].get(si, {})
    lines = [f"*{sec['title']}*"]
    for ii, text in enumerate(sec["items"]):
        mark = sec_marks.get(ii)
        sym = "✅" if mark is True else ("❌" if mark is False else "⬜️")
        lines.append(f"{ii+1}. {sym} {text}")
    done, total = _human_sec_progress(st)
    pct = int(round(100*done/total)) if total else 0
    lines += ["", f"Прогресс: *{done}/{total}* ({pct}%)", "_Нажимай на номера, чтобы ⬜️→✅→❌._"]
    return "\n".join(lines)

def _kb_section(si: int, st):
    sec = CHECKLIST[si]
    sec_marks = st["marks"].get(si, {})
    rows = []
    for ii in range(len(sec["items"])):
        v = sec_marks.get(ii)
        sym = "✅" if v is True else ("❌" if v is False else "⬜️")
        rows.append([InlineKeyboardButton(f"{ii+1} {sym}", callback_data=f"cl:toggle:{ii}")])
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
    pct = int(round(100*done/total)) if total else 0
    lines = [f"Готово: *{done}/{total}* ({pct}%)"]
    for si, sec in enumerate(CHECKLIST):
        sec_total = len(sec["items"])
        sec_done = sum(1 for ii in range(sec_total) if st["marks"].get(si, {}).get(ii) is True)
        tick = "✅" if sec_done == sec_total and sec_total > 0 else ("➖" if sec_done else "⬜️")
        lines.append(f"{tick} {sec['title']} — {sec_done}/{sec_total}")
    return "\n".join(lines)

async def _safe_edit(q, text: str, reply_markup=None, parse_mode: str | None = "Markdown"):
    try:
        await q.edit_message_text(text=text, reply_markup=reply_markup, parse_mode=parse_mode)
    except BadRequest as e:
        if "Message is not modified" in str(e):
            try:
                await q.answer("Без изменений")
            except Exception:
                pass
        else:
            raise

# ──────────────────────────────────────────────────────────────────────────────
# Регистрация с модерацией
# ──────────────────────────────────────────────────────────────────────────────
def _gen_req_id(user_id: int) -> str:
    ts = datetime.utcnow().strftime("%Y%m%d%H%M%S")
    return f"R{ts}_{user_id}"

def _role_from_secret(secret: str) -> str | None:
    if AUDITOR_SECRET and secret == AUDITOR_SECRET:
        return "auditor"
    if VIEWER_SECRET and secret == VIEWER_SECRET:
        return "viewer"
    return None

async def _notify_admin_new(context: ContextTypes.DEFAULT_TYPE, req_id: str):
    if not ADMIN_ID:
        return
    r = PENDING[req_id]
    esc = lambda s: html.escape(str(s or ""))
    text = (
        "<b>🆕 Заявка на доступ</b>\n"
        f"Req: <code>{esc(req_id)}</code>\n"
        f"User: <code>{esc(r['user_id'])}</code> @{esc(r.get('username',''))} — {esc(r.get('name',''))}\n"
        f"Магазин: <b>{esc(r['store'])}</b> — {esc(STORE_CATALOG.get(r['store'],'?'))}\n"
        f"Роль: <b>{esc(r['role'])}</b>\n"
        f"Время (UTC): {esc(r['ts'])}"
    )
    kb = InlineKeyboardMarkup([
        [
            InlineKeyboardButton("✅ Одобрить", callback_data=f"reg:approve:{req_id}"),
            InlineKeyboardButton("❌ Отклонить", callback_data=f"reg:reject:{req_id}"),
        ]
    ])
    await context.bot.send_message(chat_id=ADMIN_ID, text=text, parse_mode="HTML", reply_markup=kb)

async def cmd_register(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    /register <STORE_CODE> <ROLE_SECRET>
    → создаёт заявку и отправляет админу на модерацию
    """
    u = update.effective_user
    if len(context.args) < 2:
        await update.effective_chat.send_message(
            "Используй: <code>/register &lt;КОД_МАГАЗИНА&gt; &lt;СЕКРЕТ_РОЛИ&gt;</code>\n"
            "Коды — /stores. Секрет выдаёт администратор.",
            parse_mode="HTML",
        )
        return
    store = context.args[0].strip().upper()
    secret = context.args[1].strip()
    if store not in STORE_CATALOG:
        await update.effective_chat.send_message("Неизвестный код магазина. Список: /stores")
        return

    role = _role_from_secret(secret)
    if not role:
        await update.effective_chat.send_message("Неверный секрет роли. Проверь у администратора.")
        return

    if is_admin(u.id):
        prof = get_profile(u.id)
        prof["role"] = role
        prof["current_store"] = store
        _upd_from_user(u, prof)
        _save_staff()
        # обновим меню для админа (его личный чат)
        await refresh_chat_commands(context.bot, update.effective_chat.id, u.id)
        await update.effective_chat.send_message(
            f"Админ подтверждён сразу. Роль: <b>{html.escape(role)}</b>. Магазин: <b>{html.escape(store)}</b>.",
            parse_mode="HTML",
        )
        return

    req_id = _gen_req_id(u.id)
    PENDING[req_id] = {
        "user_id": u.id,
        "store": store,
        "role": role,
        "username": u.username or "",
        "name": f"{u.first_name or ''} {u.last_name or ''}".strip(),
        "ts": datetime.utcnow().isoformat(timespec="seconds") + "Z",
    }
    _save_pending()

    await update.effective_chat.send_message(
        f"Заявка отправлена админу. Номер: <code>{html.escape(req_id)}</code>.\n"
        "После одобрения придёт уведомление.",
        parse_mode="HTML",
    )
    await _notify_admin_new(context, req_id)

async def cmd_pending(update: Update, context: ContextTypes.DEFAULT_TYPE):
    u = update.effective_user
    if not is_admin(u.id):
        await update.effective_chat.send_message("Команда только для администратора.")
        return
    if not PENDING:
        await update.effective_chat.send_message("Очередь пуста ✅")
        return
    esc = lambda s: html.escape(str(s or ""))
    lines = ["<b>Ожидают модерации:</b>"]
    for req_id, r in sorted(PENDING.items()):
        lines.append(
            f"• <code>{esc(req_id)}</code> — user <code>{esc(r['user_id'])}</code> "
            f"@{esc(r.get('username',''))} — {esc(r.get('name',''))}, "
            f"роль <b>{esc(r['role'])}</b>, магазин <b>{esc(r['store'])}</b>"
        )
    await update.effective_chat.send_message("\n".join(lines), parse_mode="HTML")

async def reg_callbacks(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    if not q or not q.data.startswith("reg:"):
        return
    _, action, req_id = q.data.split(":", 2)
    if not is_admin(q.from_user.id):
        await q.answer("Только администратор", show_alert=True)
        return
    r = PENDING.get(req_id)
    if not r:
        await q.answer("Заявка не найдена/уже обработана", show_alert=True)
        try:
            await q.edit_message_text("Эта заявка уже обработана.")
        except Exception:
            pass
        return

    user_id = int(r["user_id"])
    if action == "approve":
        prof = get_profile(user_id)
        prof["role"] = r["role"]
        prof["current_store"] = r["store"]
        _save_staff()
        del PENDING[req_id]
        _save_pending()

        await q.answer("Одобрено ✅")
        try:
            await q.edit_message_text(q.message.text + "\n\n<b>🔔 Статус: одобрено.</b>", parse_mode="HTML")
        except Exception:
            pass
        try:
            await context.bot.send_message(
                chat_id=user_id,
                text=f"✅ Доступ одобрен администратором.\nРоль: <b>{html.escape(prof['role'])}</b>, "
                     f"магазин: <b>{html.escape(prof['current_store'])}</b>.",
                parse_mode="HTML",
            )
            # обновим меню команд в личке пользователя (chat_id == user_id для приватного чата)
            await refresh_chat_commands(context.bot, user_id, user_id)
        except Exception as e:
            log(f"notify user approve error: {e}")
        return

    if action == "reject":
        del PENDING[req_id]
        _save_pending()
        await q.answer("Отклонено ❌")
        try:
            await q.edit_message_text(q.message.text + "\n\n<b>🔔 Статус: отклонено.</b>", parse_mode="HTML")
        except Exception:
            pass
        try:
            await context.bot.send_message(
                chat_id=user_id,
                text="❌ Заявка отклонена администратором. Уточни детали у своего руководителя.",
            )
        except Exception as e:
            log(f"notify user reject error: {e}")
        return

# ──────────────────────────────────────────────────────────────────────────────
# Команды профиля/магазинов
# ──────────────────────────────────────────────────────────────────────────────
async def cmd_whoami(update: Update, context: ContextTypes.DEFAULT_TYPE):
    u = update.effective_user
    prof = get_profile(u.id)
    cur = prof.get("current_store")
    cur_name = STORE_CATALOG.get(cur, "—") if cur else "—"
    # HTML + экранирование, чтобы избежать BadRequest из-за '_'
    text = (
        "🧾 <b>Профиль</b>\n"
        f"ID: <code>{u.id}</code>\n"
        f"Роль: <b>{html.escape(_role_for_display(u.id, prof))}</b>\n"
        f"Магазин: <b>{html.escape(cur or '—')}</b> — {html.escape(cur_name)}\n"
        "Доступные магазины: "
        f"{html.escape(', '.join(prof['stores'])) if prof['stores'] else 'не ограничено'}"
    )
    await update.effective_chat.send_message(text, parse_mode="HTML")

async def cmd_stores(update: Update, context: ContextTypes.DEFAULT_TYPE):
    lines = ["Коды магазинов:"]
    for code, name in sorted(STORE_CATALOG.items()):
        lines.append(f"{code} — {name}")
    await update.effective_chat.send_message("\n".join(lines))

async def cmd_setstore(update: Update, context: ContextTypes.DEFAULT_TYPE):
    u = update.effective_user
    prof = get_profile(u.id)
    if not context.args:
        await update.effective_chat.send_message("Используй: <code>/setstore &lt;КОД&gt;</code> (см. /stores)", parse_mode="HTML")
        return
    code = context.args[0].strip().upper()
    if code not in STORE_CATALOG:
        await update.effective_chat.send_message("Неизвестный код магазина. Список: /stores")
        return
    if prof["stores"] and code not in prof["stores"]:
        await update.effective_chat.send_message("Этот магазин тебе не назначен. Обратись к администратору.")
        return
    prof["current_store"] = code
    _upd_from_user(u, prof)
    _save_staff()
    # HTML + экранирование названия магазина
    await update.effective_chat.send_message(
        f"Ок! Текущий магазин: <b>{html.escape(code)}</b> — {html.escape(STORE_CATALOG[code])}",
        parse_mode="HTML"
    )

async def cmd_setrole(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    /setrole <auditor|viewer> <user_id> [<STORE_CODE>]
    Если указан STORE_CODE: назначаем current_store и додаём в список stores (если есть ограничения).
    """
    u = update.effective_user
    if not is_admin(u.id):
        await update.effective_chat.send_message("Команда только для администратора.")
        return
    if len(context.args) < 2:
        await update.effective_chat.send_message(
            "Используй: <code>/setrole &lt;auditor|viewer&gt; &lt;user_id&gt; [&lt;КОД_МАГАЗИНА&gt;]</code>",
            parse_mode="HTML",
        )
        return
    role = context.args[0].lower()
    try:
        target = int(context.args[1])
    except Exception:
        await update.effective_chat.send_message("user_id должен быть числом.")
        return
    if role not in ("auditor", "viewer"):
        await update.effective_chat.send_message("Роль должна быть auditor или viewer.")
        return

    prof = get_profile(target)
    prof["role"] = role

    # опционально выставим магазин
    if len(context.args) >= 3:
        store_code = context.args[2].strip().upper()
        if store_code in STORE_CATALOG:
            prof["current_store"] = store_code
            # если у пользователя ограниченный список — добавим код
            if prof["stores"] is not None:
                if store_code not in prof["stores"]:
                    prof["stores"].append(store_code)
        else:
            await update.effective_chat.send_message(f"Внимание: код магазина не найден: <b>{html.escape(store_code)}</b>", parse_mode="HTML")

    _save_staff()
    await update.effective_chat.send_message(
        f"Роль пользователя {target} установлена: <b>{html.escape(role)}</b>"
        + (f"; магазин: <b>{html.escape(prof.get('current_store') or '—')}</b>" if len(context.args) >= 3 else ""),
        parse_mode="HTML"
    )
    # обновим меню команд для целевого пользователя (в его личном чате)
    await refresh_chat_commands(context.bot, target, target)

# ──────────────────────────────────────────────────────────────────────────────
# Помощь по ролям
# ──────────────────────────────────────────────────────────────────────────────
async def cmd_viewer(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = (
        "<b>Роль: Viewer</b>\n"
        "• Видит профиль и магазины: /whoami, /stores\n"
        "• Может выбрать магазин для просмотра: <code>/setstore &lt;КОД&gt;</code>\n"
        "• Для прохождения чек-листа нужна роль auditor\n"
    )
    await update.effective_chat.send_message(text, parse_mode="HTML")

async def cmd_auditor(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = (
        "<b>Роль: Auditor</b>\n"
        "• Всё как у viewer\n"
        "• Запуск чек-листа: <code>/checklist</code>\n"
        "• Важно: заранее выбрать магазин: <code>/setstore &lt;КОД&gt;</code>\n"
    )
    await update.effective_chat.send_message(text, parse_mode="HTML")

async def cmd_admin(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        await update.effective_chat.send_message("Команда только для администратора.")
        return
    text = (
        "<b>Роль: Admin</b>\n"
        "• Модерация заявок: <code>/pending</code>\n"
        "• Назначить роль: <code>/setrole &lt;auditor|viewer&gt; &lt;user_id&gt; [&lt;КОД&gt;]</code>\n"
        "• Список привязок (кто за что): <code>/bindings</code>\n"
    )
    await update.effective_chat.send_message(text, parse_mode="HTML")

# ──────────────────────────────────────────────────────────────────────────────
# Список привязок для админа («кто за что»)
# ──────────────────────────────────────────────────────────────────────────────
async def cmd_bindings(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        await update.effective_chat.send_message("Команда только для администратора.")
        return
    if not STAFF:
        await update.effective_chat.send_message("Пока нет пользователей.")
        return
    esc = lambda s: html.escape(str(s if s is not None else "—"))
    lines = ["<b>Привязки ролей:</b>"]
    for uid, prof in sorted(STAFF.items(), key=lambda kv: kv[0]):
        role = prof.get("role") or "viewer"
        uname = ("@" + prof.get("username")) if prof.get("username") else "—"
        name = prof.get("name") or "—"
        cur = prof.get("current_store") or "—"
        cur_h = STORE_CATALOG.get(prof.get("current_store"), "—") if prof.get("current_store") else "—"
        stores_list = ", ".join(prof.get("stores") or []) or "не ограничено"
        lines.append(
            f"• <code>{uid}</code> {esc(uname)} — {esc(name)}\n"
            f"  Роль: <b>{esc(role)}</b>; Текущий: <b>{esc(cur)}</b> — {esc(cur_h)}\n"
            f"  Магазины: {esc(stores_list)}"
        )
    await update.effective_chat.send_message("\n".join(lines), parse_mode="HTML")

# ──────────────────────────────────────────────────────────────────────────────
# Бизнес-логика чек-листа
# ──────────────────────────────────────────────────────────────────────────────
async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    u = update.effective_user
    prof = get_profile(u.id)
    _upd_from_user(u, prof)

    # обновим меню команд под текущую роль в этом чате
    await refresh_chat_commands(context.bot, update.effective_chat.id, u.id)

    payload = update.message.text.split(maxsplit=1)
    if len(payload) == 2:
        code = payload[1].strip().upper()
        if code in STORE_CATALOG:
            prof["current_store"] = code
            _save_staff()

    kb = [[
        InlineKeyboardButton("Проверка", callback_data="ping"),
        InlineKeyboardButton("Чек-лист", callback_data="cl:start"),
    ]]
    store_line = f"*{prof['current_store']}*" if prof.get("current_store") else "—"
    await update.effective_chat.send_message(
        "Привет! Регистрация теперь с модерацией:\n"
        "• <code>/register &lt;КОД_МАГАЗИНА&gt; &lt;СЕКРЕТ_РОЛИ&gt;</code>\n"
        "• или deep-link t.me/{username}?start=&lt;КОД&gt; (только магазин)\n\n"
        f"Текущий магазин: {store_line}. Роль: *{_role_for_display(u.id, prof)}*.\n"
        "Список кодов: /stores",
        reply_markup=InlineKeyboardMarkup(kb),
        parse_mode="Markdown",
    )

async def on_button(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    if not q or not q.data:
        return
    # не трогаем модерацию — пусть обработает reg_callbacks
    if q.data.startswith("reg:"):
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

async def cmd_checklist(update: Update, context: ContextTypes.DEFAULT_TYPE):
    u = update.effective_user
    prof = get_profile(u.id)
    if not (prof["role"] == "auditor" or is_admin(u.id)):
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

    if not (prof["role"] == "auditor" or is_admin(u.id)):
        await q.answer("Недостаточно прав", show_alert=True)
        return
    err = must_have_store(update, prof)
    if err:
        await q.answer(err, show_alert=True)
        return

    chat_id = q.message.chat_id
    st = _cl_get(chat_id)
    action = q.data.split(":", 1)[1]
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
                await q.message.chat.send_photo(photo=files[0], caption=f"Пример: {CHECKLIST[si]['title']}")
            except Exception as e:
                log(f"send_photo error: {e}")
                await q.answer("Не удалось отправить фото", show_alert=True)
            else:
                await q.answer("Пример отправлен")
        else:
            await q.answer("Для этой секции пока нет примера", show_alert=True)
        return

    if action.startswith("toggle:"):
        ii = int(action.split(":")[1])
        sec_marks = st["marks"].setdefault(si, {})
        cur = sec_marks.get(ii)
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
    # команды
    app_.add_handler(CommandHandler("start", cmd_start))
    app_.add_handler(CommandHandler("register", cmd_register))
    app_.add_handler(CommandHandler("pending", cmd_pending))
    app_.add_handler(CommandHandler("checklist", cmd_checklist))
    app_.add_handler(CommandHandler("whoami", cmd_whoami))
    app_.add_handler(CommandHandler("stores", cmd_stores))
    app_.add_handler(CommandHandler("setstore", cmd_setstore))
    app_.add_handler(CommandHandler("setrole", cmd_setrole))
    app_.add_handler(CommandHandler("viewer", cmd_viewer))
    app_.add_handler(CommandHandler("auditor", cmd_auditor))
    app_.add_handler(CommandHandler("admin", cmd_admin))
    app_.add_handler(CommandHandler("bindings", cmd_bindings))
    # СНАЧАЛА — специфические callback-и, потом общий.
    app_.add_handler(CallbackQueryHandler(reg_callbacks, pattern=r"^reg:"))
    app_.add_handler(CallbackQueryHandler(cl_callback, pattern=r"^cl:"))
    app_.add_handler(CallbackQueryHandler(on_button, block=False))  # общий, НЕ блокирует
    return app_

# ──────────────────────────────────────────────────────────────────────────────
# Фоновый поток с loop
# ──────────────────────────────────────────────────────────────────────────────
async def _ptb_init_async():
    global _app, _ptb_ready, BOT_USERNAME
    log("PTB: build application…")
    _app = build_application()
    log("PTB: application.initialize()…")
    await _app.initialize()
    me = await _app.bot.get_me()
    BOT_USERNAME = me.username
    _ptb_ready = True
    log(f"PTB: READY as @{BOT_USERNAME}")

def _ptb_thread_main():
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
    info = {
        "is_running": bool(_loop and _loop.is_running()),
        "last_ptb_error": None,
        "loop_alive": _loop_alive,
        "ptb_ready": _ptb_ready,
    }
    return app.response_class(json.dumps(info), mimetype="application/json")

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
        "pending_requests": len(PENDING),
        "staff_file": str(STAFF_FILE.resolve()),
        "pending_file": str(PENDING_FILE.resolve()),
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

@app.before_request
def _before_any():
    ensure_ptb_started()

if __name__ == "__main__":
    ensure_ptb_started()
    app.run(host="0.0.0.0", port=int(os.getenv("PORT", "5000")))



