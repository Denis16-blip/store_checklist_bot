## app.py — чек-лист + саморегистрация с модерацией + подписки TOM/RD + TZ + (опц.) уведомления + мастер выбора роли
import os
import json
import threading
import asyncio
import warnings
from datetime import datetime, timezone, timedelta
from pathlib import Path
import html
from zoneinfo import ZoneInfo

from flask import Flask, request, Response, jsonify
from dotenv import load_dotenv

from telegram import (
    Update, InlineKeyboardMarkup, InlineKeyboardButton,
    BotCommand, BotCommandScopeChat
)
from telegram.ext import (
    Application, CommandHandler, CallbackQueryHandler,
    ContextTypes
)
from telegram.error import BadRequest
from telegram.warnings import PTBUserWarning
import httpx
import psycopg  # direct Postgres


# 🔇 Спрячем предупреждение PTB про JobQueue, если его нет
warnings.filterwarnings("ignore", category=PTBUserWarning)

# ──────────────────────────────────────────────────────────────────────────────
# ENV / Globals
# ──────────────────────────────────────────────────────────────────────────────
load_dotenv()

BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()
ADMIN_ID = int(os.getenv("TELEGRAM_ADMIN_ID", "0") or 0)
BASE_URL = os.getenv("BASE_URL", "").strip()

AUDITOR_SECRET = os.getenv("AUDITOR_SECRET", "").strip()
VIEWER_SECRET  = os.getenv("VIEWER_SECRET", "").strip()

assert BOT_TOKEN, "BOT_TOKEN is required"

app = Flask(__name__)
# ──────────────────────────────────────────────────────────────────────────────
# Database (Neon via psycopg_pool)
# ──────────────────────────────────────────────────────────────────────────────
DB_URL = os.getenv("DATABASE_URL", "").strip()
assert DB_URL, "DATABASE_URL is required"

# Pooled connection for serverless Postgres

def exec_sql(sql: str, params: tuple | None = None, fetch: bool = False):
    """Simple helper for SQL execution (no pool).""" 
    try:
        with psycopg.connect(DB_URL, sslmode="require", connect_timeout=5) as conn:
            with conn.cursor() as cur:
                cur.execute(sql, params or ())
                if fetch:
                    return cur.fetchall()
    except Exception as e:
        print(f"[DB ERROR] {e}", flush=True)
        raise

_ptb_thread: threading.Thread | None = None
_loop: asyncio.AbstractEventLoop | None = None
_app: Application | None = None
_loop_alive = False
_ptb_ready = False
BOT_USERNAME = None

def log(msg: str):
    print(f"[{datetime.utcnow().isoformat(timespec='seconds')}Z] {msg}", flush=True)

def iso_now():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")

# ──────────────────────────────────────────────────────────────────────────────
# Справочники магазинов (сокращён из твоего списка + добавлены недостающие)
# ──────────────────────────────────────────────────────────────────────────────
STORE_CATALOG: dict[str, str] = {
    "C00X":"RU_ABAKAN_Ametist_SPORT","C0RG":"RU_ARKHANGELSK_TitanArena_SPORT","C082":"RU_GELENDZHIK_Lenina_SPORT",
    "C0JP":"RU_IRKUTSK_ModnyKvartal_SPORT","C03F":"RU_IZHEVSK_Pushkinskaya_SPORT","C09Z":"RU_KALUGA_RIO_SPORT",
    "C0JN":"RU_KRASNODAR_Galereya_SPORT","C0BW":"RU_KRASNОДАР_OzMoll_SPORT",
    "C0SL":"RU_MOSCOW_Afimall_SPORT","C0LU":"RU_MOSCOW_Aviapark_SPORT","C0VT":"RU_MOSCOW_Evropolis_SPORT",
    "C0TY":"RU_MOSCOW_KashirskayaPlaza_SPORT","C0VY":"RU_MOSCOW_KM7_SPORT","C0OI":"RU_MOSCOW_Kolumbus_SPORT",
    "C024":"RU_MOSCOW_KrasnayaPresnya_SPORT","C0GN":"RU_MOSCOW_MegaBelayaDacha_SPORT","C0GJ":"RU_MOSCOW_MegaBelayaDacha_URBAN",
    "C0VU":"RU_MOSCOW_Metropolis_SPORT","C022":"RU_MOSCOW_OkhotnyRyad_URBAN","C0WD":"RU_MOSCOW_PaveletskayaPlaza_SPORT",
    "C25Q":"RU_MOSCOW_Salaris_SPORT","C0TQ":"RU_MOSCOW_VegasKuncevo_SPORT","C0NJ":"RU_MOSCOW_VegasSiti_SPORT",
    "C047":"RU_MOSCOW_Vegas_SPORT",
    "C0IZ":"RU_MYTISHCHI_MytishchiKrasnykit_SPORT","C0VN":"RU_NOVOROSSIYSK_KrasnayaPloshchad_SPORT",
    "C00K":"RU_NOVOSIBIRSK_TTSAura_SPORT","C0DY":"RU_OBNINSK_TriumfPlaza_SPORT","C0VB":"RU_OMSK_Mega_SPORT",
    "C0WF":"RU_PERM_Planeta_SPORT","C08E":"RU_SAINT-PETERSBURG_Galereya_SPORT","C0OQ":"RU_SAINT-PETERSBURG_Leto_SPORT",
    "C081":"RU_SARATOV_TriumfMoll_SPORT","C0WE":"RU_SOCHI_MoreMoll_SPORT","C0EI":"RU_SURGUT_Aura_SPORT",
    "C0SM":"RU_TULA_Maksi_SPORT","C085":"RU_VORONEZH_GalereyaChizhova_SPORT","C0KH":"RU_YAROSLAVL_Aura_SPORT",
    "C002":"RU_YUZHNO-SAKHALINSK_SitiMoll_SPORT",
}

DATA_DIR = Path(os.getenv("DATA_DIR", "data"))
STAFF_FILE = DATA_DIR / "staff.json"
PENDING_FILE = DATA_DIR / "pending.json"
SUBS_FILE = DATA_DIR / "subs.json"
TOM_FILE = DATA_DIR / "tom_groups.json"
RUNS_FILE = DATA_DIR / "check_runs.jsonl"

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

def _append_jsonl(path: Path, obj: dict):
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(obj, ensure_ascii=False) + "\n")
    except Exception as e:
        log(f"append {path.name} error: {e}")

# staff: {user_id: {role, stores, current_store, username, name, tz?, inactive?, intended_role?, awaiting_approval?, approved?}}
STAFF: dict[int, dict] = {int(k): v for k, v in _read_json(STAFF_FILE, {}).items()}
PENDING: dict[str, dict] = _read_json(PENDING_FILE, {})

def _save_staff(): _write_json(STAFF_FILE, {str(k): v for k, v in STAFF.items()})
def _save_pending(): _write_json(PENDING_FILE, PENDING)

def is_admin(uid: int) -> bool: return ADMIN_ID and uid == ADMIN_ID

def get_profile(uid: int) -> dict:
    prof = STAFF.get(uid)
    if not prof:
        prof = {"role": "viewer", "stores": [], "current_store": None, "username": "", "name": "", "tz": "Europe/Moscow"}
        STAFF[uid] = prof
        _save_staff()
    if "tz" not in prof:
        prof["tz"] = "Europe/Moscow"
    return prof

def _upd_from_user(user, prof):
    prof["username"] = user.username or ""
    prof["name"] = f"{user.first_name or ''} {user.last_name or ''}".strip()

def must_have_store(update: Update, prof: dict) -> str | None:
    if not prof.get("current_store"):
        return "Сначала выбери магазин: /stores → /setstore &lt;КОД&gt; или /register &lt;КОД&gt; &lt;СЕКРЕТ&gt;"
    cur = prof["current_store"]
    if prof["stores"] and cur not in prof["stores"]:
        return "Текущий магазин не входит в твой список. Выбери другой: /setstore &lt;КОД&gt;"
    return None

# ──────────────────────────────────────────────────────────────────────────────
# Подписки (персист + индексы)
# ──────────────────────────────────────────────────────────────────────────────
def _load_subs():
    raw = _read_json(SUBS_FILE, {"USER_SUBS": {}, "STORE_SUBS": {}})
    user_subs = {}
    for k, v in raw.get("USER_SUBS", {}).items():
        uid = int(k)
        if v == "*" or (isinstance(v, list) and "*" in v):
            user_subs[uid] = {"*"}
        else:
            user_subs[uid] = set(v or [])
    store_subs = {code: set(map(int, lst)) for code, lst in raw.get("STORE_SUBS", {}).items()}
    return user_subs, store_subs

def _save_subs():
    USER_SUBS_JSON = {}
    for uid, subs in USER_SUBS.items():
        if "*" in subs:
            USER_SUBS_JSON[str(uid)] = "*"
        else:
            USER_SUBS_JSON[str(uid)] = sorted(list(subs))
    STORE_SUBS_JSON = {code: sorted(list(uids)) for code, uids in STORE_SUBS.items()}
    _write_json(SUBS_FILE, {"USER_SUBS": USER_SUBS_JSON, "STORE_SUBS": STORE_SUBS_JSON})

USER_SUBS, STORE_SUBS = _load_subs()

def _is_valid_store(code: str) -> bool:
    return code in STORE_CATALOG

def _normalize_codes(codes):
    norm, invalid = [], []
    for c in codes:
        code = c.strip().upper()
        if not code: continue
        (norm if _is_valid_store(code) else invalid).append(code)
    return norm, invalid

def _subscribe_codes(uid: int, codes: list[str]) -> tuple[int, list[str]]:
    if uid not in USER_SUBS:
        USER_SUBS[uid] = set()
    if "*" in USER_SUBS[uid]:
        return 0, []
    added, ignored = 0, []
    for code in codes:
        if code in USER_SUBS[uid]:
            ignored.append(code); continue
        USER_SUBS[uid].add(code)
        STORE_SUBS.setdefault(code, set()).add(uid)
        added += 1
    _save_subs()
    return added, ignored

def _unsubscribe_codes(uid: int, codes: list[str]) -> int:
    removed = 0
    subs = USER_SUBS.get(uid, set())
    for code in codes:
        if code in subs:
            subs.remove(code); removed += 1
        if code in STORE_SUBS:
            STORE_SUBS[code].discard(uid)
            if not STORE_SUBS[code]:
                del STORE_SUBS[code]
    USER_SUBS[uid] = subs
    _save_subs()
    return removed

def _subscribe_all(uid: int):
    USER_SUBS[uid] = {"*"}; _save_subs()

def _unsubscribe_all(uid: int):
    subs = USER_SUBS.get(uid, set())
    subs.discard("*")
    USER_SUBS[uid] = subs; _save_subs()

def _recipients_for_store(code: str) -> set[int]:
    direct = set(STORE_SUBS.get(code, set()))
    all_followers = {uid for uid, subs in USER_SUBS.items() if subs and "*" in subs}
    return direct | all_followers

def _clear_all_subs_for_user(uid: int):
    subs = USER_SUBS.pop(uid, set())
    subs.discard("*")
    for code in list(subs):
        if code in STORE_SUBS:
            STORE_SUBS[code].discard(uid)
            if not STORE_SUBS[code]:
                del STORE_SUBS[code]
    _save_subs()

# ──────────────────────────────────────────────────────────────────────────────
# ТОМ-группы + RD (все магазины)
# ──────────────────────────────────────────────────────────────────────────────
DEFAULT_TOM_GROUPS = {
    "Глазунов Глеб": [
        "C0SL","C0LU","C0VT","C0TY","C0VY","C0OI","C024","C0GN","C0GJ","C0VU",
        "C022","C0WD","C25Q","C0TQ","C0NJ","C047"
    ],
    "Данькин Григорий": ["C00X","C0JP","C00K","C0VB","C0WF","C0EI","C002"],
    "Акоста Максим": ["C0RG","C08E","C0OQ","C0KH"],
    "Санько Сергей": ["C082","C03F","C0JN","C0BW","C0VN","C081","C0WE","C085"],
    "Косинова Алина": ["C09Z","C0IZ","C0DY","C0SM"],
}

TOM_GROUPS: dict[str, dict] = {}  # slug -> {"title": str, "codes": [str]}

def _slugify(title: str) -> str:
    return "tom_" + "".join(ch if ch.isalnum() else "_" for ch in title).strip("_").lower()

def _load_tom_groups():
    global TOM_GROUPS
    cfg = _read_json(TOM_FILE, {"groups": DEFAULT_TOM_GROUPS})
    src = cfg.get("groups") or DEFAULT_TOM_GROUPS
    groups = {}
    for title, codes in src.items():
        codes_norm = [c for c in (codes or []) if c in STORE_CATALOG]
        if not codes_norm:
            continue
        slug = _slugify(title)
        groups[slug] = {"title": title, "codes": sorted(set(codes_norm))}
    TOM_GROUPS = groups
    log(f"TOM groups loaded: {len(TOM_GROUPS)}")

_load_tom_groups()

# ──────────────────────────────────────────────────────────────────────────────
# Команды/меню
# ──────────────────────────────────────────────────────────────────────────────
ROLE_COMMANDS: dict[str, list[BotCommand]] = {
    "viewer": [
        BotCommand("start", "начать"),
        BotCommand("register", "запросить доступ (код+секрет)"),
        BotCommand("whoami", "профиль"),
        BotCommand("stores", "список магазинов"),
        BotCommand("setstore", "выбрать магазин"),
        BotCommand("viewer", "что может viewer"),
        BotCommand("tom", "подписка по ТОМ / RD"),
        BotCommand("subs", "мои подписки"),
        BotCommand("follow", "подписаться на коды"),
        BotCommand("unfollow", "отписаться от кодов"),
        BotCommand("followall", "подписка на все"),
        BotCommand("unfollowall", "снять подписку на все"),
        BotCommand("settz", "установить часовой пояс"),
    ],
    "auditor": [
        BotCommand("start", "начать"),
        BotCommand("whoami", "профиль"),
        BotCommand("checklist", "чек-лист"),
        BotCommand("auditor", "что может auditor"),
        BotCommand("settz", "установить часовой пояс"),
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
        BotCommand("bindings", "кто за что"),
        BotCommand("admin", "что может admin"),
        BotCommand("subscribe", "подписать юзера на коды"),
        BotCommand("unsubscribe", "отписать юзера от кодов"),
        BotCommand("subscribeall", "подписать юзера на все"),
        BotCommand("unsubscribeall", "снять подписку на все"),
        BotCommand("deactivate", "деактивировать пользователя"),
        BotCommand("tom", "подписка по ТОМ / RD"),
        BotCommand("reload_tom", "перечитать группы ТОМ"),
        BotCommand("settz", "установить часовой пояс"),
    ],
}

def _role_for_display(uid: int, prof: dict) -> str:
    return "admin" if is_admin(uid) else prof.get("role", "viewer")

async def refresh_chat_commands(bot, chat_id: int, user_id: int):
    prof = get_profile(user_id)
    role = _role_for_display(user_id, prof)
    commands = ROLE_COMMANDS.get(role, ROLE_COMMANDS["viewer"])
    try:
        await bot.set_my_commands(commands=commands, scope=BotCommandScopeChat(chat_id))
    except Exception as e:
        log(f"set_my_commands error for chat {chat_id}: {e}")

# ──────────────────────────────────────────────────────────────────────────────
# Чек-лист (данные/рендер)
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
    0: ["AgACAgIAAxkBAAN-aPc9fUdYqxNInDdLrh01UHckFW0AApL-MRvGH7hLzIOseULYaQ0BAAMCAAN4AAM2BA"],
    1: ["AgACAgIAAxkBAAN7aPc9WeexQm229VrzIW07tL18TccAAo3-MRvGH7hLuY3p8Zmreq8BAAMCAAN4AAM2BA"],
    2: ["AgACAgIAAxkBAAN9aPc9dabPgwhMuqDyMuCP52xNiZoAApH-MRvGH7hLayPbIRcX4O0BAAMCAAN4AAM2BA"],
    3: ["AgACAgIAAxkBAAN8aPc9bcea5a-h24wkS-zxpUBbdH4AApD-MRvGH7hLc0mtlweQiY4BAAMCAAN4AAM2BA"],
    5: ["AgACAgIAAxkBAAOAaPc9jBeS7KupdZWKttfeHrjT0YAAApT-MRvGH7hLYmedyzrqAAHaAQADAgADeAADNgQ"],
    6: ["AgACAgIAAxkBAAN_aPc9hXcYmK--YdH5wyJGthZp7kIAApP-MRvGH7hLalo9O7bUB34BAAMCAAN4AAM2BA"],
}

_cl_state = {}  # chat_id -> {"sec": int, "marks": {sec: {item: bool|None}}}
FINISHED_KEYS = set()

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
    lines += ["", f"Прогресс: *{done}/{total}* ({pct}%)", "_Отмечай каждый пункт как ✅ или ❌. Без пропусков._"]
    return "\n".join(lines)


def _fmt_progress_text(st) -> str:
    """Format overall progress across all sections for Markdown."""
    lines = ["*Прогресс по чек-листу*"]
    done, total = _human_sec_progress(st)
    pct = int(round(100 * done / total)) if total else 0
    lines.append(f"Всего: *{done}/{total}* ({pct}%)")
    for i, sec in enumerate(CHECKLIST):
        sec_marks = st["marks"].get(i, {}) or {}
        d = sum(1 for v in sec_marks.values() if v is True)
        t = len(sec["items"])
        if t == 0:
            sym = "⬜️"
        elif d == 0:
            sym = "⬜️"
        elif d == t:
            sym = "✅"
        else:
            sym = "🟡"
        lines.append(f"{i+1}. {sym} {sec['title']} — {d}/{t}")
    lines.append("_Отмечай каждый пункт как ✅ или ❌. Без пропусков._")
    return "\n".join(lines)

def _kb_section(si: int, st):
    sec = CHECKLIST[si]
    sec_marks = st["marks"].get(si, {})
    rows = []
    for ii in range(len(sec["items"])):
        v = sec_marks.get(ii)
        sym = "✅" if v is True else ("❌" if v is False else "⬜️")
        rows.append([InlineKeyboardButton(f"{ii+1} {sym}", callback_data=f"cl:toggle:{ii}")])
    # Навигация
    rows.append([
        InlineKeyboardButton("⬅ Назад", callback_data="cl:prev"),
        InlineKeyboardButton("➡ Далее", callback_data="cl:next"),
    ])
    # Экстры
    extras = [InlineKeyboardButton("📋 Прогресс", callback_data="cl:progress")]
    if si in EXAMPLE_PHOTOS:
        extras.insert(0, InlineKeyboardButton("📷 Пример", callback_data="cl:photo"))
    rows.append(extras)
    rows.append([InlineKeyboardButton("📑 Перейти к разделу", callback_data="cl:goto")])
    rows.append([InlineKeyboardButton("♻️ Сброс секции", callback_data="cl:resetsec")])
    return InlineKeyboardMarkup(rows)


async def _safe_edit(q, text: str, reply_markup=None, parse_mode: str | None = "Markdown"):
    try:
        await q.edit_message_text(text=text, reply_markup=reply_markup, parse_mode=parse_mode)
    except BadRequest as e:
        if "Message is not modified" in str(e):
            try: await q.answer("Без изменений")
            except Exception: pass
        else:
            raise

# ──────────────────────────────────────────────────────────────────────────────
# Мастер выбора роли (новое)
# ──────────────────────────────────────────────────────────────────────────────
def _kb_role_picker():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("🏪 Директор / Заместитель", callback_data="role:pick:auditor")],
        [InlineKeyboardButton("👀 Наблюдатель (VM, ТОМ, РД)", callback_data="role:pick:viewer")],
    ])

async def role_pick_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    if not q or not q.data.startswith("role:"): return
    _, action, role = (q.data.split(":", 2) + ["", ""])[:3]
    if action != "pick" or role not in ("auditor", "viewer"):
        await q.answer("Неизвестный выбор", show_alert=True); return

    uid = q.from_user.id
    prof = get_profile(uid)
    prof["intended_role"] = role  # ориентация до модерации
    _save_staff()

    if role == "auditor":
        text = (
            "✅ Выбрано: <b>Директор / Заместитель</b>\n\n"
            "Дальше — подай заявку:\n"
            "1) Возьми <b>секрет для директоров</b> у администратора\n"
            "2) Отправь:\n"
            "<code>/register &lt;КОД_МАГАЗИНА&gt; &lt;СЕКРЕТ_ДИРЕКТОРА&gt;</code>\n\n"
            "После одобрения магазин будет <b>закреплён</b> за тобой.\n"
            "Чек-лист: /checklist  •  Коды: /stores"
        )
    else:
        text = (
            "✅ Выбрано: <b>Наблюдатель (VM, ТОМ, РД)</b>\n\n"
            "Теперь подай заявку:\n"
            "1) Возьми <b>секрет наблюдателя</b> у администратора\n"
            "2) Отправь:\n"
            "<code>/register &lt;ЛЮБОЙ_КОД_МАГАЗИНА&gt; &lt;СЕКРЕТ_НАБЛЮДАТЕЛЯ&gt;</code>\n\n"
            "После одобрения настрой подписки: /tom (ТОМ или RD — все магазины).\n"
            "Часовой пояс для уведомлений: <code>/settz Europe/Moscow</code>"
        )

    try:
        await q.edit_message_text(text, parse_mode="HTML")
    except Exception:
        await q.message.reply_text(text, parse_mode="HTML")
    await q.answer()

# ──────────────────────────────────────────────────────────────────────────────
# Модерация регистрации
# ──────────────────────────────────────────────────────────────────────────────
def _gen_req_id(user_id: int) -> str:
    ts = datetime.utcnow().strftime("%Y%m%d%H%M%S")
    return f"R{ts}_{user_id}"

def _role_from_secret(secret: str) -> str | None:
    if AUDITOR_SECRET and secret == AUDITOR_SECRET: return "auditor"
    if VIEWER_SECRET and secret == VIEWER_SECRET:   return "viewer"
    return None

async def _notify_admin_new(context: ContextTypes.DEFAULT_TYPE, req_id: str):
    if not ADMIN_ID: return
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
    kb = InlineKeyboardMarkup([[InlineKeyboardButton("✅ Одобрить", callback_data=f"reg:approve:{req_id}"),
                                InlineKeyboardButton("❌ Отклонить", callback_data=f"reg:reject:{req_id}")]])
    await context.bot.send_message(chat_id=ADMIN_ID, text=text, parse_mode="HTML", reply_markup=kb)

async def cmd_register(update: Update, context: ContextTypes.DEFAULT_TYPE):
    u = update.effective_user
    if len(context.args) < 2:
        await update.effective_chat.send_message(
            "Используй: <code>/register &lt;КОД_МАГАЗИНА&gt; &lt;СЕКРЕТ_РОЛИ&gt;</code>\nКоды — /stores.",
            parse_mode="HTML",
        ); return
    store = context.args[0].strip().upper()
    secret = context.args[1].strip()
    if store not in STORE_CATALOG:
        await update.effective_chat.send_message("Неизвестный код магазина. Список: /stores"); return
    role = _role_from_secret(secret)
    if not role:
        await update.effective_chat.send_message("Неверный секрет роли. Проверь у администратора."); return
    if is_admin(u.id):
        prof = get_profile(u.id)
        prof["role"] = role
        prof["current_store"] = store
        if role == "auditor":
            prof["stores"] = [store]
        prof["approved"] = True
        prof.pop("awaiting_approval", None)
        _upd_from_user(u, prof); _save_staff()
        await refresh_chat_commands(context.bot, update.effective_chat.id, u.id)
        await update.effective_chat.send_message(
            f"Админ подтверждён сразу. Роль: <b>{html.escape(role)}</b>. Магазин: <b>{html.escape(store)}</b>.",
            parse_mode="HTML",
        ); return
    req_id = _gen_req_id(u.id)
    PENDING[req_id] = {"user_id": u.id, "store": store, "role": role,
                       "username": u.username or "", "name": f"{u.first_name or ''} {u.last_name or ''}".strip(),
                       "ts": datetime.utcnow().isoformat(timespec="seconds") + "Z"}
    _save_pending()
    # пометим, что ждём модерации → мастер роли больше не показываем
    prof = get_profile(u.id)
    prof["awaiting_approval"] = True
    _save_staff()
    await update.effective_chat.send_message(
        f"Заявка отправлена админу. Номер: <code>{html.escape(req_id)}</code>.\nПосле одобрения придёт уведомление.",
        parse_mode="HTML"
    )
    await _notify_admin_new(context, req_id)

async def cmd_pending(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        await update.effective_chat.send_message("Команда только для администратора."); return
    if not PENDING:
        await update.effective_chat.send_message("Очередь пуста ✅"); return
    esc = lambda s: html.escape(str(s or ""))
    lines = ["<b>Ожидают модерации:</b>"]
    for req_id, r in sorted(PENDING.items()):
        lines.append(f"• <code>{esc(req_id)}</code> — user <code>{esc(r['user_id'])}</code> @{esc(r.get('username',''))} — {esc(r.get('name',''))}, роль <b>{esc(r['role'])}</b>, магазин <b>{esc(r['store'])}</b>")
    await update.effective_chat.send_message("\n".join(lines), parse_mode="HTML")

async def reg_callbacks(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    if not q or not q.data.startswith("reg:"): return
    _, action, req_id = q.data.split(":", 2)
    if not is_admin(q.from_user.id):
        await q.answer("Только администратор", show_alert=True); return
    r = PENDING.get(req_id)
    if not r:
        await q.answer("Заявка не найдена/уже обработана", show_alert=True)
        try: await q.edit_message_text("Эта заявка уже обработана.")
        except Exception: pass
        return
    user_id = int(r["user_id"])
    if action == "approve":
        prof = get_profile(user_id)
        prof["role"] = r["role"]
        prof["current_store"] = r["store"]
        if r["role"] == "auditor":
            prof["stores"] = [r["store"]]  # ← фиксируем магазин для аудитора
        prof["approved"] = True
        prof.pop("awaiting_approval", None)
        _save_staff()
        del PENDING[req_id]; _save_pending()
        await q.answer("Одобрено ✅")
        try: await q.edit_message_text(q.message.text + "\n\n<b>🔔 Статус: одобрено.</b>", parse_mode="HTML")
        except Exception: pass
        try:
            await context.bot.send_message(
                chat_id=user_id,
                text=f"✅ Доступ одобрен администратором.\nРоль: <b>{html.escape(prof['role'])}</b>, магазин: <b>{html.escape(prof['current_store'])}</b>.",
                parse_mode="HTML")
            await refresh_chat_commands(context.bot, user_id, user_id)
        except Exception as e: log(f"notify user approve error: {e}")
        return
    if action == "reject":
        del PENDING[req_id]; _save_pending()
        try:
            prof = get_profile(user_id)
            prof.pop("awaiting_approval", None)
            _save_staff()
        except Exception:
            pass
        await q.answer("Отклонено ❌")
        try: await q.edit_message_text(q.message.text + "\n\n<b>🔔 Статус: отклонено.</b>", parse_mode="HTML")
        except Exception: pass
        try: await context.bot.send_message(chat_id=user_id, text="❌ Заявка отклонена администратором.")
        except Exception as e: log(f"notify user reject error: {e}")
        return

# ──────────────────────────────────────────────────────────────────────────────
# Подписки — пользовательские команды (viewer)
# ──────────────────────────────────────────────────────────────────────────────
async def cmd_subs(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    subs = USER_SUBS.get(uid, set())
    if subs and "*" in subs:
        await update.effective_chat.send_message("Ты подписан на <b>ВСЕ</b> магазины.", parse_mode="HTML"); return
    if not subs:
        await update.effective_chat.send_message("Подписок нет. Пример: <code>/follow C0TQ C0SL</code> или <code>/tom</code>", parse_mode="HTML"); return
    rows = " ".join(sorted(subs))
    await update.effective_chat.send_message(f"Твои подписки: <b>{html.escape(rows)}</b>", parse_mode="HTML")

async def cmd_follow(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    if not context.args:
        await update.effective_chat.send_message("Укажи коды через пробел: <code>/follow C0TQ C0SL</code>", parse_mode="HTML"); return
    norm, invalid = _normalize_codes(context.args)
    added, ignored = _subscribe_codes(uid, norm)
    parts = []
    if added: parts.append(f"добавлено: <b>{added}</b>")
    if ignored: parts.append(f"уже были: {html.escape(' '.join(ignored))}")
    if invalid: parts.append(f"не найдены: {html.escape(' '.join(invalid))}")
    if not parts: parts.append("ничего не изменилось")
    await update.effective_chat.send_message("Подписка: " + "; ".join(parts), parse_mode="HTML")

async def cmd_unfollow(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    if not context.args:
        await update.effective_chat.send_message("Укажи коды через пробел: <code>/unfollow C0TQ C0SL</code>", parse_mode="HTML"); return
    norm, invalid = _normalize_codes(context.args)
    removed = _unsubscribe_codes(uid, norm)
    parts = [f"снято: <b>{removed}</b>"]
    if invalid: parts.append(f"не найдены: {html.escape(' '.join(invalid))}")
    await update.effective_chat.send_message("Отписка: " + "; ".join(parts), parse_mode="HTML")

async def cmd_followall(update: Update, context: ContextTypes.DEFAULT_TYPE):
    _subscribe_all(update.effective_user.id)
    await update.effective_chat.send_message("Готово. Теперь ты подписан на <b>ВСЕ</b> магазины.", parse_mode="HTML")

async def cmd_unfollowall(update: Update, context: ContextTypes.DEFAULT_TYPE):
    _unsubscribe_all(update.effective_user.id)
    await update.effective_chat.send_message("Флаг «ВСЕ» снят. Точечные подписки сохранены.", parse_mode="HTML")

# ──────────────────────────────────────────────────────────────────────────────
# Подписки — админские команды
# ──────────────────────────────────────────────────────────────────────────────
async def cmd_admin_subscribe(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        await update.effective_chat.send_message("Команда только для администратора."); return
    if len(context.args) < 2:
        await update.effective_chat.send_message("Используй: <code>/subscribe &lt;user_id&gt; &lt;К1&gt; [&lt;К2&gt; ...]</code>", parse_mode="HTML"); return
    try: target = int(context.args[0])
    except: await update.effective_chat.send_message("user_id должен быть числом."); return
    norm, invalid = _normalize_codes(context.args[1:])
    added, ignored = _subscribe_codes(target, norm)
    parts = [f"добавлено: <b>{added}</b>"]
    if ignored: parts.append(f"уже были: {html.escape(' '.join(ignored))}")
    if invalid: parts.append(f"не найдены: {html.escape(' '.join(invalid))}")
    await update.effective_chat.send_message("Подписка пользователю: " + "; ".join(parts), parse_mode="HTML")

async def cmd_admin_unsubscribe(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        await update.effective_chat.send_message("Команда только для администратора."); return
    if len(context.args) < 2:
        await update.effective_chat.send_message("Используй: <code>/unsubscribe &lt;user_id&gt; &lt;К1&gt; [&lt;К2&gt; ...]</code>", parse_mode="HTML"); return
    try: target = int(context.args[0])
    except: await update.effective_chat.send_message("user_id должен быть числом."); return
    norm, invalid = _normalize_codes(context.args[1:])
    removed = _unsubscribe_codes(target, norm)
    parts = [f"снято: <b>{removed}</b>"]
    if invalid: parts.append(f"не найдены: {html.escape(' '.join(invalid))}")
    await update.effective_chat.send_message("Отписка пользователю: " + "; ".join(parts), parse_mode="HTML")

async def cmd_admin_subscribeall(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        await update.effective_chat.send_message("Команда только для администратора."); return
    if len(context.args) < 1:
        await update.effective_chat.send_message("Используй: <code>/subscribeall &lt;user_id&gt;</code>", parse_mode="HTML"); return
    try: target = int(context.args[0])
    except: await update.effective_chat.send_message("user_id должен быть числом."); return
    _subscribe_all(target)
    await update.effective_chat.send_message(f"Пользователь {target} подписан на <b>ВСЕ</b> магазины.", parse_mode="HTML")

async def cmd_admin_unsubscribeall(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        await update.effective_chat.send_message("Команда только для администратора."); return
    if len(context.args) < 1:
        await update.effective_chat.send_message("Используй: <code>/unsubscribeall &lt;user_id&gt;</code>", parse_mode="HTML"); return
    try: target = int(context.args[0])
    except: await update.effective_chat.send_message("user_id должен быть числом."); return
    _unsubscribe_all(target)
    await update.effective_chat.send_message(f"С пользователя {target} снят флаг «ВСЕ».", parse_mode="HTML")

async def cmd_deactivate(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        await update.effective_chat.send_message("Команда только для администратора."); return
    if len(context.args) < 1:
        await update.effective_chat.send_message("Используй: <code>/deactivate &lt;user_id&gt;</code>", parse_mode="HTML"); return
    try: target = int(context.args[0])
    except: await update.effective_chat.send_message("user_id должен быть числом."); return
    prof = get_profile(target)
    prof["role"] = "viewer"; prof["stores"] = []; prof["current_store"] = None; prof["inactive"] = True
    prof.pop("approved", None); prof.pop("awaiting_approval", None)
    _save_staff()
    _clear_all_subs_for_user(target)
    await update.effective_chat.send_message(
        f"Пользователь {target} деактивирован: роль viewer, магазины очищены, подписки удалены.",
        parse_mode="HTML"
    )
    await refresh_chat_commands(context.bot, target, target)

# ──────────────────────────────────────────────────────────────────────────────
# Экран ТОМ/RD (viewer/admin)
# ──────────────────────────────────────────────────────────────────────────────
def _is_group_fully_subscribed(uid: int, codes: list[str]) -> bool:
    subs = USER_SUBS.get(uid, set())
    if "*" in subs: return True
    return all(code in subs for code in codes)

def _kb_tom(uid: int):
    rows = []
    for slug, g in sorted(TOM_GROUPS.items(), key=lambda kv: kv[1]["title"]):
        title = g["title"]; codes = g["codes"]; n = len(codes)
        on = _is_group_fully_subscribed(uid, codes)
        btn_text = f"{title} ({n}) — {'✅ Подписан' if on else 'Подписаться'}"
        rows.append([InlineKeyboardButton(btn_text, callback_data=f"tom:toggle:{slug}")])
    subs = USER_SUBS.get(uid, set())
    rd_on = ("*" in subs)
    rows.append([InlineKeyboardButton(f"RD — {'✅ ВСЕ' if rd_on else 'Подписаться на ВСЕ'}", callback_data="tom:rd:toggle")])
    rows.append([InlineKeyboardButton("Мои подписки", callback_data="tom:mine")])
    return InlineKeyboardMarkup(rows)

async def cmd_tom(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    await update.effective_chat.send_message("Выбери группу:", reply_markup=_kb_tom(uid))

async def tom_callbacks(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    if not q or not q.data.startswith("tom:"): return
    uid = q.from_user.id
    _, action, payload = (q.data.split(":", 2) + ["", ""])[:3]

    if action == "mine":
        subs = USER_SUBS.get(uid, set())
        if subs and "*" in subs:
            await q.answer("Подписан на ВСЕ")
            await _safe_edit(q, "Ты подписан на <b>ВСЕ</b> магазины.", parse_mode="HTML")
            return
        rows = " ".join(sorted(subs)) if subs else "—"
        await q.answer("Твои подписки")
        await _safe_edit(q, f"Твои подписки: <b>{html.escape(rows)}</b>", parse_mode="HTML")
        return

    if action == "rd" and payload == "toggle":
        if "*" in USER_SUBS.get(uid, set()):
            _unsubscribe_all(uid)
            await q.answer("Снял флаг «ВСЕ»")
        else:
            _subscribe_all(uid)
            await q.answer("Подписал на ВСЕ")
        try:
            await q.edit_message_reply_markup(reply_markup=_kb_tom(uid))
        except Exception: pass
        return

    if action == "toggle":
        g = TOM_GROUPS.get(payload)
        if not g:
            await q.answer("Группа не найдена", show_alert=True); return
        codes = g["codes"]
        if _is_group_fully_subscribed(uid, codes):
            removed = _unsubscribe_codes(uid, codes)
            await q.answer(f"Снято: {removed}")
        else:
            added, _ = _subscribe_codes(uid, codes)
            await q.answer(f"Добавлено: {added}")
        try:
            await q.edit_message_reply_markup(reply_markup=_kb_tom(uid))
        except Exception: pass
        return

# ──────────────────────────────────────────────────────────────────────────────
# Профиль/магазины/роль-справка/TZ
# ──────────────────────────────────────────────────────────────────────────────
async def cmd_whoami(update: Update, context: ContextTypes.DEFAULT_TYPE):
    u = update.effective_user; prof = get_profile(u.id)
    cur = prof.get("current_store"); cur_name = STORE_CATALOG.get(cur, "—") if cur else "—"
    text = ("🧾 <b>Профиль</b>\n"
            f"ID: <code>{u.id}</code>\n"
            f"Роль: <b>{html.escape(_role_for_display(u.id, prof))}</b>\n"
            f"Часовой пояс: <code>{html.escape(prof.get('tz','Europe/Moscow'))}</code>\n"
            f"Магазин: <b>{html.escape(cur or '—')}</b> — {html.escape(cur_name)}\n"
            "Доступные магазины: "
            f"{html.escape(', '.join(prof['stores'])) if prof['stores'] else 'не ограничено'}")
    await update.effective_chat.send_message(text, parse_mode="HTML")

async def cmd_stores(update: Update, context: ContextTypes.DEFAULT_TYPE):
    lines = ["Коды магазинов:"]
    for code, name in sorted(STORE_CATALOG.items()):
        lines.append(f"{code} — {name}")
    await update.effective_chat.send_message("\n".join(lines))

async def cmd_setstore(update: Update, context: ContextTypes.DEFAULT_TYPE):
    u = update.effective_user; prof = get_profile(u.id)
    # блокируем аудитору смену магазина
    if not is_admin(u.id) and prof.get("role") == "auditor":
        await update.effective_chat.send_message("Твой магазин закреплён администратором и не может быть изменён пользователем.")
        return
    if not context.args:
        await update.effective_chat.send_message("Используй: <code>/setstore &lt;КОД&gt;</code> (см. /stores)", parse_mode="HTML"); return
    code = context.args[0].strip().upper()
    if code not in STORE_CATALOG:
        await update.effective_chat.send_message("Неизвестный код магазина. Список: /stores"); return
    if prof["stores"] and code not in prof["stores"]:
        await update.effective_chat.send_message("Этот магазин тебе не назначен. Обратись к администратору."); return
    prof["current_store"] = code; _upd_from_user(u, prof); _save_staff()
    await update.effective_chat.send_message(f"Ок! Текущий магазин: <b>{html.escape(code)}</b> — {html.escape(STORE_CATALOG[code])}", parse_mode="HTML")

async def cmd_setrole(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        await update.effective_chat.send_message("Команда только для администратора."); return
    if len(context.args) < 2:
        await update.effective_chat.send_message("Используй: <code>/setrole &lt;auditor|viewer&gt; &lt;user_id&gt; [&lt;КОД&gt;]</code>", parse_mode="HTML"); return
    role = context.args[0].lower()
    try: target = int(context.args[1])
    except: await update.effective_chat.send_message("user_id должен быть числом."); return
    if role not in ("auditor","viewer"):
        await update.effective_chat.send_message("Роль должна быть auditor или viewer."); return
    prof = get_profile(target); prof["role"] = role
    if len(context.args) >= 3:
        store_code = context.args[2].strip().upper()
        if store_code in STORE_CATALOG:
            prof["current_store"] = store_code
            if role == "auditor":
                prof["stores"] = [store_code]  # ← фиксируем доступ аудитору к одному магазину
        else:
            await update.effective_chat.send_message(f"Внимание: код магазина не найден: <b>{html.escape(store_code)}</b>", parse_mode="HTML")
    _save_staff()
    await update.effective_chat.send_message(
        f"Роль пользователя {target} установлена: <b>{html.escape(role)}</b>"
        + (f"; магазин: <b>{html.escape(prof.get('current_store') or '—')}</b>" if len(context.args) >= 3 else ""),
        parse_mode="HTML")
    await refresh_chat_commands(context.bot, target, target)

async def cmd_viewer(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = ("<b>Роль: Viewer</b>\n"
            "• Профиль/магазины: /whoami, /stores\n"
            "• Выбор магазина: <code>/setstore &lt;КОД&gt;</code>\n"
            "• Подписки: /tom /subs /follow /unfollow /followall /unfollowall\n"
            "• Таймзона: <code>/settz Europe/Moscow</code>\n"
            "• Проходить чек-лист может только auditor")
    await update.effective_chat.send_message(text, parse_mode="HTML")

async def cmd_auditor(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = ("<b>Роль: Auditor</b>\n"
            "• Запуск чек-листа: <code>/checklist</code>\n"
            "• Магазин закреплён админом при регистрации\n"
            "• Профиль: /whoami\n"
            "• Таймзона: <code>/settz Europe/Moscow</code>\n"
            "• Напоминания: пн 10:00 лок. и почасовые при просрочке")
    await update.effective_chat.send_message(text, parse_mode="HTML")

async def cmd_admin(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        await update.effective_chat.send_message("Команда только для администратора."); return
    text = ("<b>Роль: Admin</b>\n"
            "• Модерация: <code>/pending</code>\n"
            "• Роли: <code>/setrole &lt;auditor|viewer&gt; &lt;user_id&gt; [&lt;КОД&gt;]</code>\n"
            "• Привязки: <code>/bindings</code>\n"
            "• Подписки юзеров: <code>/subscribe</code>/<code>/unsubscribe</code>/<code>/subscribeall</code>/<code>/unsubscribeall</code>\n"
            "• Деактивация: <code>/deactivate &lt;user_id&gt;</code>\n"
            "• ТОМ: <code>/tom</code>, перезагрузка групп: <code>/reload_tom</code>")
    await update.effective_chat.send_message(text, parse_mode="HTML")

async def cmd_bindings(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        await update.effective_chat.send_message("Команда только для администратора."); return
    if not STAFF:
        await update.effective_chat.send_message("Пока нет пользователей."); return
    esc = lambda s: html.escape(str(s if s is not None else "—"))
    lines = ["<b>Привязки ролей:</b>"]
    for uid, prof in sorted(STAFF.items(), key=lambda kv: kv[0]):
        role = prof.get("role") or "viewer"
        uname = ("@" + (prof.get("username") or "")) if prof.get("username") else "—"
        name = prof.get("name") or "—"
        cur = prof.get("current_store") or "—"
        cur_h = STORE_CATALOG.get(prof.get("current_store"), "—") if prof.get("current_store") else "—"
        stores_list = ", ".join(prof.get("stores") or []) or "не ограничено"
        subs = USER_SUBS.get(uid, set()); subs_txt = "ВСЕ" if ("*" in subs) else (", ".join(sorted(subs)) or "—")
        lines.append(f"• <code>{uid}</code> {esc(uname)} — {esc(name)}\n  Роль: <b>{esc(role)}</b>; Текущий: <b>{esc(cur)}</b> — {esc(cur_h)}\n  Магазины: {esc(stores_list)}\n  Подписки: {esc(subs_txt)}")
    await update.effective_chat.send_message("\n".join(lines), parse_mode="HTML")

async def cmd_settz(update: Update, context: ContextTypes.DEFAULT_TYPE):
    u = update.effective_user; prof = get_profile(u.id)
    if not context.args:
        await update.effective_chat.send_message("Используй: <code>/settz &lt;IANA TZ, напр. Europe/Moscow&gt;</code>", parse_mode="HTML"); return
    tz = context.args[0]
    try:
        ZoneInfo(tz)
    except Exception:
        await update.effective_chat.send_message("Неизвестная таймзона. Пример: <code>Europe/Moscow</code>", parse_mode="HTML"); return
    prof["tz"] = tz; _save_staff()
    await update.effective_chat.send_message(f"Часовой пояс установлен: <code>{html.escape(tz)}</code>", parse_mode="HTML")

async def cmd_reload_tom(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        await update.effective_chat.send_message("Команда только для администратора."); return
    _load_tom_groups()
    await update.effective_chat.send_message("Группы ТОМ перечитаны.")

# ──────────────────────────────────────────────────────────────────────────────
# Чек-лист: запуск/кнопки/финал + (уведомление подписчикам) и лог
# ──────────────────────────────────────────────────────────────────────────────
async def cmd_checklist(update: Update, context: ContextTypes.DEFAULT_TYPE):
    u = update.effective_user; prof = get_profile(u.id)
    if not (prof["role"] == "auditor" or is_admin(u.id)):
        await update.effective_chat.send_message("Твоя роль — viewer. Для прохождения чек-листа нужна роль auditor."); return
    err = must_have_store(update, prof)
    if err: await update.effective_chat.send_message(err, parse_mode="HTML"); return
    chat_id = update.effective_chat.id; st = _cl_get(chat_id); si = st["sec"]
    await update.effective_chat.send_message(_fmt_section_text(si, st), reply_markup=_kb_section(si, st), parse_mode="Markdown")

async def _notify_viewers_on_finish(context: ContextTypes.DEFAULT_TYPE, store_code: str, finished_by: int, st_obj):
    human = STORE_CATALOG.get(store_code, store_code)
    done, total = _human_sec_progress(st_obj); pct = int(round(100*done/total)) if total else 0
    header = f"📋 Чек-лист завершён по магазину <b>{html.escape(store_code)}</b> — {html.escape(human)}"
    body = f"{header}\nИтог: <b>{done}/{total}</b> ({pct}%)\nВремя (UTC): {html.escape(iso_now())}"
    for uid in _recipients_for_store(store_code):
        try: await context.bot.send_message(uid, body, parse_mode="HTML")
        except Exception: pass

def _log_run(store_code: str, auditor_id: int, st_obj):
    done, total = _human_sec_progress(st_obj)
    rec = {"ts": iso_now(), "store": store_code, "auditor": auditor_id, "done": done, "total": total}
    _append_jsonl(RUNS_FILE, rec)

async def cl_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query; u = q.from_user; prof = get_profile(u.id)
    if not (prof["role"] == "auditor" or is_admin(u.id)):
        await q.answer("Недостаточно прав", show_alert=True); return
    err = must_have_store(update, prof)
    if err: await q.answer(err, show_alert=True); return

    chat_id = q.message.chat_id; st = _cl_get(chat_id)
    action = q.data.split(":", 1)[1]; si = st["sec"]

    if action == "start":
        st["sec"] = 0; st["marks"] = {}; si = 0
        await q.answer(f"Поехали! Магазин: {prof.get('current_store')}")
        await _safe_edit(q, _fmt_section_text(si, st), reply_markup=_kb_section(si, st)); return

    if action == "photo":
        files = EXAMPLE_PHOTOS.get(si)
        if files:
            try: await q.message.chat.send_photo(photo=files[0], caption=f"Пример: {CHECKLIST[si]['title']}")
            except Exception as e: log(f"send_photo error: {e}"); await q.answer("Не удалось отправить фото", show_alert=True)
            else: await q.answer("Пример отправлен")
        else: await q.answer("Для этой секции пока нет примера", show_alert=True)
        return

    if action.startswith("toggle:"):
        ii = int(action.split(":")[1])
        sec_marks = st["marks"].setdefault(si, {})
        cur = sec_marks.get(ii)
        nxt = (not cur) if cur is not None else True
        sec_marks[ii] = nxt
        await q.answer("Обновлено")
        await _safe_edit(q, _fmt_section_text(si, st), reply_markup=_kb_section(si, st)); return

    if action == "resetsec":
        st["marks"][si] = {}; await q.answer("Секция сброшена")
        await _safe_edit(q, _fmt_section_text(si, st), reply_markup=_kb_section(si, st)); return

    if action == "progress":

        await q.answer("Прогресс")
        await _safe_edit(q, _fmt_progress_text(st) + "\n\nНажми «➡ Далее», чтобы продолжить.", reply_markup=_kb_section(si, st)); return

    if action == "prev":
        if si <= 0:
            await q.answer("Это первая секция", show_alert=True); return
        st["sec"] -= 1; si = st["sec"]
        await _safe_edit(q, _fmt_section_text(si, st), reply_markup=_kb_section(si, st)); return

    
    if action == "goto":
        buttons = []
        for i, sec in enumerate(CHECKLIST):
            buttons.append([InlineKeyboardButton(f"{i+1}. {sec['title']}", callback_data=f"cl:goto_{i}")])
        buttons.append([InlineKeyboardButton("↩ Назад", callback_data="cl:backtocur")])
        await _safe_edit(q, "Выбери раздел для перехода:", reply_markup=InlineKeyboardMarkup(buttons))
        return

    if action.startswith("goto_"):
        try:
            target = int(action.split("_")[1])
        except Exception:
            await q.answer("Ошибка номера секции", show_alert=True); return
        if 0 <= target < len(CHECKLIST):
            st["sec"] = target
            await _safe_edit(q, _fmt_section_text(target, st), reply_markup=_kb_section(target, st))
        return

    if action == "backtocur":
        si = st["sec"]
        await _safe_edit(q, _fmt_section_text(si, st), reply_markup=_kb_section(si, st))
        return

    if action == "next":
        if si >= len(CHECKLIST) - 1:
            store_code = prof.get("current_store")
            if store_code:
                _log_run(store_code, u.id, st)
                await _notify_viewers_on_finish(context, store_code, u.id, st)
            text = "🎉 Чек-лист завершён!\n\n" + _fmt_progress_text(st)
            await _safe_edit(q, text); return
        st["sec"] += 1; si = st["sec"]

    await _safe_edit(q, _fmt_section_text(si, st), reply_markup=_kb_section(si, st))

# ──────────────────────────────────────────────────────────────────────────────
# Планировщик (JobQueue) — безопасно: только если доступен
# ──────────────────────────────────────────────────────────────────────────────
def _user_now_in_tz(uid: int) -> datetime:
    tz = get_profile(uid).get("tz", "Europe/Moscow")
    try: z = ZoneInfo(tz)
    except Exception: z = ZoneInfo("Europe/Moscow")
    return datetime.now(z)

def _stores_for_user(uid: int) -> set[str]:
    subs = USER_SUBS.get(uid, set())
    if subs and "*" in subs:
        return set(STORE_CATALOG.keys())
    return set(subs or [])

def _recent_runs(days: int) -> dict[str, datetime]:
    cutoff = datetime.now(timezone.utc) - timedelta(days=days)
    last: dict[str, datetime] = {}
    if RUNS_FILE.exists():
        with RUNS_FILE.open("r", encoding="utf-8") as f:
            for line in f:
                try:
                    r = json.loads(line)
                    ts = datetime.fromisoformat(r["ts"]).astimezone(timezone.utc)
                    if ts < cutoff: continue
                    store = r["store"]
                    if store not in last or ts > last[store]:
                        last[store] = ts
                except Exception:
                    continue
    return last

async def job_viewers_weekly(context: ContextTypes.DEFAULT_TYPE):
    recent = _recent_runs(7)
    for uid in list(USER_SUBS.keys()):
        local = _user_now_in_tz(uid)
        if not (local.weekday() == 0 and local.hour == 10):
            continue
        if not (0 <= local.minute <= 7):
            continue
        stores = _stores_for_user(uid)
        if not stores: continue
        not_done = sorted([s for s in stores if s not in recent])
        if not not_done:
            msg = "Еженедельный отчёт: по твоим подпискам всё ОК ✅ (за 7 дней есть прохождения)."
        else:
            pretty = " ".join(not_done)
            msg = f"Еженедельный отчёт: не пройдено за неделю — {pretty}"
        try: await context.bot.send_message(uid, msg)
        except Exception: pass

async def job_viewers_daily(context: ContextTypes.DEFAULT_TYPE):
    recent_today = _recent_runs(1)
    for uid in list(USER_SUBS.keys()):
        local = _user_now_in_tz(uid)
        if not (local.hour == 21):
            continue
        stores = _stores_for_user(uid)
        if not stores: continue
        done = sorted([s for s in stores if s in recent_today])
        not_done = sorted([s for s in stores if s not in recent_today])
        lines = ["Дневная сводка по подпискам:"]
        lines.append("✅ Пройдено: " + ("—" if not done else " ".join(done)))
        lines.append("⏳ Не пройдено: " + ("—" if not not_done else " ".join(not_done)))
        try: await context.bot.send_message(uid, "\n".join(lines))
        except Exception: pass

async def job_auditors_weekly(context: ContextTypes.DEFAULT_TYPE):
    recent = _recent_runs(7)
    for uid, prof in STAFF.items():
        if prof.get("role") != "auditor": continue
        local = _user_now_in_tz(uid)
        if not (local.weekday() == 0 and local.hour == 10 and 0 <= local.minute <= 5):
            continue
        store = prof.get("current_store")
        if not store: continue
        if store in recent:
            continue
        try: await context.bot.send_message(uid, "Напоминание: пройди чек-лист по текущему магазину. (/checklist)")
        except Exception: pass

async def job_auditors_hourly_overdue(context: ContextTypes.DEFAULT_TYPE):
    recent = _recent_runs(7)
    for uid, prof in STAFF.items():
        if prof.get("role") != "auditor": continue
        local = _user_now_in_tz(uid)
        if 22 <= local.hour or local.hour < 8:
            continue
        store = prof.get("current_store")
        if not store: continue
        if store in recent:
            continue
        try: await context.bot.send_message(uid, "⏰ Чек-лист просрочен. Пожалуйста, пройди его. (/checklist)")
        except Exception: pass

# ──────────────────────────────────────────────────────────────────────────────
# Хэндлеры и PTB init
# ──────────────────────────────────────────────────────────────────────────────
async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    u = update.effective_user; prof = get_profile(u.id); _upd_from_user(u, prof)
    await refresh_chat_commands(context.bot, update.effective_chat.id, u.id)

    # Экран выбора роли до регистрации (мастер)
    if not prof.get("approved") and not prof.get("awaiting_approval"):
        text = (
            "👋 <b>Добро пожаловать в @VM_lamoda_bot</b>\n\n"
            "Выбери, кто ты:\n"
            "• <b>Директор / Заместитель</b> — проходишь чек-лист своего магазина.\n"
            "• <b>Наблюдатель (VM, ТОМ, РД)</b> — получаешь отчёты и уведомления по магазинам.\n\n"
            "⬇️ Нажми кнопку ниже:"
        )
        await update.effective_chat.send_message(text, parse_mode="HTML", reply_markup=_kb_role_picker())
        return

    payload = update.message.text.split(maxsplit=1)
    if len(payload) == 2:
        code = payload[1].strip().upper()
        if code in STORE_CATALOG and (is_admin(u.id) or prof.get("role") != "auditor"):
            # аудитору deep-link смену магазина не даём
            prof["current_store"] = code; _save_staff()

    kb = [[InlineKeyboardButton("Проверка", callback_data="ping"),
           InlineKeyboardButton("Чек-лист", callback_data="cl:start")]]
    if _role_for_display(u.id, prof) != "auditor":
        kb.append([InlineKeyboardButton("ТОМ / RD", callback_data="tom:menu")])
    store_line = f"*{prof['current_store']}*" if prof.get("current_store") else "—"
    await update.effective_chat.send_message(
        "Привет! Регистрация с модерацией:\n"
        "• <code>/register &lt;КОД_МАГАЗИНА&gt; &lt;СЕКРЕТ_РОЛИ&gt;</code>\n\n"
        f"Текущий магазин: {store_line}. Роль: *{_role_for_display(u.id, prof)}*.\n"
        "Подписки по ТОМ: /tom  •  Часовой пояс: /settz Europe/Moscow",
        reply_markup=InlineKeyboardMarkup(kb), parse_mode="Markdown"
    )

async def on_button(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    if not q or not q.data: return
    if q.data.startswith("reg:"): return
    if q.data == "ping":
        await q.answer("pong")
        try: await q.edit_message_text("Кнопка работает ✅")
        except BadRequest as e:
            if "Message is not modified" in str(e): await q.answer("Без изменений")
            else: raise
        return
    if q.data == "tom:menu":
        await q.answer()
        try: await q.edit_message_reply_markup(reply_markup=_kb_tom(q.from_user.id))
        except Exception:
            await q.message.reply_text("Выбери группу:", reply_markup=_kb_tom(q.from_user.id))
        return
    if q.data.startswith("cl:"):
        await cl_callback(update, context); return
    if q.data.startswith("tom:"):
        await tom_callbacks(update, context); return

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
    # подписки
    app_.add_handler(CommandHandler("subs", cmd_subs))
    app_.add_handler(CommandHandler("follow", cmd_follow))
    app_.add_handler(CommandHandler("unfollow", cmd_unfollow))
    app_.add_handler(CommandHandler("followall", cmd_followall))
    app_.add_handler(CommandHandler("unfollowall", cmd_unfollowall))
    app_.add_handler(CommandHandler("deactivate", cmd_deactivate))
    app_.add_handler(CommandHandler("subscribe", cmd_admin_subscribe))
    app_.add_handler(CommandHandler("unsubscribe", cmd_admin_unsubscribe))
    app_.add_handler(CommandHandler("subscribeall", cmd_admin_subscribeall))
    app_.add_handler(CommandHandler("unsubscribeall", cmd_admin_unsubscribeall))
    # ТОМ / RD / TZ
    app_.add_handler(CommandHandler("tom", cmd_tom))
    app_.add_handler(CommandHandler("reload_tom", cmd_reload_tom))
    app_.add_handler(CommandHandler("settz", cmd_settz))
    # callbacks
    app_.add_handler(CallbackQueryHandler(role_pick_callback, pattern=r"^role:"))
    app_.add_handler(CallbackQueryHandler(reg_callbacks, pattern=r"^reg:"))
    app_.add_handler(CallbackQueryHandler(cl_callback, pattern=r"^cl:"))
    app_.add_handler(CallbackQueryHandler(tom_callbacks, pattern=r"^tom:"))
    app_.add_handler(CallbackQueryHandler(on_button, block=False))
    return app_

# PTB init + jobs (безопасно)
async def _ptb_init_async():
    global _app, _ptb_ready, BOT_USERNAME
    log("PTB: build application…")
    _app = build_application()
    log("PTB: application.initialize()…")
    await _app.initialize()
    me = await _app.bot.get_me(); BOT_USERNAME = me.username

    # Попробуем включить JobQueue, если доступен
    jq = getattr(_app, "job_queue", None)
    if jq is None:
        log("PTB: JobQueue недоступен — планировщик уведомлений отключён (это ок).")
    else:
        # Раз в час проверяем и отправляем при подходящих локальных условиях
        jq.run_repeating(job_viewers_weekly, interval=3600, first=60)
        jq.run_repeating(job_viewers_daily, interval=3600, first=120)
        jq.run_repeating(job_auditors_weekly, interval=3600, first=180)
        jq.run_repeating(job_auditors_hourly_overdue, interval=3600, first=240)
        log("PTB: JobQueue — задания зарегистрированы.")

    _ptb_ready = True
    log(f"PTB: READY as @{BOT_USERNAME}")

def _ptb_thread_main():
    global _loop, _loop_alive
    _loop = asyncio.new_event_loop(); asyncio.set_event_loop(_loop)
    _loop_alive = True; log("PTB thread: loop created, initializing…")
    try:
        _loop.run_until_complete(_ptb_init_async()); _loop.run_forever()
    except Exception as e:
        log(f"PTB thread ERROR: {e}")
    finally:
        _loop_alive = False; log("PTB thread: exit")

def ensure_ptb_started():
    global _ptb_thread
    if _ptb_thread and _ptb_thread.is_alive(): return
    _ptb_thread = threading.Thread(target=_ptb_thread_main, name="ptb-thread", daemon=True)
    _ptb_thread.start(); log("PTB thread: started")

# ──────────────────────────────────────────────────────────────────────────────
# Flask
# ──────────────────────────────────────────────────────────────────────────────
@app.route("/health")
def health(): return "ok", 200

@app.route("/_loop")
def loop_state():
    info = {
        "is_running": bool(_loop and _loop.is_running()),
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
        "subs_file": str(SUBS_FILE.resolve()),
        "tom_file": str(TOM_FILE.resolve()),
        "runs_file": str(RUNS_FILE.resolve()),
        "user_subs_count": len(USER_SUBS),
        "tom_groups": {k: len(v["codes"]) for k,v in TOM_GROUPS.items()},
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
        r = httpx.get(f"https://api.telegram.org/bot{BOT_TOKEN}/setWebhook", params={"url": target}, timeout=15)
        log(f"setWebhook → {r.status_code} {r.text[:200]}")
        return f"Webhook set to {target}", 200
    except Exception as e:
        log(f"setWebhook ERROR: {e}")
        return f"error: {e}", 500


# ── DB schema & health endpoints ──────────────────────────────────────────────
SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS stores (
  code TEXT PRIMARY KEY,
  name TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS users (
  user_id BIGINT PRIMARY KEY,
  role TEXT NOT NULL CHECK (role IN ('admin','auditor','viewer')),
  default_store TEXT,
  created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS subscriptions (
  user_id BIGINT NOT NULL REFERENCES users(user_id) ON DELETE CASCADE,
  store_code TEXT NOT NULL REFERENCES stores(code) ON DELETE CASCADE,
  PRIMARY KEY (user_id, store_code)
);

CREATE TABLE IF NOT EXISTS checklist_runs (
  id BIGSERIAL PRIMARY KEY,
  store_code TEXT NOT NULL REFERENCES stores(code) ON DELETE CASCADE,
  auditor_id BIGINT NOT NULL REFERENCES users(user_id) ON DELETE SET NULL,
  status TEXT NOT NULL DEFAULT 'in_progress' CHECK (status IN ('in_progress','finished','cancelled')),
  started_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  finished_at TIMESTAMPTZ
);

CREATE TABLE IF NOT EXISTS checklist_items (
  run_id BIGINT NOT NULL REFERENCES checklist_runs(id) ON DELETE CASCADE,
  section INT NOT NULL,
  item_key TEXT NOT NULL,
  state TEXT NOT NULL CHECK (state IN ('✅','❌','⬜️')),
  updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  PRIMARY KEY (run_id, section, item_key)
);
"""

@app.get("/db-ping")
def db_ping():
    try:
        r = exec_sql("SELECT 1", fetch=True)
        return {"ok": True, "result": r[0][0] if r else None}
    except Exception as e:
        return {"ok": False, "error": str(e)}, 500

@app.get("/db-init")
def db_init():
    try:
        exec_sql(SCHEMA_SQL)
        exec_sql("INSERT INTO stores(code, name) VALUES "
                 "('C022','Store_C022'),('C09Z','Store_C09Z') "
                 "ON CONFLICT DO NOTHING;")
        return jsonify({"ok": True, "msg": "schema ensured"})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500
@app.post("/")
def telegram_webhook():
    if not (_loop_alive and _ptb_ready and _app and _loop):
        log("webhook → loop not ready (503)"); return Response("loop not ready", status=503)
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