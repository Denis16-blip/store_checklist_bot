import os
import asyncio
import threading

from flask import Flask, request
from dotenv import load_dotenv
from telegram import (
    Update, InlineKeyboardButton, InlineKeyboardMarkup,
    ReplyKeyboardRemove, InputMediaPhoto,
)
from telegram.ext import (
    Application, CallbackContext, CommandHandler,
    CallbackQueryHandler, MessageHandler, filters,
)

# ───────────────────────────────────────────────────────────────────────────────
# ENV
load_dotenv()
BOT_TOKEN = os.getenv("BOT_TOKEN")
ADMIN_ID = int(os.getenv("TELEGRAM_ADMIN_ID", "0"))
BASE_URL = os.getenv("BASE_URL", "")  # для /set-webhook

# ───────────────────────────────────────────────────────────────────────────────
# ЧЕК-ЛИСТ
CHECKLIST_BLOCKS = [
    {"code": "assortment","title":"1) Общее размещение ассортимента","items":[
        "Категории выстроены по зонированию",
        "Коллекции разделены по брендам/направлениям",
        "Переходные зоны нейтральны, без конфликта брендов",
    ]},
    {"code": "planograms","title":"2) Планограммы и баланс","items":[
        "Планограммы актуальны и соответствуют наполнению",
        "Баланс верхов/низов соблюдён",
        "Развеска начинается с верхов, соблюдена комплектность",
    ]},
    {"code": "posm","title":"3) POSM и коммуникация","items":[
        "Хедеры/логотипы установлены корректно",
        "Графика соответствует текущей кампании",
        "Устаревший POSM удалён, недостающее — в заявке",
    ]},
    {"code": "styling","title":"4) Стайлинг и кросс-мерч","items":[
        "Каждый второй фронт поддержан образом/слоями",
        "Ярлыки спрятаны, крючки по правилу правой руки",
        "Зоны сбалансированы по высоте/цвету/плотности",
    ]},
    {"code":"mannequins","title":"5) Манекены","items":[
        "Луки по сезону/погоде региона",
        "Товары с манекенов есть в зале (размерная линейка)",
        "Есть бестселлеры, образ завершён (аксессуары/цвет)",
    ]},
    {"code":"window","title":"6) Витрина","items":[
        "Концепт соответствует активной кампании",
        "Чистая витрина: стекло/декор без пыли/следов",
        "Свет без пересветов/бликов, акцент на графике",
    ]},
]

# ───────────────────────────────────────────────────────────────────────────────
# RAM-состояние
USER_STATE = {}

# Flask + PTB
app = Flask(__name__)
application = Application.builder().token(BOT_TOKEN).build()

# ── helpers ────────────────────────────────────────────────────────────────────
def start_payload(uid: int):
    USER_STATE[uid] = {
        "store": None, "current_block": 0, "current_item": 0,
        "answers": {}, "photos": [],
    }

def get_block_and_item(uid: int):
    st = USER_STATE[uid]
    block = CHECKLIST_BLOCKS[st["current_block"]]
    item = block["items"][st["current_item"]]
    return block, item

def kb_yes_no():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("✅ Всё ок", callback_data="ans_ok"),
         InlineKeyboardButton("⚠️ Замечание", callback_data="ans_warn")],
        [InlineKeyboardButton("📸 Приложить фото", callback_data="add_photo")],
    ])

def kb_next():
    return InlineKeyboardMarkup([[InlineKeyboardButton("➡️ Далее", callback_data="next")]])

def format_summary(uid: int):
    st = USER_STATE[uid]
    lines = ["📋 Итоговый отчёт"]
    if st.get("store"): lines.append(f"🏬 Магазин: {st['store']}")
    lines.append("")
    total = ok = 0
    for block in CHECKLIST_BLOCKS:
        code = block["code"]
        ans = st["answers"].get(code, [])
        if not ans: continue
        lines.append(f"*{block['title']}*")
        for a in ans:
            icon = "✅" if a["status"] == "ok" else "⚠️"
            comment = f" — {a['comment']}" if a.get("comment") else ""
            lines.append(f"{icon} {a['item']}{comment}")
            total += 1
            if a["status"] == "ok": ok += 1
        lines.append("")
    score = int((ok/total)*100) if total else 0
    lines.append(f"🔢 Готовность: {score}% ({ok}/{total})")
    return "\n".join(lines), score

# ── handlers ───────────────────────────────────────────────────────────────────
async def cmd_start(update: Update, context: CallbackContext):
    user_id = update.effective_user.id
    start_payload(user_id)
    await update.message.reply_text(
        "Привет! Давай пройдём ежедневный чек-лист.\n\n"
        "Сначала укажи магазин (например: ТЦ МЕГА Казань, или #23):"
    )

async def receive_store(update: Update, context: CallbackContext):
    uid = update.effective_user.id
    if uid not in USER_STATE: start_payload(uid)
    USER_STATE[uid]["store"] = update.message.text.strip()
    block, item = get_block_and_item(uid)
    await update.message.reply_text(
        f"*{block['title']}*\n\nПервый пункт:\n• {item}",
        reply_markup=kb_yes_no(), parse_mode="Markdown"
    )

async def handle_callback(update: Update, context: CallbackContext):
    q = update.callback_query
    await q.answer()
    uid = q.from_user.id
    if uid not in USER_STATE: start_payload(uid)
    st = USER_STATE[uid]
    block, item = get_block_and_item(uid)

    if q.data in ("ans_ok","ans_warn"):
        status = "ok" if q.data=="ans_ok" else "warn"
        st["answers"].setdefault(block["code"], []).append(
            {"item": item, "status": status, "comment": None}
        )
        await q.edit_message_text(
            f"{block['title']}\n\n"
            f"{'✅ Всё ок' if status=='ok' else '⚠️ Замечание'} — {item}\n\n"
            "Есть комментарий? Напиши сообщением или нажми «Далее».",
            reply_markup=kb_next()
        ); return

    if q.data == "add_photo":
        await q.edit_message_text(
            f"{block['title']}\n\nПришли фото как изображение. После — нажми «Далее».",
            reply_markup=kb_next()
        ); return

    if q.data == "next":
        if st["current_item"]+1 < len(block["items"]):
            st["current_item"] += 1
        else:
            st["current_item"] = 0
            st["current_block"] += 1

        if st["current_block"] >= len(CHECKLIST_BLOCKS):
            summary, _ = format_summary(uid)
            await q.edit_message_text(summary, parse_mode="Markdown")
            if st["photos"]:
                media = [InputMediaPhoto(pid) for pid in st["photos"][:10]]
                try: await context.bot.send_media_group(chat_id=uid, media=media)
                except Exception: pass
            if ADMIN_ID:
                try:
                    await context.bot.send_message(chat_id=ADMIN_ID, text=summary, parse_mode="Markdown")
                    if st["photos"]:
                        media = [InputMediaPhoto(pid) for pid in st["photos"][:10]]
                        await context.bot.send_media_group(chat_id=ADMIN_ID, media=media)
                except Exception: pass
            start_payload(uid); return

        block, item = get_block_and_item(uid)
        await q.edit_message_text(
            f"*{block['title']}*\n\nСледующий пункт:\n• {item}",
            reply_markup=kb_yes_no(), parse_mode="Markdown"
        )

async def save_comment(update: Update, context: CallbackContext):
    uid = update.effective_user.id
    if uid not in USER_STATE: return
    st = USER_STATE[uid]
    code = CHECKLIST_BLOCKS[st["current_block"]]["code"]
    if st["answers"].get(code):
        st["answers"][code][-1]["comment"] = update.message.text.strip()
        await update.message.reply_text("📝 Комментарий сохранён. Нажми «Далее».",
                                        reply_markup=ReplyKeyboardRemove())

async def save_photo(update: Update, context: CallbackContext):
    uid = update.effective_user.id
    if not update.message.photo: return
    file_id = update.message.photo[-1].file_id
    USER_STATE.setdefault(uid, {}).setdefault("photos", []).append(file_id)
    await update.message.reply_text("📸 Фото получено. Нажми «Далее».")

application.add_handler(CommandHandler("start", cmd_start))
application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, receive_store), 0)
application.add_handler(CallbackQueryHandler(handle_callback))
application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, save_comment), 1)
application.add_handler(MessageHandler(filters.PHOTO, save_photo))

# ───────────────────────────────────────────────────────────────────────────────
# PTB в отдельном event loop + держим живым
_loop: asyncio.AbstractEventLoop | None = None

def _run_ptb_background():
    global _loop
    _loop = asyncio.new_event_loop()
    asyncio.set_event_loop(_loop)

    async def _boot():
        await application.initialize()
        await application.start()
        print(">>> PTB started")

    _loop.run_until_complete(_boot())
    _loop.run_forever()

# стартуем фоном сразу при импорте модуля (в воркере gunicorn)
threading.Thread(target=_run_ptb_background, daemon=True).start()

# ───────────────────────────────────────────────────────────────────────────────
# Flask routes
@app.post("/")
def webhook():
    """Получаем апдейты от Telegram и ПРОСИМ PTB их обработать."""
    update = Update.de_json(request.get_json(force=True), application.bot)
    # важно: процессим напрямую, а не через update_queue
    asyncio.run_coroutine_threadsafe(application.process_update(update), _loop)
    return "ok", 200

@app.get("/set-webhook")
def set_webhook():
    async def _set():
        await application.bot.set_webhook(f"{BASE_URL}/",
                                          allowed_updates=["message","callback_query"])
    asyncio.get_event_loop().run_until_complete(_set())
    return f"Webhook set to {BASE_URL}/", 200

@app.get("/health")
def health():
    return "ok", 200

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 5000)))
