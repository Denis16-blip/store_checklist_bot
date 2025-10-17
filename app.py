import os
import threading
from typing import Dict, List, Tuple

from flask import Flask, request, Response

from dotenv import load_dotenv
from telegram import (
    Update,
    InlineKeyboardMarkup,
    InlineKeyboardButton,
)
from telegram.ext import (
    Application,
    CommandHandler,
    CallbackQueryHandler,
    MessageHandler,
    ContextTypes,
    filters,
)

# ──────────────────────────────────────────────────────────────────────────────
# ENV & CONFIG
# ──────────────────────────────────────────────────────────────────────────────
load_dotenv()
BOT_TOKEN = os.getenv("BOT_TOKEN", "")
ADMIN_ID = int(os.getenv("TELEGRAM_ADMIN_ID", "0"))
BASE_URL = os.getenv("BASE_URL", "").rstrip("/")

if not BOT_TOKEN:
    raise RuntimeError("BOT_TOKEN not set")

# ──────────────────────────────────────────────────────────────────────────────
# ЧЕК-ЛИСТ: Еженедельный чек-лист готовности ТЗ к продажам
# Структура: список блоков, в каждом блоке список конкретных пунктов
# ──────────────────────────────────────────────────────────────────────────────
CHECKLIST_BLOCKS: List[Dict[str, List[str]]] = [
    {
        "title": "1. Общее размещение ассортимента",
        "items": [
            "1.1 Категории выстроены по утверждённому зонированию магазина",
            "1.1 Коллекции внутри категории разделены по брендам и направлениям",
            "1.1 Зоны перехода между коллекциями оформлены нейтрально (без конфликта брендов)",
            "1.2 Планограммы актуальны и соответствуют наполнению",
            "1.2 Баланс на оборудовании: развеска от «верхов», соблюдена комплектность",
            "1.3 POSM размещены корректно (хедеры, логотипы; графика = текущая кампания)",
            "1.3 Отсутствующий POSM — в заявке на заказ/замену; устаревшее — удалено",
        ],
    },
    {
        "title": "2. Кросс-мерчандайзинг и стайлинг",
        "items": [
            "2.1 Кросс-мерч (обувь/сумки/рюкзаки/шапки/аксесс.) соответствует бренду, категории и цвету",
            "2.1 Торцы гондол по потоку — актуальные аксессуары по сезону/тематике зоны",
            "2.2 Каждый второй фронт поддержан стайлингом/многослойным образом",
            "2.2 Ярлыки спрятаны, крючки по правилу «правой руки»",
            "2.2 Витрины/залы сбалансированы по высоте, цвету, плотности экспозиции",
        ],
    },
    {
        "title": "3. Наполненность и пополнение",
        "items": [
            "3.1 Текстиль: размещён от меньшего размера к большему",
            "3.1 Наполнение (текстиль — 6 ед/артикул; верхняя одежда — 4 ед; KM7: текстиль 4, куртки 2)",
            "3.1 Лишние запасы не вынесены в зал",
            "3.2 Обувь: сверху вниз от большей цены к меньшей, бренды чётко разделены",
            "3.2 Присутствуют протокольные размеры (жен 5–6 UK, муж 8–9 UK)",
            "3.2 Пары чистые, шнурки заправлены, ценники выровнены",
        ],
    },
    {
        "title": "4. Манекены",
        "items": [
            "4.1 Луки соответствуют погоде/сезону региона, есть многослойность и цветовые акценты",
            "4.1 Обувь сезонная, выделены актуальные тренды",
            "4.2 Манекены закреплены за своей категорией (shop-in-shop)",
            "4.2 Все товары с манекенов доступны в зале полной размерной горкой",
            "4.2 В образах есть бестселлеры магазина/региона, образ завершён (аксессуары/кросс-мерч/цвет)",
        ],
    },
    {
        "title": "5. Витрина",
        "items": [
            "5.1 Концепт витрины соответствует актуальной кампании бренда",
            "5.1 Витрина и стекло чистые; декор без пыли",
            "5.1 POSM установлен по стандартам и маркетинговым активностям",
            "5.3 Освещение витрины: акценты на графику/инсталляции, нет пересветов и бликов",
            "5.3 При необходимости корректировки света — заявка (Jira)",
        ],
    },
    {
        "title": "6. Чистая кассовая зона",
        "items": [
            "6.1 Кассовый стол/шк: только актуальные листовки и POS-материалы",
            "6.1 Из товара только бренд SOLMATE, без ценников на лицевой стороне",
            "6.2 Аксессуарная зона у кассы соответствует сезону и спросу",
            "6.2 Рюкзаки/сумки набиты аккуратно бумагой/наполнителем",
        ],
    },
    {
        "title": "7. Освещение (зала)",
        "items": [
            "7.1 Все лампы исправны и корректно нацелены",
            "7.1 Фокусные точки: входная экспозиция, фронты, острова, POSM, манекены",
            "7.1 Проверка освещения — 1 раз в неделю; при необходимости нацеливания — заявка в Jira",
        ],
    },
]

# Ключи для хранения прогресса пользователя
STATE_PREFIX = "WEEKLY_CHECK"
# user_data[STATE_PREFIX] = {
#   "<block_index>": { "<item_index>": True/False },
#   ...
# }

# ──────────────────────────────────────────────────────────────────────────────
# Flask app
# ──────────────────────────────────────────────────────────────────────────────
app = Flask(__name__)

# PTB application (создадим позже в отдельном потоке)
tg_app: Application | None = None
tg_thread: threading.Thread | None = None


# ──────────────────────────────────────────────────────────────────────────────
# Вспомогалки: клавиатуры и сводки
# ──────────────────────────────────────────────────────────────────────────────
def main_menu_kb() -> InlineKeyboardMarkup:
    rows = []
    for idx, block in enumerate(CHECKLIST_BLOCKS):
        rows.append([InlineKeyboardButton(block["title"], callback_data=f"open:{idx}")])
    rows.append([InlineKeyboardButton("Сводка ✅/❌", callback_data="summary")])
    return InlineKeyboardMarkup(rows)


def block_kb(block_idx: int, user_state: Dict) -> InlineKeyboardMarkup:
    rows = []
    items = CHECKLIST_BLOCKS[block_idx]["items"]
    checked = user_state.get(str(block_idx), {})
    for i, text in enumerate(items):
        val = checked.get(str(i), None)  # None=не отмечено, True/False
        mark = "☐"
        if val is True:
            mark = "✅"
        elif val is False:
            mark = "❌"
        rows.append(
            [InlineKeyboardButton(f"{mark} {text}", callback_data=f"toggle:{block_idx}:{i}")]
        )
    rows.append(
        [
            InlineKeyboardButton("⬅️ Назад", callback_data="back"),
            InlineKeyboardButton("Сводка", callback_data="summary"),
        ]
    )
    return InlineKeyboardMarkup(rows)


def summarize(user_state: Dict) -> Tuple[str, int, int, int]:
    total = 0
    ok = 0
    bad = 0
    lines = []
    for b_idx, block in enumerate(CHECKLIST_BLOCKS):
        items = block["items"]
        checked = user_state.get(str(b_idx), {})
        block_ok = 0
        block_bad = 0
        for i in range(len(items)):
            total += 1
            val = checked.get(str(i))
            if val is True:
                ok += 1
                block_ok += 1
            elif val is False:
                bad += 1
                block_bad += 1
        lines.append(
            f"{block['title']} — ✅ {block_ok} / ❌ {block_bad} / ∅ {len(items) - block_ok - block_bad}"
        )
    header = f"Сводка недели:\n✅ {ok} | ❌ {bad} | ∅ {total - ok - bad} из {total}\n\n"
    return header + "\n".join(lines), total, ok, bad


# ──────────────────────────────────────────────────────────────────────────────
# Handlers
# ──────────────────────────────────────────────────────────────────────────────
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.effective_chat.send_message(
        "Привет! Это еженедельный чек-лист готовности торгового зала к продажам.\n\n"
        "Нажми «Проверка» и пройдись по блокам. В каждом пункте жми, чтобы переключать ☐ → ✅/❌.\n"
        "В конце открой «Сводка», чтобы увидеть результат.",
        reply_markup=InlineKeyboardMarkup(
            [[InlineKeyboardButton("Проверка", callback_data="menu")]]
        ),
    )


async def on_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    # просто подсказываем кнопку
    await update.effective_chat.send_message(
        "Нажми «Проверка», чтобы открыть чек-лист.",
        reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("Проверка", callback_data="menu")]]),
    )


async def on_cb(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    q = update.callback_query
    await q.answer()
    data = q.data or ""

    user_state = context.user_data.setdefault(STATE_PREFIX, {})

    if data == "menu" or data == "back":
        await q.edit_message_text("Выбери блок:", reply_markup=main_menu_kb())
        return

    if data == "summary":
        text, total, ok, bad = summarize(user_state)
        await q.edit_message_text(text, reply_markup=main_menu_kb(), disable_web_page_preview=True)
        return

    if data.startswith("open:"):
        _, b = data.split(":")
        b_idx = int(b)
        await q.edit_message_text(
            CHECKLIST_BLOCKS[b_idx]["title"],
            reply_markup=block_kb(b_idx, user_state),
        )
        return

    if data.startswith("toggle:"):
        _, b, i = data.split(":")
        b_idx, i_idx = int(b), int(i)
        block_map = user_state.setdefault(str(b_idx), {})
        current = block_map.get(str(i_idx))
        # None -> True -> False -> None
        new_val = True if current is None else (False if current is True else None)
        if new_val is None:
            block_map.pop(str(i_idx), None)
        else:
            block_map[str(i_idx)] = new_val

        await q.edit_message_text(
            CHECKLIST_BLOCKS[b_idx]["title"],
            reply_markup=block_kb(b_idx, user_state),
        )
        return


# ──────────────────────────────────────────────────────────────────────────────
# PTB init in background thread
# ──────────────────────────────────────────────────────────────────────────────
def _ptb_thread():
    global tg_app
    tg_app = Application.builder().token(BOT_TOKEN).build()

    tg_app.add_handler(CommandHandler("start", start))
    tg_app.add_handler(CallbackQueryHandler(on_cb))
    tg_app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, on_message))

    # Запуск polling-петли ВНУТРИ отдельного потока (для Render/WSGI окружения)
    tg_app.run_polling(allowed_updates=["message", "callback_query"])


def ensure_thread():
    global tg_thread
    if tg_thread and tg_thread.is_alive():
        return
    tg_thread = threading.Thread(target=_ptb_thread, name="ptb-thread", daemon=True)
    tg_thread.start()


# ──────────────────────────────────────────────────────────────────────────────
# Flask routes
# ──────────────────────────────────────────────────────────────────────────────
@app.get("/health")
def health() -> Response:
    return Response("ok", 200)


@app.get("/_loop")
def loop_state() -> Response:
    alive = bool(tg_thread and tg_thread.is_alive())
    running = bool(tg_app and tg_app.running)
    return Response(f"loop_alive={alive}, is_running={running}", 200)


@app.get("/set-webhook")
async def set_webhook():
    # для режима webhook (если решишь вернуться)
    if not BASE_URL:
        return Response("BASE_URL not set", 500)
    ensure_thread()  # не обязателен для вебхука, но не мешает
    return Response("Webhook set to " + BASE_URL + "/", 200)


@app.post("/")
def webhook() -> Response:
    # если Telegram стучится — отклоняем (мы на polling), но не 500
    return Response("polling", 503)


@app.get("/getwebhookinfo_raw")
def getinfo_raw():
    # отладочная заглушка (в polling режиме просто информируем)
    return Response('{"mode":"polling"}', 200, {"Content-Type": "application/json"})


# ──────────────────────────────────────────────────────────────────────────────
# WSGI entrypoint
# ──────────────────────────────────────────────────────────────────────────────
@app.before_request
def _ensure():
    # На каждом входящем HTTP запросе убеждаемся, что петля PTB крутится
    ensure_thread()


# ──────────────────────────────────────────────────────────────────────────────
# Local dev
# ──────────────────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    ensure_thread()
    app.run(host="0.0.0.0", port=int(os.getenv("PORT", "5000")))
