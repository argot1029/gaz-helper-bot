
import asyncio
import hashlib
import json
import logging
import os
import re
from pathlib import Path

from playwright.async_api import async_playwright, TimeoutError as PlaywrightTimeoutError
from telegram import (
    Update,
    ReplyKeyboardMarkup,
    InlineKeyboardMarkup,
    InlineKeyboardButton,
)
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    CallbackQueryHandler,
    ContextTypes,
    filters,
)

# Настройки
TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
CHAT_ID = 8580346032
URL = "https://trader.gaz.trade/payouts/available"

DATA_DIR = Path(os.getenv("DATA_DIR", "/data"))
PROFILE_DIR = DATA_DIR / "browser_profile"
SEEN_FILE = DATA_DIR / "seen_requests.json"

DEFAULT_INTERVAL = 15
MIN_INTERVAL = 15
MAX_INTERVAL = 300
MAX_REQUESTS_IN_MESSAGE = 5

logging.basicConfig(
    format="%(asctime)s | %(levelname)s | %(message)s",
    level=logging.INFO,
)
log = logging.getLogger("monitor")

MENU = ReplyKeyboardMarkup(
    [
        ["📋 Все заявки", "🆕 Новые"],
        ["📊 Статус", "⏱ Интервал"],
        ["🛑 Стоп", "▶️ Запуск"],
    ],
    resize_keyboard=True,
)

state = {
    "running": True,
    "interval": DEFAULT_INTERVAL,
    "first_scan": True,
    "last_check": "Ещё не проверялось",
    "last_error": "",
    "requests": [],
    "known": set(),
    "page_title": "",
    "page_url": URL,
    "waiting_interval": False,
    "browser_ready": False,
}

browser_task = None
stop_event = asyncio.Event()


def load_seen():
    try:
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        if SEEN_FILE.exists():
            data = json.loads(SEEN_FILE.read_text(encoding="utf-8"))
            return set(data if isinstance(data, list) else [])
    except Exception:
        log.exception("Не удалось загрузить список заявок")
    return set()


def save_seen():
    try:
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        SEEN_FILE.write_text(
            json.dumps(sorted(state["known"]), ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
    except Exception:
        log.exception("Не удалось сохранить список заявок")


def fingerprint(text):
    normalized = re.sub(r"\s+", " ", text).strip().lower()
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()


async def send_message(app, text, reply_markup=None):
    await app.bot.send_message(
        chat_id=CHAT_ID,
        text=text[:4000],
        reply_markup=reply_markup,
        disable_web_page_preview=True,
    )


async def extract_requests(page):
    """Ищет элементы с текстом «Взять в работу» и извлекает текст карточек."""
    results = []
    buttons = page.get_by_text("Взять в работу", exact=True)
    count = await buttons.count()

    for index in range(min(count, 200)):
        button = buttons.nth(index)

        try:
            if not await button.is_visible():
                continue

            card_text = ""
            for level in range(1, 7):
                candidate = button.locator("xpath=" + "/..".join([""] * 0))
                # Поднимаемся от кнопки к родительским элементам.
                candidate = button
                for _ in range(level):
                    candidate = candidate.locator("xpath=..")

                try:
                    text = (await candidate.inner_text(timeout=1500)).strip()
                    if len(text) >= 20:
                        card_text = text
                        break
                except Exception:
                    continue

            if not card_text:
                card_text = (await button.locator("xpath=..").inner_text()).strip()

            card_text = re.sub(r"\n{3,}", "\n\n", card_text).strip()
            if card_text and card_text not in results:
                results.append(card_text)

        except Exception:
            continue

    return results


async def inspect_page(page):
    await page.goto(URL, wait_until="domcontentloaded", timeout=60000)
    try:
        await page.wait_for_load_state("networkidle", timeout=10000)
    except PlaywrightTimeoutError:
        pass

    await page.wait_for_timeout(1500)

    title = await page.title()
    body_text = await page.locator("body").inner_text(timeout=10000)

    state["page_title"] = title
    state["page_url"] = page.url

    if re.search(
        r"войти|авторизация|сессия истекла|sign in|log in",
        body_text[:5000],
        re.IGNORECASE,
    ):
        state["last_error"] = (
            "Возможно, сайт требует авторизации. Проверь вход в аккаунт."
        )

    requests = await extract_requests(page)
    return requests, body_text


async def monitor_loop(app):
    state["known"] = load_seen()

    DATA_DIR.mkdir(parents=True, exist_ok=True)
    PROFILE_DIR.mkdir(parents=True, exist_ok=True)

    async with async_playwright() as p:
        context = await p.chromium.launch_persistent_context(
            user_data_dir=str(PROFILE_DIR),
            headless=True,
            viewport={"width": 1440, "height": 1000},
            locale="ru-RU",
            args=["--no-sandbox"],
        )

        page = context.pages[0] if context.pages else await context.new_page()
        state["browser_ready"] = True

        try:
            while not stop_event.is_set():
                if not state["running"]:
                    await asyncio.sleep(1)
                    continue

                try:
                    requests, body_text = await inspect_page(page)
                    state["requests"] = requests
                    state["last_check"] = __import__("datetime").datetime.now().strftime(
                        "%Y-%m-%d %H:%M:%S"
                    )

                    if state["last_error"].startswith("Ошибка проверки:"):
                        state["last_error"] = ""

                    current = {}
                    for item in requests:
                        key = fingerprint(item)
                        current[key] = item

                    if state["first_scan"]:
                        state["known"].update(current.keys())
                        state["first_scan"] = False
                        save_seen()
                        log.info("Первое сканирование: найдено заявок %s", len(requests))

                    else:
                        new_items = [
                            (key, item)
                            for key, item in current.items()
                            if key not in state["known"]
                        ]

                        for key, item in new_items:
                            await send_message(
                                app,
                                "🆕 Новая заявка\n\n"
                                + item[:3000]
                                + "\n\nСсылка на сайт: "
                                + URL,
                                reply_markup=InlineKeyboardMarkup(
                                    [[InlineKeyboardButton(
                                        "🌐 Открыть сайт",
                                        url=URL,
                                    )]]
                                ),
                            )
                            state["known"].add(key)

                        if new_items:
                            save_seen()
                            log.info("Новых заявок: %s", len(new_items))

                    log.info(
                        "Проверка завершена: найдено %s заявок",
                        len(requests),
                    )

                except Exception as exc:
                    state["last_error"] = f"Ошибка проверки: {exc}"
                    log.exception("Ошибка мониторинга")

                await asyncio.sleep(state["interval"])

        finally:
            state["browser_ready"] = False
            await context.close()


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_chat.id != CHAT_ID:
        return

    await update.message.reply_text(
        "Монитор заявок запущен.\n"
        "Используй кнопки меню для управления.",
        reply_markup=MENU,
    )


async def test(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_chat.id != CHAT_ID:
        return
    await update.message.reply_text("✅ Telegram работает. Бот получил команду.")


async def check(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_chat.id != CHAT_ID:
        return

    try:
        await update.message.reply_text(
            "Страница: " + state["page_url"] + "\n"
            + "Заголовок: " + (state["page_title"] or "неизвестен") + "\n"
            + "Найдено карточек: " + str(len(state["requests"])) + "\n"
            + "Последняя проверка: " + state["last_check"] + "\n"
            + "Ошибка: " + (state["last_error"] or "нет")
        )
    except Exception as exc:
        await update.message.reply_text(f"Ошибка: {exc}")


async def requests_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_chat.id != CHAT_ID:
        return

    items = state["requests"]
    if not items:
        await update.message.reply_text(
            "Заявок пока не найдено. Проверь авторизацию командой /check."
        )
        return

    for i in range(0, len(items), MAX_REQUESTS_IN_MESSAGE):
        batch = items[i:i + MAX_REQUESTS_IN_MESSAGE]
        text = "📋 Найденные заявки\n\n" + "\n\n──────────\n\n".join(batch)
        await update.message.reply_text(text[:4000])


async def new_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_chat.id != CHAT_ID:
        return

    items = state["requests"]
    if not items:
        await update.message.reply_text("Новых карточек сейчас не найдено.")
        return

    await requests_command(update, context)


async def status(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_chat.id != CHAT_ID:
        return

    await update.message.reply_text(
        "📊 Статус мониторинга\n"
        f"Работает: {'да' if state['running'] else 'нет'}\n"
        f"Интервал: {state['interval']} сек.\n"
        f"Найдено заявок: {len(state['requests'])}\n"
        f"Известных заявок: {len(state['known'])}\n"
        f"Последняя проверка: {state['last_check']}\n"
        f"Профиль браузера: {PROFILE_DIR}\n"
        f"Ошибка: {state['last_error'] or 'нет'}"
    )


async def interval_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_chat.id != CHAT_ID:
        return

    if context.args:
        try:
            value = int(context.args[0])
            if not MIN_INTERVAL <= value <= MAX_INTERVAL:
                raise ValueError
            state["interval"] = value
            await update.message.reply_text(f"Интервал установлен: {value} сек.")
        except ValueError:
            await update.message.reply_text(
                f"Укажи число от {MIN_INTERVAL} до {MAX_INTERVAL}. Например: /interval 30"
            )
    else:
        state["waiting_interval"] = True
        await update.message.reply_text(
            f"Введи интервал в секундах от {MIN_INTERVAL} до {MAX_INTERVAL}."
        )


async def stop_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_chat.id != CHAT_ID:
        return
    state["running"] = False
    await update.message.reply_text("⏸ Проверка остановлена. Нажми «▶️ Запуск», чтобы продолжить.")


async def run_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_chat.id != CHAT_ID:
        return
    state["running"] = True
    await update.message.reply_text("▶️ Проверка снова включена.")


async def text_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_chat.id != CHAT_ID:
        return

    text = (update.message.text or "").strip()

    if state["waiting_interval"]:
        state["waiting_interval"] = False
        try:
            value = int(text)
            if not MIN_INTERVAL <= value <= MAX_INTERVAL:
                raise ValueError
            state["interval"] = value
            await update.message.reply_text(f"Интервал установлен: {value} сек.")
        except ValueError:
            await update.message.reply_text(
                f"Нужно целое число от {MIN_INTERVAL} до {MAX_INTERVAL}."
            )
        return

    if text == "📋 Все заявки":
        await requests_command(update, context)
    elif text == "🆕 Новые":
        await new_command(update, context)
    elif text == "📊 Статус":
        await status(update, context)
    elif text == "⏱ Интервал":
        await interval_command(update, context)
    elif text == "🛑 Стоп":
        await stop_command(update, context)
    elif text == "▶️ Запуск":
        await run_command(update, context)


async def post_init(app):
    global browser_task
    state["running"] = True
    browser_task = asyncio.create_task(monitor_loop(app))
    try:
        await send_message(
            app,
            "🤖 Монитор запущен на сервере.\n"
            "Проверяю страницу и отслеживаю новые карточки.",
            reply_markup=MENU,
        )
    except Exception:
        log.exception("Не удалось отправить стартовое сообщение")


async def post_shutdown(app):
    stop_event.set()
    if browser_task:
        browser_task.cancel()
        try:
            await browser_task
        except asyncio.CancelledError:
            pass


def main():
    if not TOKEN:
        raise RuntimeError(
            "Не задан TELEGRAM_BOT_TOKEN. Добавь токен в переменные окружения Amvera."
        )

    application = (
        Application.builder()
        .token(TOKEN)
        .post_init(post_init)
        .post_shutdown(post_shutdown)
        .build()
    )

    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("test", test))
    application.add_handler(CommandHandler("check", check))
    application.add_handler(CommandHandler("requests", requests_command))
    application.add_handler(CommandHandler("new", new_command))
    application.add_handler(CommandHandler("status", status))
    application.add_handler(CommandHandler("interval", interval_command))
    application.add_handler(CommandHandler("stop", stop_command))
    application.add_handler(CommandHandler("run", run_command))
    application.add_handler(
        MessageHandler(filters.TEXT & ~filters.COMMAND, text_handler)
    )

    log.info("Запуск Telegram-бота")
    application.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    main()
