
import os
import asyncio
import traceback
from threading import Thread, Event
from datetime import datetime
from typing import List, Dict, Any, Optional

from dotenv import load_dotenv
from flask import Flask, request, jsonify, Response

from telegram import (
    Update,
    InlineKeyboardMarkup,
    InlineKeyboardButton,
    InputMediaPhoto,
)
from telegram.constants import ParseMode
from telegram.ext import (
    Application,
    CommandHandler,
    CallbackQueryHandler,
    MessageHandler,
    ContextTypes,
    filters,
)

# ──────────────────────────────────────────────────────────────────────────────
# ENV & Flask
# ──────────────────────────────────────────────────────────────────────────────
load_dotenv()

BOT_TOKEN = os.getenv("BOT_TOKEN")
ADMIN_ID = os.getenv("TELEGRAM_ADMIN_ID")
BASE_URL = os.getenv("BASE_URL", "").rstrip("/")

if not BOT_TOKEN:
    raise RuntimeError("BOT_TOKEN is not set")
if not BASE_URL:
    raise RuntimeError("BASE_URL is not set (e.g. https://store-checklist-bot.onrender.com)")

ADMIN_ID_INT: Optional[int] = None
try:
    if ADMIN_ID:
        ADMIN_ID_INT = int(ADMIN_ID)
except Exception:
    raise RuntimeError("TELEGRAM_ADMIN_ID must be an integer")

app = Flask(__name__)

# ──────────────────────────────────────────────────────────────────────────────
# PTB loop/thread globals
# ──────────────────────────────────────────────────────────────────────────────
ptb_loop: asyncio.AbstractEventLoop | None = None
application: Application | None = None
ptb_ready = Event()             # PTB полностью поднят
last_ptb_error: Optional[str] = None  # текст последней ошибки старта

# ──────────────────────────────────────────────────────────────────────────────
# ЧЕК-ЛИСТ (СТРОГО ПО PPTX, разбивка на тезисы)
# ──────────────────────────────────────────────────────────────────────────────
SECTIONED_ITEMS: List[Dict[str, Any]] = [
    {
        "section": "1. ОБЩЕЕ РАЗМЕЩЕНИЕ АССОРТИМЕНТА",
        "items": [
            {
                "code": "1.1",
                "title": "ЗОНИРОВАНИЕ И КАТЕГОРИИ",
                "subtexts": [
                    "КАТЕГОРИИ ВЫСТРОЕНЫ СОГЛАСНО УТВЕРЖДЁННОМУ ЗОНИРОВАНИЮ МАГАЗИНА.",
                    "КОЛЛЕКЦИИ ВНУТРИ КАТЕГОРИИ РАЗДЕЛЕНЫ *ПО БРЕНДАМ И НАПРАВЛЕНИЯМ*",
                    "ЗОНЫ ПЕРЕХОДА МЕЖДУ КОЛЛЕКЦИЯМИ ОФОРМЛЕНЫ НЕЙТРАЛЬНО, БЕЗ ВИЗУАЛЬНОГО КОНФЛИКТА БРЕНДОВ.",
                ],
            },
            {
                "code": "1.2",
                "title": "ПЛАНОГРАММЫ И БАЛАНС НА ОБОРУДОВАНИИ",
                "subtexts": [
                    "ПЛАНОГРАММЫ АКТУАЛЬНЫ И СООТВЕТСТВУЮТ НАПОЛНЕНИЮ.",
                    "БАЛАНС ВЕРХОВ И НИЗОВ СОБЛЮДЁН  РАЗВЕСКА НАЧИНАЕТСЯ С ВЕРХОВ, СОБЛЮДЕНА КОМПЛЕКТНОСТЬ ПРИ ЧЕРЕДОВАНИИ ПРОДУКТ ТИПОВ (ПРИМЕР: ДЛИННЫЙ РУКАВ/НИЗЫ/КОРОТКИЙ РУКАВ))",
                ],
            },
            {
                "code": "1.3",
                "title": "POSM И КОММУНИКАЦИЯ",
                "subtexts": [
                    "POSM РАЗМЕЩЕНЫ КОРРЕКТНО: ХЕДЕРЫ  КАТЕГОРИИ И ЛОГОТИПЫ БРЕНДА УСТАНОВЛЕНЫ",
                    "ГРАФИКА СООТВЕТСТВУЕТ ТЕКУЩЕЙ КАМПАНИИ.",
                    "ОТСУТСТВУЮЩИЙ POSM — В ЗАЯВКЕ НА ЗАКАЗ ИЛИ ЗАМЕНУ.",
                    "УСТАРЕВШИЕ И ПОВРЕЖДЁННЫЕ МАТЕРИАЛЫ УДАЛЯЮТСЯ НЕМЕДЛЕННО.",
                ],
            },
        ],
    },
    {
        "section": "2. КРОСС-МЕРЧАНДАЙЗИНГ И СТАЙЛИНГ",
        "items": [
            {
                "code": "2.1",
                "title": "КРОСС-МЕРЧАНДАЙЗИНГ",
                "subtexts": [
                    "КРОСС-МЕРЧ (ОБУВЬ, СУМКИ, РЮКЗАКИ, ШАПКИ, КЕПКИ, ФИТНЕС-АКСЕССУАРЫ) РАЗМЕЩЁНЫ КОРРЕКТНО: СООТВЕТСТВУЕТ БРЕНДУ, КАТЕГОРИИ И ЦВЕТУ.",
                    "НЕ ПЕРЕГРУЖАЕТ СТЕНУ ИЛИ ГОНДОЛУ.",
                    "НА ТОРЦАХ ГОНДОЛ ПО ПОТОКУ ПОКУПАТЕЛЕЙ РАЗМЕЩЕНЫ АКТУАЛЬНЫЕ АКСЕССУАРЫ, СООТВЕТСТВУЮЩИЕ СЕЗОНУ И ТЕМАТИКЕ ЗОНЫ:СПОРТ-ЗОНА — ФИТНЕС-АКСЕССУАРЫ/ ФУТБОЛ — ФУТБОЛЬНЫЕ АКСЕССУАРЫ / СПОРТСТИЛЬ/ЛАЙФСТАЙЛ — НОСКИ, РЮКЗАКИ И АКСЕССУАРЫ ПОДХОДЯЩИЕ К КАТЕГОРИИ СПОРТИВНЫЙ СТИЛЬ и  тд",
                ],
            },
            {
                "code": "2.2",
                "title": "СТАЙЛИНГ",
                "subtexts": [
                    "КАЖДЫЙ ВТОРОЙ ФРОНТ ПОДДЕРЖАН СТАЙЛИНГОМ ИЛИ МНОГОСЛОЙНЫМ ОБРАЗОМ.",
                    "ВСЕ ЯРЛЫКИ СПРЯТАНЫ.",
                    "КРЮЧКИ НАПРАВЛЕНЫ ПО ПРАВИЛУ “ПРАВОЙ РУКИ”.",
                    "ВИТРИНЫ И ЗАЛЫ ВИЗУАЛЬНО СБАЛАНСИРОВАНЫ ПО ВЫСОТЕ, ЦВЕТУ И ПЛОТНОСТИ ЭКСПОЗИЦИИ.",
                ],
            },
        ],
    },
    {
        "section": "3. НАПОЛНЕННОСТЬ И ПОПОЛНЕНИЕ",
        "items": [
            {
                "code": "3.1",
                "title": "ТЕКСТИЛЬ",
                "subtexts": [
                    "РАЗМЕЩЁН ОТ МЕНЬШЕГО РАЗМЕРА К БОЛЬШЕМУ.",
                    "НАПОЛНЕНИЕ: ТЕКСТИЛЬ — 6 ЕД. НА АРТИКУЛ. ВЕРХНЯЯ ОДЕЖДА — 4 ЕД.  /  KM7  — ТЕКСТИЛЬ 4 ЕД; КУРТКИ — 2 ЕД.",
                    "ЛИШНИЕ ЗАПАСЫ НЕ ВЫНОСЯТСЯ НА ЗАЛ.",
                ],
            },
            {
                "code": "3.2",
                "title": "ОБУВЬ",
                "subtexts": [
                    "РАЗМЕЩЕНА ОТ БОЛЬШЕЙ ЦЕНЫ К МЕНЬШЕЙ (СВЕРХУ ВНИЗ).",
                    "БРЕНДЫ ЧЁТКО РАЗДЕЛЕНЫ ПО VM-ИНСТРУКЦИЯМ.",
                    "ПРИСУТСТВУЮТ ПРОТОКОЛЬНЫЕ РАЗМЕРЫ:   * ЖЕНСКИЕ — 5–6 UK.   * МУЖСКИЕ — 8–9 UK.",
                    "ВСЕ ПАРЫ ЧИСТЫЕ, ШНУРКИ ЗАПРАВЛЕНЫ, ЦЕННИКИ ВЫРОВНЕНЫ.",
                ],
            },
        ],
    },
    {
        "section": "4. МАНЕКЕНЫ",
        "items": [
            {
                "code": "4.1",
                "title": "АКТУАЛЬНОСТЬ И СЕЗОННОСТЬ",
                "subtexts": [
                    "ЛУКИ СООТВЕТСТВУЮТ ПОГОДНЫМ УСЛОВИЯМ РЕГИОНА.",
                    "ПРИСУТСТВУЕТ МНОГОСЛОЙНОСТЬ И ЦВЕТОВЫЕ АКЦЕНТЫ.",
                    "ОБУВЬ СООТВЕТСТВУЕТ СЕЗОНУ И СПРОСУ, ВЫДЕЛЕНЫ АКТУАЛЬНЫЕ ТРЕНДЫ",
                ],
            },
            {
                "code": "4.2",
                "title": "СООТВЕТСТВИЕ МАНЕКЕНОВ SHOP-IN-SHOP",
                "subtexts": [
                    "КАЖДЫЙ МАНЕКЕН ЗАКРЕПЛЁН ЗА СВОЕЙ КАТЕГОРИЕЙ.",
                    "ВСЕ ТОВАРЫ С МАНЕКЕНОВ ДОСТУПНЫ В ЗАЛЕ В ПОЛНОЙ РАЗМЕРНОЙ ГОРКЕ",
                    "В ЛУКАХ ЕСТЬ БЕСТСЕЛЛЕРЫ МАГАЗИНА ИЛИ РЕГИОНА.",
                    "ОБРАЗ ЗАВЕРШЁН — АКСЕССУАРЫ, КРОСС-МЕРЧ И ЦВЕТОВАЯ ЛОГИКА СОБЛЮДЕНЫ.",
                ],
            },
        ],
    },
    {
        "section": "5. ВИТРИНА",
        "items": [
            {
                "code": "5.1",
                "title": "ОФОРМЛЕНИЕ",
                "subtexts": [
                    "КОНЦЕПТ ВИТРИНЫ СООТВЕТСТВУЕТ АКТУАЛЬНОЙ КАМПАНИИ БРЕНДА.",
                    "ВИТРИНА ЧИСТАЯ, СТЕКЛО БЕЗ СЛЕДОВ, ДЕКОР НЕ ПЫЛЬНЫЙ.",
                    "POSM УСТАНОВЛЕН В СООТВЕТСТВИИ СО СТАНДАРТАМИ И МАРКЕТИНГОВЫМ АКТИВНОСТЯМ.",
                ],
            },
            {
                "code": "5.3",
                "title": "ОСВЕЩЕНИЕ ВИТРИНЫ",
                "subtexts": [
                    "СВЕТ АКЦЕНТИРУЕТ НА ГРАФИКУ И ДОП ИНСТАЛЛЯЦИИ ПРИ НАЛИЧИИ.",
                    "ОТСУТСТВУЮТ ПЕРЕСВЕТЫ, БЛИКИ И ТЕНИ НА СТЕКЛЕ.",
                    "ПРИ НЕОБХОДИМОСТИ КОРРЕКТИРОВКИ СВЕТА — ЗАЯВКА В JIRA.",
                ],
            },
        ],
    },
    {
        "section": "6.    ЧИСТАЯ КАССОВАЯ ЗОНА",
        "items": [
            {
                "code": "6.1",
                "title": "КАССОВЫЙ СТОЛ И I ШКАФ",
                "subtexts": [
                    "ТОЛЬКО АКТУАЛЬНЫЕ ЛИСТОВКИ И POS-МАТЕРИАЛЫ.",
                    "ИЗ ТОВАРА — ТОЛЬКО БРЕНД SOLMATE, БЕЗ ЦЕННИКОВ НА ЛИЦЕВОЙ СТОРОНЕ.",
                ],
            },
            {
                "code": "6.2",
                "title": "АКСЕССУАРНАЯ ЗОНА",
                "subtexts": [
                    "СООТВЕТСТВУЮТ СЕЗОНУ И СПРОСУ.",
                    "РЮКЗАКИ И СУМКИ АККУРАТНО НАБИТЫ БУМАГОЙ ИЗ ПОД ТОВАРА ИЛИ НАПОЛНИТЕЛЕМ.",
                ],
            },
        ],
    },
    {
        "section": "7. ОСВЕЩЕНИЕ",
        "items": [
            {
                "code": "7.0",
                "title": "ПРОВЕРКА ОСВЕЩЕНИЯ",
                "subtexts": [
                    "ВСЕ ЛАМПЫ ИСПРАВНЫ И НАПРАВЛЕНЫ КОРРЕКТНО.",
                    "ФОКУСНЫЕ ТОЧКИ: ВХОДНАЯ ЭКСПОЗИЦИЯ  ФРОНТЫ КАЖДОЕ ОСТРОВНОЕ ОБОРУДОВАНИЕ ОСНОВНЫЕ КРУПНЫЕ   P OSM МАНЕКЕНЫ",
                    "----- ПРОВЕРКА ОСВЕЩЕНИЯ — РАЗ В НЕДЕЛЮ. ПРИ НЕОБХОДИМОСТИ НАЦЕЛИВАНИЯ — СОЗДАТЬ ЗАЯВКУ В JIRA.",
                ],
            },
        ],
    },
]

ALL_ITEMS: List[Dict[str, str]] = []
for block in SECTIONED_ITEMS:
    for it in block["items"]:
        for sub in it["subtexts"]:
            ALL_ITEMS.append(
                {"section": block["section"], "code": it["code"], "title": it["title"], "text": sub}
            )

# ──────────────────────────────────────────────────────────────────────────────
# Кнопки
# ──────────────────────────────────────────────────────────────────────────────
def kb_main():
    return InlineKeyboardMarkup(
        [
            [InlineKeyboardButton("✅ Ок", callback_data="ans_ok"),
             InlineKeyboardButton("⚠️ Проблема", callback_data="ans_issue")],
            [InlineKeyboardButton("⏭ Пропустить", callback_data="ans_skip"),
             InlineKeyboardButton("🏁 Завершить", callback_data="finish")],
        ]
    )

def kb_next_only():
    return InlineKeyboardMarkup([[InlineKeyboardButton("⏭ Дальше", callback_data="next")]])

START_KB = InlineKeyboardMarkup([[InlineKeyboardButton("🚀 Начать чек-лист", callback_data="start_checklist")]])

def render_item_text(item: Dict[str, str]) -> str:
    return f"{item['section']}\n{item['code']} — {item['title']}\n\n{item['text']}"

# ──────────────────────────────────────────────────────────────────────────────
# HANDLERS
# ──────────────────────────────────────────────────────────────────────────────
async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "ЕЖЕДНЕВНЫЙ ЧЕК-ЛИСТ ГОТОВНОСТИ ТОРГОВОГО ЗАЛА К ПРОДАЖАМ\n\nНажми кнопку ниже, чтобы начать.",
        reply_markup=START_KB,
    )

async def cb_start_checklist(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    context.user_data.clear()
    context.user_data.update({"idx": 0, "answers": [], "mode": "idle", "temp_photos": []})
    first = ALL_ITEMS[0]
    await q.message.reply_text(render_item_text(first), reply_markup=kb_main())

async def handle_main_buttons(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    data = q.data
    await q.answer()
    user = context.user_data
    idx = int(user.get("idx", 0))
    if idx >= len(ALL_ITEMS):
        await q.message.reply_text("Все пункты уже пройдены. Нажми «🏁 Завершить».", reply_markup=kb_next_only())
        return
    item = ALL_ITEMS[idx]

    if data == "ans_ok":
        user["answers"].append({**item, "status": "ok"})
        await go_next_or_finish(q, context)
    elif data == "ans_skip":
        user["answers"].append({**item, "status": "skip"})
        await go_next_or_finish(q, context)
    elif data == "ans_issue":
        user["mode"] = "collecting_photos"
        user["temp_photos"] = []
        await q.message.reply_text("⚠️ Зафиксируем проблему по пункту. Пришли 1–10 фото. Когда достаточно — напиши «готово».")
    elif data == "finish":
        await send_summary(q.message.chat_id, context)
    elif data == "next":
        await go_next_or_finish(q, context)

async def go_next_or_finish(q, context: ContextTypes.DEFAULT_TYPE):
    user = context.user_data
    user["idx"] = int(user.get("idx", 0)) + 1
    if user["idx"] >= len(ALL_ITEMS):
        await q.message.reply_text("Пункты закончились. Нажми «🏁 Завершить».",
                                   reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🏁 Завершить", callback_data="finish")]]))
        return
    next_item = ALL_ITEMS[user["idx"]]
    await q.message.reply_text(render_item_text(next_item), reply_markup=kb_main())

async def on_photo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = context.user_data
    if user.get("mode") != "collecting_photos":
        return
    file_id = update.message.photo[-1].file_id
    user.setdefault("temp_photos", []).append(file_id)
    if len(user["temp_photos"]) >= 10:
        await update.message.reply_text("Принял 10 фото (максимум). Напиши комментарий к проблеме.")
        user["mode"] = "collecting_comment"

async def on_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = (update.message.text or "").strip().lower()
    user = context.user_data
    mode = user.get("mode", "idle")

    if mode == "collecting_photos":
        if text in ("готово", "всё", "все", "done", "дальше"):
            user["mode"] = "collecting_comment"
            await update.message.reply_text("Ок. Напиши короткий комментарий к проблеме.")
        else:
            await update.message.reply_text("Пришли фото проблемы. Когда будет достаточно — напиши «готово».")
        return

    if mode == "collecting_comment":
        comment = update.message.text.strip()
        idx = int(user.get("idx", 0))
        if idx >= len(ALL_ITEMS):
            idx = len(ALL_ITEMS) - 1
        item = ALL_ITEMS[idx]
        photos = user.get("temp_photos", [])
        user["answers"].append({**item, "status": "issue", "comment": comment, "photos": photos[:]})
        user["temp_photos"] = []
        user["mode"] = "idle"
        await update.message.reply_text("Записал проблему. Двигаемся дальше.", reply_markup=kb_next_only())
        return

    await update.message.reply_text("Напиши /start, чтобы начать чек-лист, или используй кнопки под текущим пунктом.")

def _format_summary_header(answers: List[Dict[str, Any]]) -> str:
    total = len(answers)
    oks = sum(1 for a in answers if a["status"] == "ok")
    issues = [a for a in answers if a["status"] == "issue"]
    skips = sum(1 for a in answers if a["status"] == "skip")
    return f"Сводка чек-листа\nВсего пунктов: {total}\n✅ Ок: {oks}\n⚠️ Проблем: {len(issues)}\n⏭ Пропущено: {skips}"

async def send_summary(chat_id: int, context: ContextTypes.DEFAULT_TYPE):
    user = context.user_data
    answers = user.get("answers", [])
    if not answers:
        await context.bot.send_message(chat_id, "Нет данных для отчёта. Пройди хотя бы один пункт через кнопки.", reply_markup=START_KB)
        return

    header = _format_summary_header(answers)
    await context.bot.send_message(chat_id, header)

    issues = [a for a in answers if a["status"] == "issue"]
    for it in issues:
        caption = f"{it['section']}\n{it['code']} — {it['title']}\n\n{it.get('text','')}\n\n{it.get('comment','')}"
        photos: List[str] = it.get("photos", []) or []
        if photos:
            media = []
            for j, pid in enumerate(photos[:10]):
                if j == 0:
                    media.append(InputMediaPhoto(pid, caption=caption))
                else:
                    media.append(InputMediaPhoto(pid))
            await context.bot.send_media_group(chat_id=chat_id, media=media)
        else:
            await context.bot.send_message(chat_id, caption)

    await context.bot.send_message(chat_id, "🏁 Отчёт сформирован. Спасибо!")

    if ADMIN_ID_INT and ADMIN_ID_INT != chat_id:
        try:
            await context.bot.send_message(ADMIN_ID_INT, f"📋 Отчёт от пользователя {chat_id}\n\n" + header)
            for it in issues:
                caption = f"{it['section']}\n{it['code']} — {it['title']}\n\n{it.get('text','')}\n\n{it.get('comment','')}"
                photos: List[str] = it.get("photos", []) or []
                if photos:
                    media = []
                    for j, pid in enumerate(photos[:10]):
                        if j == 0:
                            media.append(InputMediaPhoto(pid, caption=caption))
                        else:
                            media.append(InputMediaPhoto(pid))
                    await context.bot.send_media_group(chat_id=ADMIN_ID_INT, media=media)
                else:
                    await context.bot.send_message(ADMIN_ID_INT, caption)
        except Exception:
            pass

def register_handlers(app_: Application):
    app_.add_handler(CommandHandler("start", cmd_start))
    app_.add_handler(CallbackQueryHandler(cb_start_checklist, pattern="^start_checklist$"))
    app_.add_handler(CallbackQueryHandler(handle_main_buttons, pattern="^(ans_ok|ans_issue|ans_skip|finish|next)$"))
    app_.add_handler(MessageHandler(filters.PHOTO, on_photo))
    app_.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, on_text))

# ──────────────────────────────────────────────────────────────────────────────
# PTB bootstrap in dedicated thread (+ автовебхук + логирование ошибок)
# ──────────────────────────────────────────────────────────────────────────────
def _ptb_thread():
    global ptb_loop, application, last_ptb_error
    try:
        print(">>> PTB thread: creating event loop")
        ptb_loop = asyncio.new_event_loop()
        asyncio.set_event_loop(ptb_loop)

        print(">>> PTB thread: building Application")
        application = Application.builder().token(BOT_TOKEN).concurrent_updates(True).build()
        register_handlers(application)

        async def _boot():
            print(">>> PTB boot: initialize()")
            await application.initialize()
            print(">>> PTB boot: start()")
            await application.start()
            # Автоматически ставим вебхук
            url = f"{BASE_URL}/"
            print(f">>> PTB boot: set_webhook({url})")
            await application.bot.set_webhook(url=url)
            ptb_ready.set()
            print(">>> PTB boot: ready")

        ptb_loop.run_until_complete(_boot())
        ptb_loop.run_forever()
    except Exception as e:
        last_ptb_error = f"{e.__class__.__name__}: {e}\n{traceback.format_exc()}"
        print(">>> PTB thread crashed:\n", last_ptb_error)
    finally:
        try:
            ptb_ready.clear()
            if application:
                if ptb_loop and ptb_loop.is_running():
                    pass
                if ptb_loop:
                    ptb_loop.run_until_complete(application.stop())
                    ptb_loop.run_until_complete(application.shutdown())
        except Exception:
            pass
        try:
            if ptb_loop:
                ptb_loop.close()
        except Exception:
            pass
        print(">>> PTB thread: shutdown complete")

def ensure_ptb_thread():
    if not ptb_ready.is_set():
        print(">>> ensure_ptb_thread: starting thread")
        t = Thread(target=_ptb_thread, name="ptb-thread", daemon=True)
        t.start()

# Запускаем PTB-тред при импортe (под gunicorn = 1 воркер)
ensure_ptb_thread()

# ──────────────────────────────────────────────────────────────────────────────
# ГАРАНТИРОВАННЫЙ ПУСК И РУЧНОЙ РЕСТАРТ PTB-ТРЕДА
# ──────────────────────────────────────────────────────────────────────────────
@app.before_first_request
def _kick_ptb_on_first_request():
    try:
        print(">>> before_first_request: ensure_ptb_thread()")
        ensure_ptb_thread()
    except Exception as e:
        print(">>> before_first_request error:", e)

@app.get("/_restart_ptb")
def restart_ptb():
    # если не поднят — поднимем
    if not ptb_ready.is_set():
        print(">>> manual restart: ensure_ptb_thread()")
        ensure_ptb_thread()
        return jsonify({"ok": True, "action": "started"}), 200
    # уже бежит
    return jsonify({"ok": True, "action": "already_running"}), 200

# ──────────────────────────────────────────────────────────────────────────────
# Flask endpoints (все синхронные)
# ──────────────────────────────────────────────────────────────────────────────
@app.get("/health")
def health():
    return Response("ok", status=200, mimetype="text/plain")

@app.get("/")
def index_get():
    return Response("Method Not Allowed", status=405, mimetype="text/plain")

@app.post("/")
def webhook():
    if not (ptb_loop and application and ptb_ready.is_set()):
        print(">>> webhook: loop not ready (503) — Telegram will retry")
        return Response("loop not ready", status=503, mimetype="text/plain")

    data = request.get_json(force=True, silent=True) or {}
    try:
        update = Update.de_json(data, application.bot)
        fut = asyncio.run_coroutine_threadsafe(application.update_queue.put(update), ptb_loop)
        fut.result(timeout=0.2)
    except Exception as e:
        app.logger.exception("webhook error")
        return Response(f"webhook error: {e.__class__.__name__}", status=200, mimetype="text/plain")

    return Response("ok", status=200, mimetype="text/plain")

@app.get("/_loop")
def diag_loop():
    running = bool(ptb_loop and ptb_loop.is_running())
    return jsonify({
        "loop_alive": bool(ptb_loop),
        "is_running": running,
        "ptb_ready": ptb_ready.is_set(),
        "last_ptb_error": last_ptb_error,
        "total_steps": len(ALL_ITEMS),
    }), 200

@app.get("/getwebhookinfo")
def get_webhookinfo():
    if not (ptb_loop and application and ptb_ready.is_set()):
        return jsonify({"error": "ptb_not_ready", "last_ptb_error": last_ptb_error}), 202
    fut = asyncio.run_coroutine_threadsafe(application.bot.get_webhook_info(), ptb_loop)
    info = fut.result(timeout=10)
    return jsonify(info.to_dict()), 200

@app.get("/diag")
def diag():
    d = {
        "time": datetime.utcnow().isoformat() + "Z",
        "env": {"BASE_URL": BASE_URL, "ADMIN_ID": ADMIN_ID},
        "loop_alive": bool(ptb_loop),
        "is_running": bool(ptb_loop and ptb_loop.is_running()),
        "ptb_ready": ptb_ready.is_set(),
        "last_ptb_error": last_ptb_error,
        "items_total": len(ALL_ITEMS),
    }
    return jsonify(d), 200

# ──────────────────────────────────────────────────────────────────────────────
# Local run
# ──────────────────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.getenv("PORT", "10000")))
