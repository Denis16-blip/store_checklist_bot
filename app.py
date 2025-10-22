import os
import html as pyhtml
import logging
from typing import Dict, Any, List, Tuple

from flask import Flask, request, jsonify
from telegram import (
    Update,
    InlineKeyboardMarkup,
    InlineKeyboardButton,
)
from telegram.constants import ParseMode
from telegram.error import BadRequest
from telegram.ext import (
    Application,
    ApplicationBuilder,
    CommandHandler,
    CallbackQueryHandler,
    ContextTypes,
)

# -------------------- Конфигурация --------------------

BOT_TOKEN = os.getenv("BOT_TOKEN", "")
WEBHOOK_SECRET = os.getenv("WEBHOOK_SECRET", "")  # если используешь
EXTERNAL_BASE_URL = os.getenv("EXTERNAL_BASE_URL", "")  # твой https URL без /bot

STORE_CATALOG: Dict[str, str] = {
    "MSK01": "Москва, ТЦ «Пример»",
    "SPB02": "Санкт-Петербург, ТРК «Образец»",
    "EKB03": "Екатеринбург, ТЦ «Урал»",
    # добавь свои коды
}

# -------------------- Чек-лист --------------------

CHECKLIST: List[Dict[str, Any]] = [
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
            "Экспозиции визуально сбалансированы по высоте, цвету и плотности.",
        ],
    },
    {
        "title": "3. НАПОЛНЕННОСТЬ И ПОПОЛНЕНИЕ",
        "items": [
            "Текстиль размещён от меньшего размера к большему; нормативы по единицам соблюдены.",
            "Лишние запасы не на зале.",
            "Обувь сверху вниз — от большей цены к меньшей; протокольные размеры присутствуют.",
            "Пары чистые, шнурки заправлены, ценники выровнены.",
        ],
    },
    {
        "title": "4. МАНЕКЕНЫ",
        "items": [
            "Луки по погоде; есть цветовые акценты и многослойность.",
            "Обувь по сезону/спросу; выделены тренды.",
            "Манекены закреплены за категориями; товары из образа доступны в зале; есть бестселлеры.",
            "Образ завершён: аксессуары/кросс-мерч/цветовая логика.",
        ],
    },
    {
        "title": "5. ВИТРИНА",
        "items": [
            "Концепт витрины соответствует актуальной кампании бренда.",
            "Витрина чистая, стекло без следов, декор не пыльный.",
            "POSM установлен по стандартам и маркетинговым активностям.",
            "Свет на витрине без пересветов, бликов и теней.",
            "При необходимости корректировки света — заявка в Jira.",
        ],
    },
    {
        "title": "6. ЧИСТАЯ КАССОВАЯ ЗОНА",
        "items": [
            "На кассовом столе и в кассовом шкафу только актуальные листовки и POS-материалы.",
            "Из товара — только бренд SOLMATE; ценников на лицевой стороне нет.",
            "Аксессуарная зона соответствует сезону и спросу.",
            "Рюкзаки и сумки аккуратно набиты бумагой/наполнителем.",
        ],
    },
    {
        "title": "7. ОСВЕЩЕНИЕ",
        "items": [
            "Все лампы исправны и направлены корректно.",
            "Фокус на: входную экспозицию, фронты, островное оборудование, крупные POSM и манекены.",
            "Проверка освещения — раз в неделю; при необходимости нацеливания — заявка в Jira.",
        ],
    },
]

# Примеры фото на пункты (если нужно)
EXAMPLE_PHOTOS: Dict[int, List[str]] = {
    # 0: ["<file_id>"], ...
}

# -------------------- Инфраструктура --------------------

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("bot")

app = Flask(__name__)
application: Application = ApplicationBuilder().token(BOT_TOKEN).build()

# Универсальная безопасная отправка
async def safe_send(chat, text: str, *, parse_html: bool = True, **kwargs):
    try:
        if parse_html:
            return await chat.send_message(
                text, parse_mode=ParseMode.HTML, **kwargs
            )
        return await chat.send_message(text, **kwargs)
    except BadRequest:
        # Фолбэк без форматирования, чтобы никогда не падать
        return await chat.send_message(text=pyhtml.unescape(text), **kwargs)

def h(s: str) -> str:
    return pyhtml.escape(s, quote=False)

# -------------------- Состояние пользователя --------------------

def _user_state(ctx: ContextTypes.DEFAULT_TYPE) -> Dict[str, Any]:
    ud = ctx.user_data
    if "idx" not in ud:
        ud.update({"section": 0, "item": 0, "answers": {}, "idx": (0, 0)})
    return ud

def _next_item(section: int, item: int) -> Tuple[int, int, bool]:
    if section >= len(CHECKLIST):
        return section, item, True
    items = CHECKLIST[section]["items"]
    if item + 1 < len(items):
        return section, item + 1, False
    if section + 1 < len(CHECKLIST):
        return section + 1, 0, False
    return section, item, True

def _progress_text(section: int, item: int) -> str:
    total_sections = len(CHECKLIST)
    total_items = sum(len(s["items"]) for s in CHECKLIST)
    passed_items = sum(
        len(CHECKLIST[s]["items"]) if s < section else (item) if s == section else 0
        for s in range(total_sections)
    )
    return f"Прогресс: {passed_items}/{total_items}"

def _render_item(section: int, item: int) -> str:
    s = CHECKLIST[section]
    title = s["title"]
    text = s["items"][item]
    return (
        f"<b>{h(title)}</b>\n"
        f"<i>{h(_progress_text(section, item))}</i>\n\n"
        f"• {h(text)}"
    )

def _kb(section: int, item: int) -> InlineKeyboardMarkup:
    buttons = [
        [
            InlineKeyboardButton("✅ Да", callback_data=f"ans:{section}:{item}:yes"),
            InlineKeyboardButton("❌ Нет", callback_data=f"ans:{section}:{item}:no"),
        ],
        [
            InlineKeyboardButton("➡️ Далее", callback_data=f"next:{section}:{item}"),
        ],
    ]
    return InlineKeyboardMarkup(buttons)

# -------------------- Хендлеры --------------------

async def cmd_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    ud = _user_state(ctx)
    ud["section"], ud["item"] = 0, 0
    text = (
        "<b>Ежедневный чек-лист готовности ТЗ к продажам</b>\n\n"
        "Нажимай кнопки под каждым пунктом. Можно просто жать «Далее», если не хочешь фиксировать ответ.\n\n"
        f"{h(_progress_text(0, 0))}"
    )
    await safe_send(update.effective_chat, text)
    await safe_send(
        update.effective_chat,
        _render_item(0, 0),
        reply_markup=_kb(0, 0),
    )

async def cmd_whoami(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    role = ctx.user_data.get("role", "VIEWER")
    store = ctx.user_data.get("store", "—")
    msg = (
        f"<b>Вы:</b> {h(user.full_name)} (id {user.id})\n"
        f"<b>Роль:</b> {h(role)}\n"
        f"<b>Магазин:</b> {h(store)}"
    )
    await safe_send(update.effective_chat, msg)

async def cmd_setstore(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    args = (update.message.text or "").split()
    if len(args) < 2:
        await safe_send(update.effective_chat, "Использование: <code>/setstore CODE</code>")
        return
    code = args[1].strip().upper()
    if code not in STORE_CATALOG:
        await safe_send(update.effective_chat, f"Не знаю код <b>{h(code)}</b>. Доступно: {', '.join(map(h, STORE_CATALOG))}")
        return
    ctx.user_data["store"] = f"{code} — {STORE_CATALOG[code]}"
    await safe_send(
        update.effective_chat,
        f"Ок! Текущий магазин: <b>{h(code)}</b> — {h(STORE_CATALOG[code])}",
    )

async def cb(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()  # мгновенно убираем «часики»
    data = q.data or ""
    ud = _user_state(ctx)
    section, item = ud.get("section", 0), ud.get("item", 0)

    try:
        kind, s, i, *tail = data.split(":")
        s, i = int(s), int(i)
    except Exception:
        kind = "noop"

    # Зафиксировать «Да/Нет»
    if data.startswith("ans:"):
        ans = tail[0] if tail else "yes"
        ud.setdefault("answers", {}).setdefault(s, {})[i] = ans
        await q.edit_message_text(
            _render_item(s, i) + f"\n\nОтвет: <b>{'Да' if ans=='yes' else 'Нет'}</b>",
            parse_mode=ParseMode.HTML,
            reply_markup=_kb(s, i),
        )
        return

    # Перейти к следующему
    if data.startswith("next:"):
        ns, ni, done = _next_item(s, i)
        ud["section"], ud["item"] = ns, ni
        if done:
            total_yes = sum(
                1
                for si, items in ud.get("answers", {}).items()
                for _, v in items.items()
                if v == "yes"
            )
            total = sum(len(x["items"]) for x in CHECKLIST)
            await q.edit_message_text(
                f"<b>Готово!</b>\nИтог: {total_yes}/{total} «Да».",
                parse_mode=ParseMode.HTML,
            )
        else:
            await q.edit_message_text(
                _render_item(ns, ni),
                parse_mode=ParseMode.HTML,
                reply_markup=_kb(ns, ni),
            )
        return

    # На всякий случай
    await q.edit_message_reply_markup(reply_markup=_kb(section, item))

# -------------------- Flask webhook --------------------

@app.route("/health")
def health():
    return "OK", 200

@app.post("/")
def webhook():
    if WEBHOOK_SECRET and request.headers.get("X-Telegram-Bot-Api-Secret-Token") != WEBHOOK_SECRET:
        return jsonify({"ok": False}), 403
    data = request.get_json(force=True, silent=True) or {}
    application.update_queue.put_nowait(Update.de_json(data, application.bot))
    return jsonify(ok=True)

# -------------------- Инициализация PTB --------------------

@app.before_first_request
def _setup_bot():
    # Запускаем PTB в фоновом режиме (без create_task до initialize)
    async def runner():
        await application.initialize()
        await application.start()
        # Вебхук на наш Flask endpoint
        if EXTERNAL_BASE_URL:
            await application.bot.set_webhook(
                url=f"{EXTERNAL_BASE_URL}/",
                secret_token=WEBHOOK_SECRET or None,
                allowed_updates=["message", "callback_query"],
            )
        log.info("PTB: READY")

    import asyncio
    loop = asyncio.get_event_loop()
    loop.create_task(runner())

# Роуты команд/колбэков
application.add_handler(CommandHandler("start", cmd_start))
application.add_handler(CommandHandler("whoami", cmd_whoami))
application.add_handler(CommandHandler("setstore", cmd_setstore))
application.add_handler(CallbackQueryHandler(cb))

if __name__ == "__main__":
    # Локальный запуск без вебхука (polling)
    async def main():
        await application.initialize()
        await application.start()
        await application.bot.delete_webhook(drop_pending_updates=True)
        await application.updater.start_polling(allowed_updates=["message", "callback_query"])
        await application.updater.stop()
        await application.stop()
        await application.shutdown()

    import asyncio
    asyncio.run(main())

