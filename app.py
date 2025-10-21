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
    MessageHandler, filters,   # ← ДОБАВЛЕНО
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

# ──────────────────────────────────────────────────────────────────────────────
# Состояние чек-листа (поквартально/секциями)
# ──────────────────────────────────────────────────────────────────────────────
# Для каждого чата храним:
# sec — индекс текущего раздела
# marks — словарь {секция: {item_index: True/False/None}}
# None = не отмечено, True = выполнено, False = не выполнено
_cl_state = {}

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
    # Кнопка на каждый пункт: номер + текущий символ
    for ii in range(len(sec["items"])):
        v = sec_marks.get(ii, None)
        sym = "✅" if v is True else ("❌" if v is False else "⬜️")
        label = f"{ii+1} {sym}"
        rows.append([InlineKeyboardButton(label, callback_data=f"cl:toggle:{ii}")])

    # Управление секцией
    controls = [
        InlineKeyboardButton("➡ Далее", callback_data="cl:next"),
        InlineKeyboardButton("↩ Пропустить секцию", callback_data="cl:skip"),
    ]
    rows.append(controls)
    rows.append([
        InlineKeyboardButton("♻️ Сброс секции", callback_data="cl:resetsec"),
        InlineKeyboardButton("📋 Прогресс", callback_data="cl:progress"),
    ])
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
# Бизнес-логика бота
# ──────────────────────────────────────────────────────────────────────────────
async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    kb = [[
        InlineKeyboardButton("Проверка", callback_data="ping"),
        InlineKeyboardButton("Чек-лист", callback_data="cl:start"),
    ]]
    await update.effective_chat.send_message(
        "Привет! Нажми «Чек-лист», чтобы пройти блоками.",
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
    if q.data.startswith("cl:"):
        await cl_callback(update, context)
        return

# ───────────── Чек-лист блочно ─────────────
async def cmd_checklist(update: Update, context: ContextTypes.DEFAULT_TYPE):
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
    chat_id = q.message.chat_id
    st = _cl_get(chat_id)

    action = q.data.split(":", 1)[1]  # всё после "cl:"
    si = st["sec"]

    if action == "start":
        st["sec"] = 0
        st["marks"] = {}
        si = 0
        await q.answer("Поехали!")
        await q.edit_message_text(
            _fmt_section_text(si, st),
            reply_markup=_kb_section(si, st),
            parse_mode="Markdown",
        )
        return

    if action.startswith("toggle:"):
        # переключаем состояние конкретного пункта секции: None -> True -> False -> None
        ii = int(action.split(":")[1])
        sec_marks = st["marks"].setdefault(si, {})
        cur = sec_marks.get(ii, None)
        nxt = True if cur is None else (False if cur is True else None)
        sec_marks[ii] = nxt
        await q.answer("Обновлено")
        await q.edit_message_text(
            _fmt_section_text(si, st),
            reply_markup=_kb_section(si, st),
            parse_mode="Markdown",
        )
        return

    if action == "resetsec":
        st["marks"][si] = {}
        await q.answer("Секция сброшена")
        await q.edit_message_text(
            _fmt_section_text(si, st),
            reply_markup=_kb_section(si, st),
            parse_mode="Markdown",
        )
        return

    if action == "progress":
        await q.answer("Прогресс")
        await q.edit_message_text(
            _fmt_progress_text(st) + "\n\nНажми «➡ Далее», чтобы продолжить.",
            reply_markup=_kb_section(si, st),
            parse_mode="Markdown",
        )
        return

    if action == "skip":
        st["sec"] = min(st["sec"] + 1, len(CHECKLIST) - 1)
        si = st["sec"]

    if action == "next":
        # Переходим к следующей секции
        if si >= len(CHECKLIST) - 1:
            # конец чек-листа
            text = "🎉 Чек-лист завершён!\n\n" + _fmt_progress_text(st)
            await q.edit_message_text(text, parse_mode="Markdown")
            return
        st["sec"] += 1
        si = st["sec"]

    # показать текущую/новую секцию
    await q.edit_message_text(
        _fmt_section_text(si, st),
        reply_markup=_kb_section(si, st),
        parse_mode="Markdown",
    )

# ──────────────────────────────────────────────────────────────────────────────
# 🔎 РЕЖИМ СБОРА file_id ДЛЯ ФОТО (АДМИН)
# ──────────────────────────────────────────────────────────────────────────────
# Для каждого чата можно включить режим сбора,
# чтобы админ мог накидать до N фото и получить их file_id.
_photo_collect_state: dict[int, dict] = {}  # chat_id -> {"active": bool, "target": int, "ids": []}

def _is_admin(update: Update) -> bool:
    user = update.effective_user
    return bool(user and ADMIN_ID and user.id == ADMIN_ID)

async def cmd_photo_ids_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not _is_admin(update):
        return
    chat_id = update.effective_chat.id
    target = 6  # собираем 6 фото
    _photo_collect_state[chat_id] = {"active": True, "target": target, "ids": []}
    await update.effective_chat.send_message(
        f"Режим сбора file_id включён. Пришлите {target} фото (по одному). Я верну file_id каждого. "
        f"Команды: /photo_ids_status, /photo_ids_stop"
    )

async def cmd_photo_ids_stop(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not _is_admin(update):
        return
    chat_id = update.effective_chat.id
    st = _photo_collect_state.get(chat_id)
    if not st or not st.get("active"):
        await update.effective_chat.send_message("Режим сбора уже выключен.")
        return
    st["active"] = False
    await update.effective_chat.send_message("Режим сбора file_id выключен.")

async def cmd_photo_ids_status(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not _is_admin(update):
        return
    chat_id = update.effective_chat.id
    st = _photo_collect_state.get(chat_id, {"active": False, "ids": [], "target": 6})
    await update.effective_chat.send_message(
        f"Состояние: {'включён' if st.get('active') else 'выключен'} | "
        f"Собрано: {len(st.get('ids', []))}/{st.get('target', 6)}\n"
        f"IDs: {json.dumps(st.get('ids', []), ensure_ascii=False)}"
    )

async def on_photo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Реагируем на фото только если режим сбора активен и отправитель — админ."""
    if not _is_admin(update):
        return
    chat_id = update.effective_chat.id
    st = _photo_collect_state.get(chat_id)
    if not st or not st.get("active"):
        return

    msg = update.effective_message
    if not msg or not msg.photo:
        return

    # Берём самое большое превью (последний элемент)
    file_id = msg.photo[-1].file_id
    st["ids"].append(file_id)
    await update.effective_chat.send_message(f"✅ file_id сохранён:\n`{file_id}`", parse_mode="Markdown")

    # Проверим, достигли ли лимита
    if len(st["ids"]) >= st["target"]:
        st["active"] = False
        ids_json = json.dumps(st["ids"], ensure_ascii=False, indent=2)
        await update.effective_chat.send_message(
            "🎯 Собрано нужное количество фото. Режим отключён.\n"
            "Список для копирования:\n"
            f"```\n{ids_json}\n```",
            parse_mode="Markdown",
        )

# ──────────────────────────────────────────────────────────────────────────────
# Регистрация хэндлеров
# ──────────────────────────────────────────────────────────────────────────────
def build_application() -> Application:
    app_ = Application.builder().token(BOT_TOKEN).build()

    # Команды
    app_.add_handler(CommandHandler("start", cmd_start))
    app_.add_handler(CommandHandler("checklist", cmd_checklist))

    # Админ-команды для сбора file_id
    app_.add_handler(CommandHandler("photo_ids_start", cmd_photo_ids_start))
    app_.add_handler(CommandHandler("photo_ids_stop", cmd_photo_ids_stop))
    app_.add_handler(CommandHandler("photo_ids_status", cmd_photo_ids_status))

    # Кнопки
    app_.add_handler(CallbackQueryHandler(on_button))
    app_.add_handler(CallbackQueryHandler(cl_callback, pattern=r"^cl:"))

    # Фото — в самом конце, чтобы не мешать остальному
    app_.add_handler(MessageHandler(filters.PHOTO, on_photo))

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
    # покажем суммарные шаги для справки
    total = sum(len(s["items"]) for s in CHECKLIST)
    info = {
        "loop_alive": _loop_alive,
        "loop_is_running": bool(_loop and _loop.is_running()),
        "ptb_ready": _ptb_ready,
        "has_application": _app is not None,
        "now": datetime.utcnow().isoformat(timespec="seconds") + "Z",
        "checklist_total_items": total,
        "sections": len(CHECKLIST),
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


