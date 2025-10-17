import os
import asyncio
import threading
from datetime import datetime

from flask import Flask, request, jsonify, Response
from dotenv import load_dotenv

from telegram import (
    Update,
    InlineKeyboardMarkup,
    InlineKeyboardButton,
    InputMediaPhoto,
)
from telegram.constants import ParseMode
from telegram.ext import (
    Application,
    ApplicationBuilder,
    CommandHandler,
    MessageHandler,
    CallbackQueryHandler,
    ContextTypes,
    filters,
)

# ──────────────────────────────────────────────────────────────────────────────
# ENV & Flask
# ──────────────────────────────────────────────────────────────────────────────
load_dotenv()

BOT_TOKEN = os.getenv("BOT_TOKEN")
ADMIN_ID = int(os.getenv("TELEGRAM_ADMIN_ID", "0"))
BASE_URL = os.getenv("BASE_URL", "").rstrip("/")

app = Flask(__name__)

# ──────────────────────────────────────────────────────────────────────────────
# PTB application (background thread loop)
# ──────────────────────────────────────────────────────────────────────────────
PTB_APP: Application | None = None
PTB_THREAD: threading.Thread | None = None
PTB_READY = False


def build_application() -> Application:
    if not BOT_TOKEN:
        raise RuntimeError("BOT_TOKEN env is empty.")
    return (
        ApplicationBuilder()
        .token(BOT_TOKEN)
        .concurrent_updates(True)
        .build()
    )


async def init_handlers(application: Application) -> None:
    # ↓↓↓ Register handlers
    application.add_handler(CommandHandler("start", h_start))
    application.add_handler(CommandHandler("cancel", h_cancel))
    application.add_handler(CallbackQueryHandler(h_buttons))
    # прием фото/текста ВО ВРЕМЯ чек-листа
    application.add_handler(MessageHandler(filters.PHOTO, h_photo))
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, h_text))


def ptb_worker():
    global PTB_APP, PTB_READY
    async def runner():
        global PTB_READY
        PTB_APP = build_application()
        await init_handlers(PTB_APP)

        # webhook (Render)
        if BASE_URL:
            await PTB_APP.bot.set_webhook(url=f"{BASE_URL}/")
        PTB_READY = True
        await PTB_APP.initialize()
        await PTB_APP.start()
        # WebhookUpdateProcessor is implicit in webhook mode – we just keep loop alive
        while True:
            await asyncio.sleep(3600)

    try:
        asyncio.run(runner())
    except Exception as e:
        PTB_READY = False
        print(f"[PTB ERROR] {e}")


def ensure_thread_started():
    global PTB_THREAD
    if PTB_THREAD and PTB_THREAD.is_alive():
        return
    PTB_THREAD = threading.Thread(target=ptb_worker, daemon=True)
    PTB_THREAD.start()


# ──────────────────────────────────────────────────────────────────────────────
# Checklist data & simple in-memory state
# ──────────────────────────────────────────────────────────────────────────────

# ЕЖЕНЕДЕЛЬНЫЙ чек-лист готовности ТЗ к продажам (по PPTX)
CHECKLIST = [
    # 1. Общее размещение ассортимента
    "1.1 Категории выстроены по утверждённому зонированию магазина",
    "1.1 Коллекции внутри категории разделены по брендам и направлениям",
    "1.1 Зоны перехода между коллекциями оформлены нейтрально (без конфликта брендов)",
    "1.2 Планограммы актуальны и соответствуют наполнению",
    "1.2 Баланс «верхов/низов» соблюдён; развеска начинается с верхов, сохранена комплектность",
    "1.2 При чередовании типов изделий (длинный рукав/низы/короткий рукав) соблюдена логика",
    "1.3 POSM размещены корректно: хедеры категорий и логотипы бренда установлены",
    "1.3 Графика соответствует текущей кампании",
    "1.3 Отсутствующий POSM в заявке на заказ/замену; устаревшее/повреждённое удалено",

    # 2. Кросс-мерчандайзинг и стайлинг
    "2.1 Кросс-мерч (обувь/сумки/рюкзаки/шапки/кепки/фитнес-аксессуары) соответствует бренду, категории и цвету",
    "2.1 Кросс-мерч не перегружает стену/гондолу",
    "2.1 Торцы гондол по потоку оформлены актуальными сезонными аксессуарами под тематику зоны",
    "2.1 В спорт-зоне — фитнес-аксессуары; в футболе — футбольные; в лайфстайле — носки/рюкзаки и т. п.",
    "2.2 Каждый второй фронт поддержан стайлингом или многослойным образом",
    "2.2 Ярлыки спрятаны, крючки по правилу «правой руки»",
    "2.2 Витрины и зал визуально сбалансированы по высоте, цвету и плотности экспозиции",

    # 3. Наполненность и пополнение
    "3.1 Текстиль: размещён от меньшего размера к большему",
    "3.1 Наполнение: текстиль — 6 ед/артикул; верхняя одежда — 4 ед (KM7: текстиль 4, куртки 2)",
    "3.1 Лишние запасы не вынесены в зал",
    "3.2 Обувь: сверху вниз — от большей цены к меньшей; бренды разделены по VM-инструкциям",
    "3.2 Протокольные размеры в наличии: женские 5–6 UK; мужские 8–9 UK",
    "3.2 Пары чистые, шнурки заправлены, ценники выровнены",

    # 4. Манекены
    "4.1 Луки соответствуют погоде/сезону региона, есть многослойность и цветовые акценты",
    "4.1 Обувь сезонная, выделены актуальные тренды",
    "4.2 Манекены закреплены за своей категорией (shop-in-shop)",
    "4.2 Все товары с манекенов доступны в зале полной размерной горкой",
    "4.2 В образах есть бестселлеры магазина/региона; образ завершён (аксессуары/кросс-мерч/цвет)",

    # 5. Витрина
    "5.1 Концепт витрины соответствует актуальной кампании бренда",
    "5.1 Витрина и стекло чистые; декор без пыли",
    "5.1 POSM установлен по стандартам и активностям",
    "5.3 Освещение витрины акцентирует графику/инсталляции, нет пересветов и бликов",
    "5.3 При необходимости корректировки света — заявка в Jira",

    # 6. Чистая кассовая зона
    "6.1 На кассовом столе/в шкафу только актуальные листовки и POS-материалы",
    "6.1 Из товара на кассе — только бренд SOLMATE; без ценников на лицевой стороне",
    "6.2 Аксессуарная зона у кассы соответствует сезону и спросу",
    "6.2 Рюкзаки и сумки аккуратно набиты бумагой/наполнителем",

    # 7. Освещение зала
    "7.1 Все лампы исправны и корректно нацелены",
    "7.1 Фокусные точки: входная экспозиция, фронты, островное оборудование, ключевые POSM, манекены",
    "7.1 Проверка освещения — 1 раз в неделю; при необходимости нацеливания — заявка в Jira",
]

# user_id -> session dict
SESSIONS: dict[int, dict] = {}


def kb_for_question():
    return InlineKeyboardMarkup(
        [
            [InlineKeyboardButton("✅ Ок", callback_data="ans:ok"),
             InlineKeyboardButton("⚠️ Проблема", callback_data="ans:issue")],
            [InlineKeyboardButton("⏭ Пропустить", callback_data="ans:skip"),
             InlineKeyboardButton("🏁 Завершить", callback_data="ans:finish")],
        ]
    )


def kb_next():
    return InlineKeyboardMarkup(
        [[InlineKeyboardButton("➡️ Дальше", callback_data="nav:next")],
         [InlineKeyboardButton("🏁 Завершить", callback_data="ans:finish")]]
    )


def session_get(uid: int) -> dict:
    s = SESSIONS.get(uid)
    if not s:
        s = {
            "idx": 0,                # current question index
            "answers": [],           # list of dicts: {q, status, comment, photos}
            "collect_mode": False,   # True если ждём фото/коммент для "Проблема"
        }
        SESSIONS[uid] = s
    return s


def reset_session(uid: int):
    if uid in SESSIONS:
        del SESSIONS[uid]


# ──────────────────────────────────────────────────────────────────────────────
# Handlers
# ──────────────────────────────────────────────────────────────────────────────

async def h_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    ensure_thread_started()
    uid = update.effective_user.id
    reset_session(uid)

    kb = InlineKeyboardMarkup(
        [[InlineKeyboardButton("🚀 Начать чек-лист", callback_data="start")]]
    )
    await update.effective_message.reply_text(
        "Привет! Бот на вебхуке.\nЭто *Еженедельный чек-лист готовности ТЗ к продажам*.\nНажми «Начать чек-лист».",
        parse_mode=ParseMode.MARKDOWN,
        reply_markup=kb,
    )


async def h_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    reset_session(uid)
    await update.effective_message.reply_text("Окей, чек-лист отменён.")


async def send_question(chat_id: int, context: ContextTypes.DEFAULT_TYPE):
    s = session_get(chat_id)
    idx = s["idx"]

    # если все вопросы пройдены — финал
    if idx >= len(CHECKLIST):
        await finish_and_send(chat_id, context)
        return

    q = CHECKLIST[idx]
    await context.bot.send_message(
        chat_id=chat_id,
        text=f"*Пункт {idx+1}/{len(CHECKLIST)}:*\n{q}",
        parse_mode=ParseMode.MARKDOWN,
        reply_markup=kb_for_question(),
    )


async def h_buttons(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.callback_query:
        return
    q = update.callback_query
    await q.answer()

    uid = q.from_user.id
    s = session_get(uid)
    data = q.data

    if data == "start":
        # инициализация
        s["idx"] = 0
        s["answers"] = []
        s["collect_mode"] = False
        await q.message.edit_text("Поехали. Отвечай по пунктам чек-листа 👇")
        await send_question(uid, context)
        return

    if data.startswith("ans:"):
        action = data.split(":", 1)[1]
        if action == "finish":
            await q.message.edit_reply_markup(None)
            await finish_and_send(uid, context)
            return

        idx = s["idx"]
        # создаём запись для текущего вопроса, если ещё нет
        while len(s["answers"]) <= idx:
            s["answers"].append({"q": CHECKLIST[idx], "status": None, "comment": "", "photos": []})

        rec = s["answers"][idx]

        if action == "ok":
            rec["status"] = "OK"
            s["collect_mode"] = False
            s["idx"] += 1
            await q.message.edit_text(f"✅ Зафиксировано: *ОК*.\n", parse_mode=ParseMode.MARKDOWN)
            await send_question(uid, context)
            return

        if action == "skip":
            rec["status"] = "SKIP"
            s["collect_mode"] = False
            s["idx"] += 1
            await q.message.edit_text(f"⏭ Пропущено.\n")
            await send_question(uid, context)
            return

        if action == "issue":
            rec["status"] = "ISSUE"
            s["collect_mode"] = True
            await q.message.edit_text(
                "⚠️ Пометил как *Проблема*.\n"
                "Пришли *фото* и/или *комментарий*.\n"
                "Готово — жми «Дальше».",
                parse_mode=ParseMode.MARKDOWN,
                reply_markup=kb_next(),
            )
            return

    if data == "nav:next":
        # выходим из режима сбора док-в и идём дальше
        s["collect_mode"] = False
        s["idx"] += 1
        await q.message.edit_text("Принято. Идём дальше ➡️")
        await send_question(uid, context)
        return


async def h_photo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    s = session_get(uid)

    # принимаем фото только если в процессе чек-листа
    if s.get("idx", 0) >= len(CHECKLIST):
        return

    # создаём текущую запись при необходимости
    idx = s["idx"]
    while len(s["answers"]) <= idx:
        s["answers"].append({"q": CHECKLIST[idx], "status": None, "comment": "", "photos": []})
    rec = s["answers"][idx]

    file_id = update.effective_message.photo[-1].file_id
    rec["photos"].append(file_id)

    # если ещё не выставлен статус, считаем как ISSUE
    if not rec["status"]:
        rec["status"] = "ISSUE"
        s["collect_mode"] = True

    await update.effective_message.reply_text("📸 Фото добавлено. Жми «Дальше» когда готов.", reply_markup=kb_next())


async def h_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    s = session_get(uid)
    if s.get("idx", 0) >= len(CHECKLIST):
        return

    idx = s["idx"]
    while len(s["answers"]) <= idx:
        s["answers"].append({"q": CHECKLIST[idx], "status": None, "comment": "", "photos": []})
    rec = s["answers"][idx]

    # свободный текст = комментарий к текущему пункту
    comment = (rec.get("comment") or "").strip()
    if comment:
        comment += " | "
    rec["comment"] = comment + update.effective_message.text.strip()

    # если ещё не выставлен статус — трактуем как ISSUE
    if not rec["status"]:
        rec["status"] = "ISSUE"
        s["collect_mode"] = True

    await update.effective_message.reply_text("✍️ Комментарий добавлен. Жми «Дальше» когда готов.", reply_markup=kb_next())


def render_summary(user_name: str, s: dict) -> str:
    lines = [
        f"*Еженедельный чек-лист готовности ТЗ к продажам*",
        f"_Исполнитель:_ {user_name}",
        f"_Дата:_ {datetime.now().strftime('%Y-%m-%d %H:%M')}",
        "",
    ]
    ok, issues, skipped = 0, 0, 0
    for i, rec in enumerate(s["answers"]):
        st = rec.get("status") or "—"
        if st == "OK":
            ok += 1
            emoji = "✅"
        elif st == "ISSUE":
            issues += 1
            emoji = "⚠️"
        elif st == "SKIP":
            skipped += 1
            emoji = "⏭"
        else:
            emoji = "•"

        cmnt = f"\n    _Комментарий:_ {rec['comment']}" if rec.get("comment") else ""
        ph = f"\n    _Фото:_ {len(rec['photos'])} шт." if rec.get("photos") else ""
        lines.append(f"*{i+1}. {emoji}* {rec['q']}{cmnt}{ph}")

    lines.append("")
    lines.append(f"*Итоги:* ✅ {ok}  ⚠️ {issues}  ⏭ {skipped}")
    return "\n".join(lines)


async def finish_and_send(uid: int, context: ContextTypes.DEFAULT_TYPE):
    s = session_get(uid)
    user = await context.bot.get_chat(uid)
    summary = render_summary(user_name=user.full_name, s=s)

    # отправляем пользователю
    await context.bot.send_message(uid, summary, parse_mode=ParseMode.MARKDOWN)

    # отправляем админу (если задан)
    targets = [uid]
    if ADMIN_ID and ADMIN_ID != uid:
        targets.append(ADMIN_ID)

    # фото сгруппируем по альбомам на каждый пункт, где они есть
    for tgt in targets:
        for i, rec in enumerate(s["answers"]):
            photos = rec.get("photos") or []
            if not photos:
                continue
            # Не больше 10 в одном media_group — Telegram ограничение
            chunk = []
            for fid in photos:
                chunk.append(InputMediaPhoto(media=fid, caption=f"Пункт {i+1}: {rec['q']}" if not chunk else None))
                if len(chunk) == 10:
                    await context.bot.send_media_group(tgt, media=chunk)
                    chunk = []
            if chunk:
                await context.bot.send_media_group(tgt, media=chunk)

    await context.bot.send_message(uid, "Готово! Спасибо 🙌")
    if ADMIN_ID and ADMIN_ID != uid:
        await context.bot.send_message(ADMIN_ID, f"Отчёт от {user.full_name} получен ✅")

    # очистим сессию
    reset_session(uid)


# ──────────────────────────────────────────────────────────────────────────────
# Webhook endpoint for Telegram + service endpoints
# ──────────────────────────────────────────────────────────────────────────────

@app.post("/")
def telegram_webhook():
    if not PTB_READY or PTB_APP is None:
        # Telegram сам ретраит, если 503
        print(">>> webhook: loop not ready (503) — Telegram will retry")
        return Response("loop not ready", status=503)

    try:
        update = Update.de_json(request.get_json(force=True, silent=True), PTB_APP.bot)
    except Exception:
        return Response("bad request", status=400)

    PTB_APP.update_queue.put_nowait(update)
    return Response("ok", status=200)


@app.get("/health")
def health():
    return Response("ok", status=200)


@app.get("/_loop")
def loop_info():
    alive = PTB_THREAD.is_alive() if PTB_THREAD else False
    return f"loop_alive={alive}, is_running={PTB_READY}"


@app.get("/diag")
async def diag():
    info = {
        "ptb_ready": PTB_READY,
        "thread_alive": PTB_THREAD.is_alive() if PTB_THREAD else False,
        "base_url": BASE_URL,
    }
    return jsonify(info)


@app.get("/getwebhookinfo_raw")
def getwebhookinfo_raw():
    if PTB_APP is None:
        return Response("no app", status=503)
    data = asyncio.run(PTB_APP.bot.get_webhook_info())
    return jsonify(data.to_dict())


@app.get("/getwebhookinfo")
def getwebhookinfo():
    if PTB_APP is None:
        return Response("no app", status=503)
    info = asyncio.run(PTB_APP.bot.get_webhook_info())
    return jsonify({
        "url": info.url,
        "has_custom_certificate": info.has_custom_certificate,
        "pending_update_count": info.pending_update_count,
        "last_error_date": info.last_error_date,
        "last_error_message": info.last_error_message,
        "max_connections": info.max_connections,
        "ip_address": info.ip_address,
        "allowed_updates": info.allowed_updates,
    })


@app.get("/set-webhook")
def set_webhook():
    if PTB_APP is None:
        ensure_thread_started()
        return Response("PTB starting, try again in a few seconds", status=202)
    asyncio.run(PTB_APP.bot.set_webhook(url=f"{BASE_URL}/"))
    return Response(f"Webhook set to {BASE_URL}/", status=200)


# ──────────────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    ensure_thread_started()
    app.run(host="0.0.0.0", port=int(os.getenv("PORT", "5000")))

