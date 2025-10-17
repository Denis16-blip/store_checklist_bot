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
# Бизнес-логика бота (минимум для проверки)
# ──────────────────────────────────────────────────────────────────────────────
async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    kb = [[InlineKeyboardButton("Проверка", callback_data="ping")]]
    await update.effective_chat.send_message(
        "Привет! Бот на вебхуке жив. Нажми кнопку или пришли /start ещё раз.",
        reply_markup=InlineKeyboardMarkup(kb),
    )

async def on_button(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.callback_query.answer("pong")
    await update.callback_query.edit_message_text("Кнопка работает ✅")

def build_application() -> Application:
    app_ = Application.builder().token(BOT_TOKEN).build()
    app_.add_handler(CommandHandler("start", cmd_start))
    app_.add_handler(CallbackQueryHandler(on_button))
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
        # ждать не нужно — главное успешно поставить таску
        fut.add_done_callback(lambda f: None)  # чтобы не висели предупреждения
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
