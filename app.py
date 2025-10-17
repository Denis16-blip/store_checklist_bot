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
    Application, CommandHandler, CallbackQueryHandler, ContextTypes
)

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
# ДАННЫЕ ЧЕК-ЛИСТА (из PPTX)
# ──────────────────────────────────────────────────────────────────────────────
# Структура: список разделов, в разделе: заголовок и список пунктов.
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

# подготовим плоский индекс для прохождения
_FLAT_ITEMS = []
for si, section in enumerate(CHECKLIST):
    for ii, item in enumerate(section["items"]):
        _FLAT_ITEMS.append((si, ii))

TOTAL_STEPS = len(_FLAT_ITEMS)

# состояние по чатам: {chat_id: {"idx": int, "done": set[(si,ii)]}}
_cl_state = {}

def _cl_get(chat_id: int):
    st = _cl_state.get(chat_id)
    if not st:
        st = {"idx": 0, "done": set()}
        _cl_state[chat_id] = st
    return st

def _fmt_step(si: int, ii: int) -> str:
    title = CHECKLIST[si]["title"]
    text = CHECKLIST[si]["items"][ii]
    # человеко-читаемая нумерация
    k = _FLAT_ITEMS.index((si, ii)) + 1
    return f"*{title}*\n\n• {text}\n\nПрогресс: *{k}/{TOTAL_STEPS}*"

def _fmt_progress(done: set) -> str:
    cnt = len(done)
    pct = int(round(100 * cnt / TOTAL_STEPS)) if TOTAL_STEPS else 0
    # короткий отчёт по разделам
    lines = [f"Готово: *{cnt}/{TOTAL_STEPS}* ({pct}%)"]
    for si, section in enumerate(CHECKLIST):
        total = len(section["items"])
        done_i = sum((si, ii) in done for ii in range(total))
        tick = "✅" if done_i == total else ("➖" if done_i else "⬜️")
        lines.append(f"{tick} {section['title']} — {done_i}/{total}")
    return "\n".join(lines)

def _kb_main():
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton("✔ Выполнено", callback_data="cl:done"),
            InlineKeyboardButton("➡ Пропустить", callback_data="cl:skip"),
        ],
        [
            InlineKeyboardButton("📋 Прогресс", callback_data="cl:progress"),
            InlineKeyboardButton("🔁 Сброс", callback_data="cl:reset"),
        ],
    ])

# ──────────────────────────────────────────────────────────────────────────────
# Бизнес-логика бота
# ──────────────────────────────────────────────────────────────────────────────
async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    kb = [[
        InlineKeyboardButton("Проверка", callback_data="ping"),
        InlineKeyboardButton("Чек-лист", callback_data="cl:start"),
    ]]
    await update.effective_chat.send_message(
        "Привет! Бот на вебхуке жив. Нажми кнопку или пришли /start ещё раз.",
        reply_markup=InlineKeyboardMarkup(kb),
    )

async def on_button(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    if not q or not q.data:
        return
    if q.data == "ping":
        await q.answer("pong")
        await q.edit_message_text("Кнопка работает ✅")
        return
    # обработка чек-листа
    if q.data.startswith("cl:"):
        await cl_callback(update, context)
        return

# ───────────── Чек-лист: команды/коллбеки ─────────────
async def cmd_checklist(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    st = _cl_get(chat_id)
    si, ii = _FLAT_ITEMS[st["idx"]]
    await update.effective_chat.send_message(
        _fmt_step(si, ii),
        reply_markup=_kb_main(),
        parse_mode="Markdown",
    )

async def cl_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    chat_id = q.message.chat_id
    st = _cl_get(chat_id)

    action = q.data.split(":", 1)[1]
    if action == "start":
        st["idx"] = 0
        st["done"] = set()
        si, ii = _FLAT_ITEMS[st["idx"]]
        await q.answer("Поехали!")
        await q.edit_message_text(
            _fmt_step(si, ii),
            reply_markup=_kb_main(),
            parse_mode="Markdown",
        )
        return

    if action == "done":
        si, ii = _FLAT_ITEMS[st["idx"]]
        st["done"].add((si, ii))
        # падение к следующему пункту
        st["idx"] = min(st["idx"] + 1, TOTAL_STEPS - 1)

    elif action == "skip":
        st["idx"] = min(st["idx"] + 1, TOTAL_STEPS - 1)

    elif action == "reset":
        st["idx"] = 0
        st["done"] = set()
        await q.answer("Сброшено.")
    elif action == "progress":
        await q.answer("Прогресс")
        text = _fmt_progress(st["done"])
        await q.edit_message_text(
            text + "\n\nНажми «➡ Пропустить» или «✔ Выполнено», чтобы продолжить.",
            reply_markup=_kb_main(),
            parse_mode="Markdown",
        )
        return

    # проверяем, не закрыли ли всё
    if len(st["done"]) == TOTAL_STEPS:
        text = "🎉 Чек-лист завершён!\n\n" + _fmt_progress(st["done"])
        await q.edit_message_text(text, parse_mode="Markdown")
        return

    si, ii = _FLAT_ITEMS[st["idx"]]
    await q.edit_message_text(
        _fmt_step(si, ii),
        reply_markup=_kb_main(),
        parse_mode="Markdown",
    )

def build_application() -> Application:
    app_ = Application.builder().token(BOT_TOKEN).build()
    app_.add_handler(CommandHandler("start", cmd_start))
    app_.add_handler(CommandHandler("checklist", cmd_checklist))
    app_.add_handler(CallbackQueryHandler(on_button))
    # отдельный хендлер на всякий случай, если прилетят cl:* напрямую
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
    await _app.initialize()       # регистрирует хэндлеры, готовит bot/session
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
        # дальше loop просто живёт; никаких polling/start() нам не нужно
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
    info = {
        "loop_alive": _loop_alive,
        "loop_is_running": bool(_loop and _loop.is_running()),
        "ptb_ready": _ptb_ready,
        "has_application": _app is not None,
        "now": datetime.utcnow().isoformat(timespec="seconds") + "Z",
        "checklist_total_steps": TOTAL_STEPS,
    }
    return app.response_class(json.dumps(info, ensure_ascii=False, indent=2), mimetype="application/json")

@app.route("/getwebhookinfo_raw")
def getwebhookinfo_raw():
    """Прямой вызов Telegram API без PTB/loop — для диагностики."""
    try:
        r = httpx.get(f"https://api.telegram.org/bot{BOT_TOKEN}/getWebhookInfo", timeout=10)
        return app.response_class(r.text, mimetype="application/json", status=r.status_code)
    except Exception as e:
        return f"error: {e}", 500

@app.route("/set-webhook")
def set_webhook():
    """Удобно дергать из браузера после деплоя."""
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
    """Телега шлёт JSON сюда. Гоним апдейт в PTB через loop из фонового потока."""
    if not (_loop_alive and _ptb_ready and _app and _loop):
        # Телега сама ретраит; отдаём 503, пока PTB не готов.
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
    # гарантируем запуск фонового потока как только приходит первый запрос
    ensure_ptb_started()

if __name__ == "__main__":
    # локальный запуск
    ensure_ptb_started()
    app.run(host="0.0.0.0", port=int(os.getenv("PORT", "5000")))

