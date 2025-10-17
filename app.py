import os
import json
import time
import threading
import asyncio
from datetime import datetime, timezone, timedelta
from typing import Dict, Any, Optional

from flask import Flask, request, jsonify, abort

# --- Telegram / PTB ---
from telegram import Update, BotCommand
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    CallbackQueryHandler,
    ContextTypes,
    filters,
)

# =========================================
# Конфиг
# =========================================
PORT = int(os.getenv("PORT", "10000"))
HOST = "0.0.0.0"

BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()
WEBHOOK_SECRET_TOKEN = os.getenv("WEBHOOK_SECRET_TOKEN", "").strip()
BASE_URL = os.getenv("BASE_URL", "").rstrip("/")  # https://store-checklist-bot.onrender.com

if not BOT_TOKEN:
    raise RuntimeError("BOT_TOKEN env var is empty")

# =========================================
# Глобальные объекты PTB
# =========================================
application: Optional[Application] = None
ptb_loop: Optional[asyncio.AbstractEventLoop] = None
ptb_thread: Optional[threading.Thread] = None
last_ptb_error: Optional[str] = None

# Служебные флаги, чтобы понимать состояние
LOOP_STATE = {
    "is_running": False,
    "loop_alive": False,
    "last_ptb_error": None,
    "ptb_ready": False,
    "total_steps": 0,
}

# =========================================
# Бизнес-логика бота (твои хендлеры)
# =========================================

async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("Привет! Я живой и работаю 🚀")

async def cmd_ping(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("pong")

async def any_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    txt = update.message.text or ""
    await update.message.reply_text(f"Ты сказал: {txt}")

# Кнопки/колбэки если нужны
async def on_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    await q.edit_message_text(f"callback: {q.data}")

def register_handlers(app: Application):
    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("ping", cmd_ping))
    app.add_handler(CallbackQueryHandler(on_callback))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, any_text))

# =========================================
# PTB Thread + Webhook
# =========================================

def _ptb_thread():
    global ptb_loop, application, last_ptb_error
    try:
        print(">>> PTB thread: creating event loop")
        ptb_loop = asyncio.new_event_loop()
        asyncio.set_event_loop(ptb_loop)

        print(">>> PTB thread: building Application")
        # ВАЖНО: уходим от HTTPX → используем AiohttpRequest,
        # чтобы обойти падение в httpcore/AsyncConnectionPool.
        from telegram.request import AiohttpRequest
        req = AiohttpRequest()  # Avoid httpx/httpcore
        application = Application.builder().token(BOT_TOKEN).concurrent_updates(True).request(req).build()
        register_handlers(application)

        # Команды (опционально)
        async def _set_commands():
            await application.bot.set_my_commands(
                [
                    BotCommand("start", "Запуск"),
                    BotCommand("ping", "Проверка бота"),
                ]
            )

        print(">>> PTB thread: setting commands")
        ptb_loop.run_until_complete(_set_commands())

        # Запуск webhook сервера PTB не нужен — мы принимаем POST во Flask
        # и сами прокидываем апдейты внутрь PTB (см. / endpoint ниже).
        LOOP_STATE.update(is_running=True, loop_alive=True, ptb_ready=True)
        print(">>> PTB thread: ready")

        # Держим цикл «живым»
        while LOOP_STATE["is_running"]:
            LOOP_STATE["total_steps"] += 1
            time.sleep(1)

    except Exception as e:
        last_ptb_error = f"{type(e).__name__}: {e}"
        LOOP_STATE.update(ptb_ready=False, is_running=False)
        print(">>> PTB thread crashed:\n ", last_ptb_error)
    finally:
        try:
            if application:
                print(">>> PTB thread: shutdown complete")
        except Exception:
            pass

def ensure_ptb_thread():
    global ptb_thread
    if LOOP_STATE["ptb_ready"]:
        return True
    print(">>> ensure_ptb_thread(): starting...")
    LOOP_STATE.update(is_running=True, loop_alive=False, ptb_ready=False)
    ptb_thread = threading.Thread(target=_ptb_thread, daemon=True)
    ptb_thread.start()
    # чуть ждём, чтобы тред успел собрать Application
    time.sleep(1.0)
    return LOOP_STATE["ptb_ready"]

# =========================================
# Flask
# =========================================
app = Flask(__name__)

@app.get("/health")
def health():
    return "OK"

@app.get("/_loop")
def loop_state():
    return jsonify(
        {
            "is_running": LOOP_STATE["is_running"],
            "loop_alive": LOOP_STATE["loop_alive"],
            "ptb_ready": LOOP_STATE["ptb_ready"],
            "last_ptb_error": last_ptb_error,
            "total_steps": LOOP_STATE["total_steps"],
        }
    )

@app.get("/_restart_ptb")
def restart_ptb():
    print(">>> manual restart: ensure_ptb_thread()")
    ensure_ptb_thread()
    return jsonify({"action": "started", "ok": LOOP_STATE["ptb_ready"]})

@app.post("/")
def telegram_webhook():
    """
    Этот эндпойнт вызывается Telegram'ом. Мы не используем PTB-встроенный веб-сервер.
    Полученный JSON прокидываем в PTB вручную.
    Пока PTB-цикл не готов — отвечаем 503 (Telegram повторит запрос).
    """
    if not LOOP_STATE["ptb_ready"] or application is None or ptb_loop is None:
        print(">>> webhook: loop not ready (503) — Telegram will retry")
        return ("loop not ready", 503)

    try:
        data = request.get_json(force=True, silent=False)
    except Exception:
        abort(400)

    # Валидация secret token, если используешь
    if WEBHOOK_SECRET_TOKEN:
        if request.headers.get("X-Telegram-Bot-Api-Secret-Token") != WEBHOOK_SECRET_TOKEN:
            abort(403)

    # Превращаем JSON в Update и отправляем в очередь PTB
    try:
        update = Update.de_json(data, application.bot)
    except Exception as e:
        print(">>> invalid update:", e)
        abort(400)

    # Выполняем обработку внутри PTB-цикла
    async def _process_update():
        await application.process_update(update)

    fut = asyncio.run_coroutine_threadsafe(_process_update(), ptb_loop)
    _ = fut.result(timeout=30)

    return jsonify({"ok": True})

def main():
    # Стартуем фоновой PTB-тред
    ensure_ptb_thread()

    # Выведем куда стучаться для установки webhook
    if BASE_URL:
        hook_url = f"{BASE_URL}/"
        print(">>> Expected webhook URL:", hook_url)
        print(">>> Secret token set:", bool(WEBHOOK_SECRET_TOKEN))

    # Flask web server
    app.run(host=HOST, port=PORT)

if __name__ == "__main__":
    main()

