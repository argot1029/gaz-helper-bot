"""Telegram-бот: мониторинг заявок на trader.gaz.trade + вход через бота.

Сессия на сайте общая для всех пользователей: уведомления приходят всем одновременно,
управлять ботом может любой, кому открыт доступ. Основные аккаунты заданы в config.CHAT_IDS,
остальных добавляют прямо в боте: «📊 Статус» → «➕ Добавить пользователя».
"""
import asyncio
import logging
import re
import time
import warnings
from collections import OrderedDict
from datetime import datetime
from html import escape
from zoneinfo import ZoneInfo

from telegram import (
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    LinkPreviewOptions,
    ReplyKeyboardMarkup,
    ReplyParameters,
    Update,
)
from telegram.constants import ChatType, ParseMode
from telegram.error import RetryAfter
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    ConversationHandler,
    MessageHandler,
    TypeHandler,
    filters,
)
from telegram.warnings import PTBUserWarning

import autotake
import config
import users
from browser import LoginError, LoginFlow, SiteSession, TakeResult
from cards import Card, format_card_html, keys_for, parse_card
from fastlane import FAST_ON
from registry import ACTIVE, EXPIRED, Registry
from storage import read_json, write_json

logging.basicConfig(
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
    level=logging.INFO,
)
# httpx пишет в лог полный URL запроса, а в нём лежит токен бота.
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("httpcore").setLevel(logging.WARNING)
# Кнопка «🔐 Вход» под сообщением о слёте сессии запускает диалог входа: так и задумано.
warnings.filterwarnings("ignore", message=r".*per_message=False.*", category=PTBUserWarning)
log = logging.getLogger("monitor")

BTN_LIST = "📋 Список заявок"
BTN_STATUS = "📊 Статус"
BTN_AUTO = "⚡ Автовзятие"
BTN_LOGIN = "🔐 Вход"
BTN_SCREEN = "📸 Скриншот"
BTN_STOP = "🛑 Стоп"
BTN_RUN = "▶️ Запуск"
BTN_CANCEL = "❌ Отмена"
# Кнопки из прошлых версий: у старых клавиатур они ещё могут быть.
LEGACY_LIST_BUTTONS = ["📋 Все заявки", "🆕 Новые"]
LEGACY_INTERVAL_BUTTON = "⏱ Интервал"

MENU = ReplyKeyboardMarkup(
    [
        [BTN_LIST, BTN_AUTO],
        [BTN_STATUS, BTN_LOGIN],
        [BTN_SCREEN, BTN_STOP, BTN_RUN],
    ],
    resize_keyboard=True,
)
LOGIN_MENU = ReplyKeyboardMarkup([[BTN_CANCEL]], resize_keyboard=True)


class AllowedFilter(filters.UpdateFilter):
    """Пропускает только личные сообщения от тех, кому открыт доступ.

    Список проверяется при каждом сообщении, поэтому добавленный пользователь
    начинает работать сразу, без перезапуска бота.
    """

    def filter(self, update: Update) -> bool:
        user, chat = update.effective_user, update.effective_chat
        return bool(
            user
            and chat
            and chat.type == ChatType.PRIVATE
            and users.is_allowed(user.id)
        )


ALLOWED = AllowedFilter()

MENU_TEXTS = [
    BTN_LIST,
    BTN_STATUS,
    BTN_AUTO,
    BTN_SCREEN,
    BTN_STOP,
    BTN_RUN,
    LEGACY_INTERVAL_BUTTON,
    *LEGACY_LIST_BUTTONS,
]
MENU_FILTER = filters.Regex("^(" + "|".join(re.escape(t) for t in MENU_TEXTS) + ")$")
CANCEL_FILTER = filters.Regex(f"^{re.escape(BTN_CANCEL)}$")
LOGIN_BUTTON_FILTER = filters.Regex(f"^{re.escape(BTN_LOGIN)}$")
# Текст, который диалог входа принимает как логин / пароль / код.
LOGIN_INPUT = (
    ALLOWED
    & filters.TEXT
    & ~filters.COMMAND
    & ~MENU_FILTER
    & ~CANCEL_FILTER
    & ~LOGIN_BUTTON_FILTER
)

LOGIN_STATE, PASSWORD_STATE, CODE_STATE = range(3)

TZ = ZoneInfo(config.TIMEZONE)
NO_PREVIEW = LinkPreviewOptions(is_disabled=True)
STATUS_EXPIRED = "⌛ <i>Устарела</i>"
# Сколько актуальных заявок показывать в списке (если в config.py нет LIST_MAX — 30).
LIST_MAX = getattr(config, "LIST_MAX", 30)
# Быстрая линия (fastlane.py): как часто перепроверять страницу целиком, пока API работает.
FAST_AUDIT = float(getattr(config, "FAST_AUDIT_INTERVAL", 15.0))

AUTH_ALERT = (
    "⚠️ Сессия на сайте слетела или ещё не создана.\n"
    "Нажми «🔐 Вход» и пройди авторизацию (логин, пароль, 2FA). "
    "Сессия общая — войти достаточно с одного аккаунта."
)

session = SiteSession()
registry = Registry(config.REGISTRY_FILE)
login_flow: LoginFlow | None = None
login_claim: dict = {"chat": None, "since": 0.0}  # кто сейчас проходит вход
monitor_task: asyncio.Task | None = None
fast_task: asyncio.Task | None = None
wake = asyncio.Event()
stop_event = asyncio.Event()
taking: set[str] = set()  # заявки, которые сейчас берутся в работу

# Все сообщения с кнопкой «Взять в работу» (уведомления и списки) у всех аккаунтов:
# ключ заявки -> [{chat, mid, html, expired, list}]. Нужны, чтобы после взятия заявки
# или её устаревания обновить каждое такое сообщение, где бы оно ни лежало.
shown: OrderedDict[str, list[dict]] = OrderedDict()
SHOWN_LIMIT = 300      # заявок
SHOWN_PER_KEY = 40     # сообщений на одну заявку
listing: set[int] = set()  # аккаунты, которым сейчас отправляется список
LIST_PAUSE = 0.12      # пауза между сообщениями списка (лимиты Telegram)

state = {
    "running": True,
    "interval": float(getattr(config, "SCAN_INTERVAL", 5.0)),  # период проверки страницы, сек
    "last_check": "Ещё не проверялось",
    "last_error": "",
    "page_title": "",
    "page_url": config.URL,
    "adding_user": set(),  # чаты основных аккаунтов, от которых ждём @username или ID
    "authenticated": False,
    "auth_alert_sent": False,  # о слёте сессии сообщаем один раз
    "auth_alert_msgs": {},  # {chat_id: message_id} — сообщения с кнопкой «Вход»
    "errors_in_row": 0,
    "last_log": 0.0,
    "last_count": -1,
    "fast_on": False,  # быстрая линия была включена при прошлой проверке
    "fast_announced": False,
    "fast_off_at": 0.0,
}


def now_str() -> str:
    return datetime.now(TZ).strftime("%Y-%m-%d %H:%M:%S")


def person(update: Update) -> str:
    user = update.effective_user
    return (user.full_name if user else "") or "кто-то"


def is_notification(message) -> bool:
    """Уведомление о новой заявке (в отличие от заявки из списка) начинается с «🆕»."""
    return (getattr(message, "text_html", None) or "").startswith("🆕")


# --- Настройки ----------------------------------------------------------------


def load_settings() -> None:
    data = read_json(config.SETTINGS_FILE, {}) or {}
    if isinstance(data.get("running"), bool):
        state["running"] = data["running"]


def save_settings() -> None:
    write_json(config.SETTINGS_FILE, {"running": state["running"]})


# --- Отправка сообщений ---------------------------------------------------------


def _seconds(value) -> float:
    return value.total_seconds() if hasattr(value, "total_seconds") else float(value)


async def tg(func, *args, **kwargs):
    """Вызов Telegram API с повтором, если Telegram просит подождать (flood control)."""
    for attempt in range(4):
        try:
            return await func(*args, **kwargs)
        except RetryAfter as exc:
            if attempt == 3:
                raise
            wait = _seconds(exc.retry_after) + 0.5
            log.warning("Telegram просит подождать %.0f сек.", wait)
            await asyncio.sleep(wait)


async def send_all(
    app,
    text: str,
    reply_markup=None,
    parse_mode: str | None = None,
    exclude: set[int] | None = None,
    to: list[int] | None = None,
) -> dict[int, int]:
    """Отправляет сообщение всем пользователям одновременно. Возвращает {chat_id: message_id}."""
    recipients = users.chat_ids() if to is None else to
    targets = [cid for cid in recipients if cid not in (exclude or set())]

    async def one(chat_id: int):
        try:
            message = await tg(
                app.bot.send_message,
                chat_id=chat_id,
                text=text[:4000],
                parse_mode=parse_mode,
                reply_markup=reply_markup,
                link_preview_options=NO_PREVIEW,
            )
            return chat_id, message.message_id
        except Exception as exc:
            # Чаще всего: аккаунт ещё не нажал /start у бота.
            log.warning("Не удалось отправить сообщение в чат %s: %s", chat_id, exc)
            return chat_id, None

    results = await asyncio.gather(*(one(cid) for cid in targets))
    return {cid: mid for cid, mid in results if mid is not None}


async def delete_quietly(message) -> None:
    try:
        await message.delete()
    except Exception:
        pass


async def say(app, chat_id: int, reply_to: int | None, html: str, photo: bytes | None = None) -> None:
    """Сообщение в чат — ответом на заявку, под которой нажали кнопку."""
    reply = (
        ReplyParameters(message_id=reply_to, allow_sending_without_reply=True)
        if reply_to
        else None
    )
    try:
        if photo and len(html) <= 1000:
            await tg(
                app.bot.send_photo,
                chat_id=chat_id,
                photo=photo,
                caption=html,
                parse_mode=ParseMode.HTML,
                reply_parameters=reply,
            )
            return
        if photo:
            await tg(app.bot.send_photo, chat_id=chat_id, photo=photo, reply_parameters=reply)
        await tg(
            app.bot.send_message,
            chat_id=chat_id,
            text=html[:4000],
            parse_mode=ParseMode.HTML,
            reply_parameters=reply,
            link_preview_options=NO_PREVIEW,
        )
    except Exception:
        log.exception("Не удалось отправить сообщение в чат %s", chat_id)


def register(
    key: str, chat_id: int, message_id: int, html: str, expired_html: str, is_list: bool = False
) -> None:
    """Запоминает сообщение с кнопкой «Взять в работу»."""
    refs = shown.setdefault(key, [])
    refs.append(
        {"chat": chat_id, "mid": message_id, "html": html, "expired": expired_html, "list": is_list}
    )
    del refs[:-SHOWN_PER_KEY]
    shown.move_to_end(key)
    while len(shown) > SHOWN_LIMIT:
        shown.popitem(last=False)


def targets_for(key: str, pressed=None) -> list[dict]:
    """Все сообщения этой заявки. Нажатое сообщение добавляем всегда: после перезапуска
    бота в памяти ничего нет, но кнопки в старых сообщениях ещё работают."""
    refs = list(shown.get(key, []))
    if pressed is not None and not any(
        r["chat"] == pressed.chat.id and r["mid"] == pressed.message_id for r in refs
    ):
        text = getattr(pressed, "text_html", None) or ""
        refs.append(
            {
                "chat": pressed.chat.id,
                "mid": pressed.message_id,
                "html": text,
                "expired": f"{text}\n\n{STATUS_EXPIRED}" if text else "",
                "list": not is_notification(pressed),
            }
        )
    return refs


async def _strip_one(app, chat_id: int, message_id: int) -> None:
    try:
        await tg(
            app.bot.edit_message_reply_markup,
            chat_id=chat_id,
            message_id=message_id,
            reply_markup=None,
        )
    except Exception as exc:
        if "not modified" not in str(exc).lower():
            log.warning("Не удалось убрать кнопку %s/%s: %s", chat_id, message_id, exc)


async def _edit_one(app, ref: dict, text: str, reply_markup=None) -> None:
    """Меняет текст сообщения. Кнопку убирает, если не передана новая."""
    try:
        await tg(
            app.bot.edit_message_text,
            chat_id=ref["chat"],
            message_id=ref["mid"],
            text=text[:4000],
            parse_mode=ParseMode.HTML,
            reply_markup=reply_markup,
            link_preview_options=NO_PREVIEW,
        )
    except Exception as exc:
        if "not modified" not in str(exc).lower():
            log.warning("Не удалось обновить сообщение %s/%s: %s", ref["chat"], ref["mid"], exc)


async def finish_messages(app, key: str, pressed=None, status_html: str = "") -> None:
    """Заявка закрыта (взята в работу или уже обработана): у всех копий сообщения —
    и в уведомлениях, и в списке — убираем кнопку и дописываем статус."""
    refs = targets_for(key, pressed)
    shown.pop(key, None)

    async def one(ref: dict) -> None:
        if status_html and ref["html"]:
            await _edit_one(app, ref, f"{ref['html']}\n\n{status_html}")
        else:
            await _strip_one(app, ref["chat"], ref["mid"])

    await asyncio.gather(*(one(ref) for ref in refs))


async def expire_messages(app, key: str, pressed=None) -> None:
    """Заявка устарела. В списке её быть не должно — сообщение удаляем. Уведомление
    остаётся как история: убираем кнопку и пишем, что оно устарело."""
    refs = targets_for(key, pressed)
    shown.pop(key, None)

    async def one(ref: dict) -> None:
        if ref["list"]:
            try:
                await tg(app.bot.delete_message, chat_id=ref["chat"], message_id=ref["mid"])
                return
            except Exception as exc:  # например, сообщению больше 48 часов
                log.warning("Не удалось удалить сообщение %s/%s: %s", ref["chat"], ref["mid"], exc)
        if ref["expired"]:
            await _edit_one(app, ref, ref["expired"])
        else:
            await _strip_one(app, ref["chat"], ref["mid"])

    await asyncio.gather(*(one(ref) for ref in refs))


# --- Уведомления о заявках ------------------------------------------------------


def take_markup(key: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([[InlineKeyboardButton("✅ Взять в работу", callback_data=f"take:{key}")]])


def notification_html(card: Card) -> str:
    return (
        "🆕 <b>Новая заявка!</b>\n\n"
        + format_card_html(card)
        + f"\n\nСсылка на сайт: {escape(config.URL)}"
    )


async def send_new(app, entry: dict) -> None:
    html = notification_html(Card.from_dict(entry["card"]))
    sent = await send_all(app, html, take_markup(entry["key"]), ParseMode.HTML)
    if sent:
        registry.set_notified(entry["key"])
        for chat_id, message_id in sent.items():
            register(entry["key"], chat_id, message_id, html, f"{html}\n\n{STATUS_EXPIRED}")
    else:
        log.error("Заявку %s не удалось отправить ни в один чат, повторю", entry["key"])


# --- Автовзятие -------------------------------------------------------------------
# Новая заявка, подходящая под фильтры (см. autotake.py), берётся сразу: бот начинает
# нажимать кнопку на сайте одновременно с отправкой уведомления и не ждёт Telegram.
# Не подошла под фильтры или автовзятие выключено — обычное уведомление с кнопкой.

ATTEMPT_LINE = "⚡ <b>Пытаюсь взять в работу…</b>"
AUTO_OK_LINE = "<b>Откуп в корзине, шеф</b> 🤗"
AUTO_FAIL_LINE = "<b>Извините, шеф, неудача</b> 😔"
AUTO_MAX_TRIES = 2  # сколько раз бот сам пробует взять одну заявку, потом только вручную
auto_attempts: dict[str, int] = {}


async def run_take(key: str) -> TakeResult:
    try:
        return await session.take(key)
    except asyncio.CancelledError:
        raise
    except Exception as exc:
        log.exception("Ошибка при взятии заявки в работу")
        return TakeResult("failed", f"Внутренняя ошибка бота: {exc}")


def auto_failure(result: TakeResult) -> tuple[str, bool]:
    """(причина для человека, можно ли взять вручную)."""
    if result.status == "gone":
        return "заявку уже забрали или она устарела", False
    if result.status in ("need_login", "no_session"):
        return "сессия на сайте слетела — нужен вход", True
    return (result.message or "причина неизвестна").strip()[:400], True


async def auto_take_entry(app, entry: dict, why: str) -> None:
    key = entry["key"]
    card = Card.from_dict(entry["card"])
    base = notification_html(card)
    auto_attempts[key] = auto_attempts.get(key, 0) + 1
    log.info("Автовзятие: пробую взять заявку %s (%s)", key, why)

    taking.add(key)
    job = asyncio.ensure_future(run_take(key))  # браузер работает, пока уходят сообщения
    try:
        sent: dict[int, int] = {}
        try:
            sent = await send_all(app, f"{base}\n\n{ATTEMPT_LINE}", parse_mode=ParseMode.HTML)
        except Exception:
            log.exception("Не удалось отправить уведомление об автовзятии")
        if sent:
            registry.set_notified(key)
            for chat_id, message_id in sent.items():
                register(key, chat_id, message_id, base, f"{base}\n\n{STATUS_EXPIRED}")
        result = await job
    finally:
        taking.discard(key)

    refs = list(shown.get(key, []))

    if result.status == "ok":
        mark_closed(key)
        registry.remove(key)
        shown.pop(key, None)
        auto_attempts.pop(key, None)
        log.info("Автовзятие: заявка %s взята в работу", key)
        if refs:
            await asyncio.gather(
                *(_edit_one(app, r, f"{base}\n\n{AUTO_OK_LINE}") for r in refs),
                *(say(app, r["chat"], r["mid"], AUTO_OK_LINE) for r in refs),
            )
        else:  # уведомление никому не дошло — хотя бы результат отправим
            await send_all(app, f"{AUTO_OK_LINE}\n\n{format_card_html(card)}", parse_mode=ParseMode.HTML)
        return

    reason, manual = auto_failure(result)
    log.warning("Автовзятие: заявка %s не взята: %s", key, reason)
    if result.status == "gone":
        mark_closed(key)
        registry.mark(key, EXPIRED)
        shown.pop(key, None)
    elif result.status in ("need_login", "no_session"):
        state["authenticated"] = False

    detail = f"{AUTO_FAIL_LINE}\nПричина: {escape(reason)}"
    on_message = detail + ("\nМожно взять вручную — кнопка ниже." if manual else "")
    markup = take_markup(key) if manual else None
    photo = None if result.site_error else result.screenshot  # сайт молчал — показываем страницу
    await asyncio.gather(
        *(_edit_one(app, r, f"{base}\n\n{on_message}", markup) for r in refs),
        *(say(app, r["chat"], r["mid"], detail, photo) for r in refs),
    )


# --- Новые заявки: общий разбор для обычной проверки и быстрой линии ----------------------------

in_flight: set[str] = set()  # заявки, которые прямо сейчас разбираются (чтобы не взять дважды)
closed_at: dict[str, float] = {}  # заявки, которые бот только что закрыл: ключ -> когда
CLOSED_TTL = 15.0  # столько секунд устаревший снимок страницы может показывать уже закрытую заявку


def mark_closed(key: str) -> None:
    now = time.monotonic()
    closed_at[key] = now
    for old in [k for k, t in closed_at.items() if now - t > CLOSED_TTL]:
        closed_at.pop(old, None)


def is_closed(key: str) -> bool:
    t = closed_at.get(key)
    return t is not None and time.monotonic() - t < CLOSED_TTL


async def _guarded(coro, key: str):
    try:
        return await coro
    finally:
        in_flight.discard(key)


async def handle_pending(app) -> tuple[int, int]:
    """Новые заявки без уведомления: подходящие берутся сами, остальные приходят с кнопкой.
    Возвращает (сколько берётся автоматически, сколько отправлено вручную)."""
    auto_jobs, manual = [], []
    for entry in registry.pending():  # новые и те, что не удалось отправить раньше
        key = entry["key"]
        if key in in_flight:
            continue
        in_flight.add(key)
        ok, why = False, ""
        if auto_attempts.get(key, 0) < AUTO_MAX_TRIES:
            ok, why = autotake.should_auto(Card.from_dict(entry["card"]))
        if ok:
            auto_jobs.append(asyncio.ensure_future(_guarded(auto_take_entry(app, entry, why), key)))
        else:
            manual.append(entry)
    for entry in manual:  # обычное уведомление с кнопкой — параллельно с автовзятием
        try:
            await send_new(app, entry)
        finally:
            in_flight.discard(entry["key"])
    if auto_jobs:
        for outcome in await asyncio.gather(*auto_jobs, return_exceptions=True):
            if isinstance(outcome, BaseException):
                log.error("Сбой автовзятия", exc_info=outcome)
    return len(auto_jobs), len(manual)


async def process_fast(app, pairs: list[tuple[str, Card]]) -> None:
    """Список заявок из API: он ведёт реестр, пока быстрая линия работает."""
    pairs = [(k, c) for k, c in pairs if not is_closed(k)]
    if session.fast.complete:
        await apply_pairs(app, pairs, "api")
        return
    # Страниц слишком много, список неполный: новые берём, но «устаревшими» ничего не помечаем.
    if registry.fresh:
        return
    if registry.add_new(pairs, src="api"):
        await handle_pending(app)


async def process_push(app, pairs: list[tuple[str, Card]]) -> None:
    """Push по WebSocket: новая заявка разбирается сразу, не дожидаясь опроса."""
    if registry.fresh:  # первая проверка ещё не прошла: всё, что висит, считается старым
        return
    pairs = [(k, c) for k, c in pairs if not is_closed(k)]
    added = registry.add_new(pairs, src="api")
    if not added:
        return
    log.info("Быстрая линия: push, новых заявок %s", len(added))
    await handle_pending(app)


async def fast_loop(app) -> None:
    """Подключает быструю линию к браузеру и опрашивает список заявок через API сайта."""
    while not stop_event.is_set():
        fast = session.fast
        if not (FAST_ON and state["running"] and session.has_session):
            await asyncio.sleep(0.5)
            continue
        if not fast.attached or fast.context is not session.context:
            try:
                await fast.attach(session.context)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                fast.error = f"не удалось подключиться: {exc}"
                log.exception("Быстрая линия: не удалось подключиться")
                await asyncio.sleep(10.0)
            continue
        wait = max(fast.backoff_until, fast.cooldown_until) - time.monotonic()
        if wait > 0:
            await asyncio.sleep(min(wait, 1.0))
            continue

        started = time.monotonic()
        try:
            pairs = await fast.poll()
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            log.warning("Быстрая линия: опрос не удался: %s", exc)
            await asyncio.sleep(2.0)
            continue
        if pairs is None:
            if not fast.ready:
                wake.set()  # API не отвечает: пусть обычная проверка разберётся (возможно, слетел вход)
                await fast_notice(app)
            await asyncio.sleep(2.0)
            continue
        try:
            await process_fast(app, pairs)
            await fast_notice(app)
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("Ошибка быстрой линии")
        await asyncio.sleep(max(0.05, fast.poll_interval() - (time.monotonic() - started)))


async def fast_notice(app) -> None:
    """Сообщает основным аккаунтам, когда быстрая линия включилась или отключилась."""
    if not FAST_ON:
        return
    fast = session.fast
    owners = users.owners()
    if fast.ready and not state["fast_on"]:
        state["fast_on"] = True
        if not state["fast_announced"]:
            state["fast_announced"] = True
            await send_all(
                app,
                "⚡ <b>Быстрая линия включена</b>\n"
                "Бот работает напрямую с API сайта: новые заявки приходят по WebSocket сразу, "
                "а «Взять в работу» — один запрос, без страницы и клика.",
                parse_mode=ParseMode.HTML,
                to=owners,
            )
    elif not fast.ready and state["fast_on"]:
        state["fast_on"] = False
        if time.monotonic() - state["fast_off_at"] > 600:
            state["fast_off_at"] = time.monotonic()
            reason = fast.disabled_note or fast.error or fast.reason
            await send_all(
                app,
                f"⚠️ Быстрая линия отключена: {escape(reason)}.\nРаботаю обычным способом (страница и кнопка).",
                parse_mode=ParseMode.HTML,
                to=owners,
            )


# --- Слёт сессии ------------------------------------------------------------------


def login_markup() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([[InlineKeyboardButton("🔐 Вход", callback_data="login")]])


async def clear_auth_alert(app) -> None:
    """Сессия снова есть — убираем кнопку «Вход» под сообщениями о слёте."""
    messages, state["auth_alert_msgs"] = state["auth_alert_msgs"], {}
    await asyncio.gather(*(_strip_one(app, chat, mid) for chat, mid in messages.items()))


# --- Мониторинг -----------------------------------------------------------------


async def pause(seconds: float) -> None:
    try:
        await asyncio.wait_for(wake.wait(), timeout=seconds)
    except asyncio.TimeoutError:
        pass
    wake.clear()


async def apply_pairs(app, pairs: list[tuple[str, Card]], src: str) -> None:
    """Полный снимок заявок (страница или API): новые -> уведомление или автовзятие,
    пропавшие -> «устарела»."""
    first = registry.fresh
    quiet = first
    if src == "api" and not registry.api_baselined:
        quiet = True  # первый полный снимок API: всё, что уже висит, не «новое»
        registry.api_baselined = True
        log.info("Быстрая линия: первый снимок списка (%s заявок) запомнен без уведомлений", len(pairs))
    diff = registry.update(pairs, first=quiet, src=src)
    if quiet and not first:
        registry.save()

    if first:
        log.info("Первое сканирование: найдено заявок %s", len(pairs))
        await send_all(
            app,
            f"✅ Первая проверка прошла. Заявок на странице сейчас: {len(pairs)}.\n"
            "О них уведомлений не будет — буду присылать только новые.",
        )
    else:
        auto_count, _ = await handle_pending(app)
        if diff.new:
            log.info("Новых заявок: %s (автовзятие: %s, источник: %s)", len(diff.new), auto_count, src)

    for entry in diff.expired:
        if entry["key"] not in taking:
            await expire_messages(app, entry["key"])


async def process_ok(app, result) -> None:
    state["authenticated"] = True
    state["last_error"] = ""

    if state["auth_alert_sent"]:
        state["auth_alert_sent"] = False
        await clear_auth_alert(app)
        await send_all(app, "✅ Сессия на сайте активна, мониторинг продолжается.")

    cards = [parse_card(text) for text in result.requests]
    pairs = list(zip(keys_for(cards), cards))
    # Заявки, которые бот только что закрыл (взял или их забрали), на снимке страницы ещё могли остаться.
    pairs = [(k, c) for k, c in pairs if not is_closed(k)]
    if FAST_ON:
        session.fast.audit({k for k, _ in pairs})  # всё нарисованное должно быть и в списке API

    if FAST_ON and session.fast.ready and not registry.fresh:
        pass  # заявками ведёт API (он точнее и быстрее), страница нужна для сверки и проверки входа
    else:
        await apply_pairs(app, pairs, "dom")

    await fast_notice(app)


async def process_unauth(app, result) -> None:
    state["authenticated"] = False
    state["last_error"] = result.message
    log.warning("Нет авторизации: %s", result.message)

    if state["auth_alert_sent"]:
        return  # о слёте сессии сообщаем один раз и больше не напоминаем

    sent = await send_all(app, AUTH_ALERT, reply_markup=login_markup())
    if sent:
        state["auth_alert_sent"] = True
        state["auth_alert_msgs"] = sent


def cycle_left(started: float) -> float:
    """Сколько ещё ждать до следующей проверки: период считается от НАЧАЛА предыдущей."""
    interval = state["interval"]
    if FAST_ON and session.fast.ready:  # быстрая линия следит за списком, страница нужна для сверки
        interval = max(interval, FAST_AUDIT)
    return max(0.5, interval - (time.monotonic() - started))


async def monitor_loop(app) -> None:
    while not stop_event.is_set():
        if not state["running"]:
            await pause(1)
            continue

        started = time.monotonic()
        try:
            result = await session.scan()
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            state["errors_in_row"] += 1
            state["last_error"] = f"Ошибка проверки: {exc}"
            log.exception("Ошибка мониторинга")

            errors = state["errors_in_row"]
            if errors == 3:
                await send_all(app, f"⚠️ Проверки сайта падают с ошибкой:\n{exc}"[:1000])
            if errors % 3 == 0:
                try:
                    log.warning("Перезапускаю браузер")
                    await session.restart()
                except Exception:
                    log.exception("Не удалось перезапустить браузер")
            await pause(cycle_left(started))
            continue

        if state["errors_in_row"] >= 3:
            await send_all(app, "✅ Проверки сайта снова работают.")
        state["errors_in_row"] = 0
        state["last_check"] = now_str()
        state["page_title"] = result.title or state["page_title"]
        state["page_url"] = result.url or state["page_url"]

        try:
            if result.status == "ok":
                await process_ok(app, result)
                count = len(result.requests)
                if count != state["last_count"] or time.monotonic() - state["last_log"] > 60:
                    log.info("Проверка завершена: найдено %s заявок", count)  # не чаще раза в минуту
                    state["last_count"], state["last_log"] = count, time.monotonic()
                await pause(cycle_left(started))
            else:
                await process_unauth(app, result)
                await pause(max(state["interval"], config.UNAUTH_INTERVAL))
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("Ошибка обработки результата проверки")
            await pause(cycle_left(started))


# --- Список заявок ----------------------------------------------------------------
# Только актуальные. Каждая заявка — отдельное сообщение с кнопкой «Взять в работу».


async def send_list(app, chat_id: int) -> None:
    active = registry.active()

    if not active:
        hint = "" if state["authenticated"] else "\nСессия не активна — нажми «🔐 Вход»."
        await tg(app.bot.send_message, chat_id, "Актуальных заявок сейчас нет." + hint)
        return

    header = f"📋 <b>Список заявок</b> · актуальных: {len(active)}"
    if not state["authenticated"]:
        header += "\n⚠️ Сессия на сайте не активна, данные могут быть неактуальны."
    await tg(
        app.bot.send_message,
        chat_id=chat_id,
        text=header,
        parse_mode=ParseMode.HTML,
        link_preview_options=NO_PREVIEW,
    )

    for entry in active[:LIST_MAX]:
        current = registry.get(entry["key"])
        if current is None or current["status"] != ACTIVE:
            continue  # пока шла отправка, заявку взяли или она устарела
        html = format_card_html(Card.from_dict(current["card"]))
        message = await tg(
            app.bot.send_message,
            chat_id=chat_id,
            text=html,
            parse_mode=ParseMode.HTML,
            reply_markup=take_markup(current["key"]),
            link_preview_options=NO_PREVIEW,
        )
        register(
            current["key"],
            chat_id,
            message.message_id,
            html,
            f"{html}\n\n{STATUS_EXPIRED}",
            is_list=True,
        )
        await asyncio.sleep(LIST_PAUSE)

    hidden = len(active) - LIST_MAX
    if hidden > 0:
        await tg(
            app.bot.send_message,
            chat_id,
            f"… и ещё {hidden} актуальных (показаны первые {LIST_MAX}).",
        )


async def list_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    if chat_id in listing:  # двойное нажатие: список уже отправляется
        return
    listing.add(chat_id)

    async def run() -> None:
        try:
            await send_list(context.application, chat_id)
        except Exception:
            log.exception("Не удалось отправить список заявок")
        finally:
            listing.discard(chat_id)

    # В фоне, чтобы пока идёт отправка, кнопки «Взять в работу» уже работали.
    context.application.create_task(run(), update=update)


# --- Взять в работу -----------------------------------------------------------------
# Как бот определяет результат: нажал кнопку на сайте и смотрит на список там.
#   заявка пропала с сайта  -> успех
#   заявка осталась / сайт показал ошибку -> не взялась (причина — из сообщения сайта)
# Нажать можно и под уведомлением, и под заявкой из «📋 Списка заявок».
# О результате бот пишет всегда: во всплывающей подсказке, ответом на заявку и
# в самой заявке (у всех пользователей).


async def answer_quietly(query, text: str | None = None) -> bool:
    """Короткая всплывающая подсказка вверху экрана (до 200 символов)."""
    try:
        await query.answer(text=text[:200] if text else None)
        return True
    except Exception as exc:  # например, прошло слишком много времени
        log.warning("Не удалось ответить на нажатие кнопки: %s", exc)
        return False


def failure_html(card_html: str, reason_html: str) -> str:
    return f"❌ <b>Заявка не взята в работу</b>\n\n{card_html}\n\n{reason_html}"


async def take_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    user = query.from_user
    if not users.is_allowed(user.id):
        await query.answer("Нет доступа", show_alert=True)
        return
    message = query.message
    if message is None:
        await query.answer()
        return

    app = context.application
    key = (query.data or "").split(":", 1)[-1]
    chat_id = message.chat.id
    notification = is_notification(message)
    # Ответом на заявку пишем только под уведомлением: заявка из списка может исчезнуть.
    reply_to = message.message_id if notification else None
    who = user.full_name or "кто-то"
    entry = registry.get(key)

    if entry is None:  # уже взята (и убрана из реестра) или давно устарела
        await answer_quietly(query, "ℹ️ Заявка уже обработана")
        await say(app, chat_id, reply_to, "ℹ️ Эта заявка уже обработана: её взяли в работу или она устарела.")
        await finish_messages(app, key, message, "ℹ️ <i>Уже обработана</i>")
        return
    if entry["status"] != ACTIVE:
        await answer_quietly(query, "⌛ Заявка устарела")
        await expire_messages(app, key, message)
        await say(app, chat_id, reply_to, "⌛ Заявка устарела: её уже нет на сайте.")
        return
    if key in taking:
        await answer_quietly(query, "⏳ Заявка уже обрабатывается")
        return

    card_html = format_card_html(Card.from_dict(entry["card"]))

    # Пока идёт нажатие на сайте, кнопка в Telegram «крутится». Отвечаем по результату.
    taking.add(key)
    try:
        result = await session.take(key)
    except Exception as exc:
        log.exception("Ошибка при взятии заявки в работу")
        result = TakeResult("failed", f"Внутренняя ошибка бота: {exc}")
    finally:
        taking.discard(key)

    if result.status == "ok":
        mark_closed(key)
        registry.remove(key)  # пропала с сайта — убираем и из списка бота
        log.info("Заявка %s взята в работу: %s", key, who)
        await answer_quietly(query, "✅ Заявка взята в работу")
        await asyncio.gather(
            say(app, chat_id, message.message_id, f"✅ <b>Заявка взята в работу</b>\n\n{card_html}"),
            send_all(
                app,
                f"✅ <b>{escape(who)}</b> взял(а) в работу заявку\n\n{card_html}",
                parse_mode=ParseMode.HTML,
                exclude={chat_id},
            ),
            finish_messages(app, key, message, f"✅ <i>Взята в работу — {escape(who)}</i>"),
        )
        return

    if result.status == "gone":
        mark_closed(key)
        registry.mark(key, EXPIRED)
        await answer_quietly(query, "❌ Заявки уже нет на сайте")
        await expire_messages(app, key, message)
        await say(
            app,
            chat_id,
            reply_to,
            failure_html(card_html, "Заявки уже нет на сайте: её забрали или она устарела."),
        )
        return

    if result.status in ("need_login", "no_session"):
        state["authenticated"] = False
        await answer_quietly(query, "❌ Сессия на сайте слетела")
        await say(
            app,
            chat_id,
            message.message_id,
            failure_html(card_html, "Сессия на сайте слетела. Нажми «🔐 Вход», затем повтори."),
        )
        return

    reason = (result.message or "причина неизвестна").strip()[:400]
    log.warning("Заявка %s не взялась: %s", key, reason)
    await answer_quietly(query, f"❌ Не взялась: {reason}")
    # Если сайт ничего не написал, прикладываем скриншот страницы — видно, что там было.
    photo = None if result.site_error else result.screenshot
    await say(
        app,
        chat_id,
        message.message_id,
        failure_html(card_html, f"Причина: {escape(reason)}\nКнопка осталась — можно нажать ещё раз."),
        photo,
    )


# --- Пользователи ---------------------------------------------------------------------
# Добавлять и удалять пользователей могут только основные аккаунты (config.CHAT_IDS).

ADD_PROMPT = (
    "➕ <b>Новый пользователь</b>\n\n"
    "Отправь <b>@username</b> или числовой <b>ID</b> аккаунта Telegram.\n\n"
    "• По ID доступ открывается сразу.\n"
    "• По @username — как только этот человек откроет бота и нажмёт /start "
    "(Telegram не позволяет боту найти человека по имени заранее).\n\n"
    "Свой ID можно узнать у бота @userinfobot."
)


def admin_markup() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [InlineKeyboardButton("➕ Добавить пользователя", callback_data="adm:add")],
            [InlineKeyboardButton("👥 Пользователи", callback_data="adm:list")],
        ]
    )


def status_markup(is_owner: bool) -> InlineKeyboardMarkup:
    rows = [[InlineKeyboardButton("⚡ Настроить автовзятие", callback_data="af:open")]]
    if is_owner:
        rows += admin_markup().inline_keyboard
    return InlineKeyboardMarkup(rows)


def users_view() -> tuple[str, InlineKeyboardMarkup]:
    lines = ["👥 <b>Пользователи бота</b>", "", "<b>Основные</b> (их нельзя удалить):"]
    lines += [f"• <code>{uid}</code>" for uid in users.owners()]

    added = users.added()
    if added:
        lines += ["", "<b>Добавленные:</b>"]
        for e in added:
            if e.get("name") or e.get("username"):
                lines.append(f"• {escape(users.label(e))} — <code>{e['id']}</code>")
            else:  # имя станет известно, когда человек сам напишет боту
                lines.append(f"• <code>{e['id']}</code> — ещё не открывал(а) бота")

    waiting = users.pending()
    if waiting:
        lines += ["", "<b>Ждут, пока нажмут /start:</b>"]
        lines += [f"• @{escape(p['username'])}" for p in waiting]

    rows = [
        [InlineKeyboardButton(f"❌ {users.label(e)[:40]}", callback_data=f"adm:rm:{e['id']}")]
        for e in added
    ]
    rows += [
        [InlineKeyboardButton(f"❌ @{p['username']} (ждёт /start)", callback_data=f"adm:rmp:{p['username']}")]
        for p in waiting
    ]
    rows.append([InlineKeyboardButton("➕ Добавить пользователя", callback_data="adm:add")])
    return "\n".join(lines), InlineKeyboardMarkup(rows)


async def prompt_add_user(app, chat_id: int) -> None:
    state["adding_user"].add(chat_id)
    await tg(
        app.bot.send_message,
        chat_id=chat_id,
        text=ADD_PROMPT,
        parse_mode=ParseMode.HTML,
        reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("✖️ Отмена", callback_data="adm:cancel")]]),
    )


async def invite(app, uid: int) -> bool:
    """Сообщение новому пользователю. Не получится, пока он сам не нажмёт /start у бота."""
    try:
        await tg(
            app.bot.send_message,
            chat_id=uid,
            text="👋 Тебе открыт доступ к боту мониторинга заявок.\nНажми /start, чтобы начать.",
            reply_markup=MENU,
        )
        return True
    except Exception as exc:
        log.info("Не удалось написать новому пользователю %s: %s", uid, exc)
        return False


async def add_user_input(app, chat_id: int, admin, text: str) -> None:
    kind, value = users.parse_target(text)
    if kind == "error":
        await tg(
            app.bot.send_message,
            chat_id=chat_id,
            text=f"❌ {value}\nОтправь @username или ID ещё раз либо /cancel.",
        )
        return

    state["adding_user"].discard(chat_id)
    by = admin.full_name or str(admin.id)

    if kind == "id":
        if users.add_by_id(int(value), by) == "exists":
            reply = f"ℹ️ У аккаунта <code>{value}</code> уже есть доступ."
        elif await invite(app, int(value)):
            reply = f"✅ Пользователь <code>{value}</code> добавлен и получил приглашение."
        else:
            reply = (
                f"✅ Пользователь <code>{value}</code> добавлен.\n"
                "⚠️ Написать ему бот пока не может: пусть откроет бота и нажмёт /start. "
                "После этого начнут приходить уведомления."
            )
    else:
        result = users.add_by_username(str(value), by)
        if result == "exists":
            reply = f"ℹ️ У @{value} уже есть доступ."
        elif result == "waiting":
            reply = f"ℹ️ @{value} уже в списке ожидания: доступ откроется, когда он нажмёт /start у бота."
        else:
            reply = (
                f"✅ @{value} добавлен(а).\n"
                "Доступ откроется, как только этот аккаунт откроет бота и нажмёт /start "
                f"(Telegram не позволяет боту написать первым). Ожидание действует {users.PENDING_DAYS} дней."
            )

    await tg(
        app.bot.send_message,
        chat_id=chat_id,
        text=reply,
        parse_mode=ParseMode.HTML,
        reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("👥 Пользователи", callback_data="adm:list")]]),
    )


async def admin_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    user = query.from_user
    if not users.is_owner(user.id):
        await query.answer("Доступно только основным аккаунтам", show_alert=True)
        return
    if query.message is None:
        await query.answer()
        return

    app = context.application
    chat_id = query.message.chat.id
    data = query.data or ""

    async def refresh() -> None:
        text, markup = users_view()
        try:
            await query.edit_message_text(text, parse_mode=ParseMode.HTML, reply_markup=markup)
        except Exception as exc:
            if "not modified" not in str(exc).lower():
                log.warning("Не удалось обновить список пользователей: %s", exc)

    if data == "adm:add":
        await query.answer()
        await prompt_add_user(app, chat_id)
    elif data == "adm:cancel":
        state["adding_user"].discard(chat_id)
        await query.answer("Отменено")
        try:
            await query.edit_message_text("Добавление отменено.")
        except Exception:
            pass
    elif data == "adm:list":
        await query.answer()
        text, markup = users_view()
        await tg(
            app.bot.send_message,
            chat_id=chat_id,
            text=text,
            parse_mode=ParseMode.HTML,
            reply_markup=markup,
        )
    elif data.startswith("adm:rm:"):
        try:
            uid = int(data.rsplit(":", 1)[-1])
        except ValueError:
            await query.answer()
            return
        removed = users.remove_user(uid)
        await query.answer("Пользователь удалён" if removed else "Его уже нет в списке")
        await refresh()
    elif data.startswith("adm:rmp:"):
        removed = users.remove_pending(data.split(":", 2)[-1])
        await query.answer("Убран из ожидания" if removed else "Его уже нет в списке")
        await refresh()
    else:
        await query.answer()


async def adduser_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not users.is_owner(update.effective_user.id):
        await update.message.reply_text("Добавлять пользователей могут только основные аккаунты.")
        return
    if context.args:
        await add_user_input(
            context.application, update.effective_chat.id, update.effective_user, " ".join(context.args)
        )
    else:
        await prompt_add_user(context.application, update.effective_chat.id)


async def users_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not users.is_owner(update.effective_user.id):
        await update.message.reply_text("Список пользователей доступен только основным аккаунтам.")
        return
    text, markup = users_view()
    await update.message.reply_text(text, parse_mode=ParseMode.HTML, reply_markup=markup)


async def track_user(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Срабатывает на любое сообщение: человек, добавленный по @username, нажал /start у бота."""
    user = update.effective_user
    if user is None:
        return
    joined = users.seen(user)
    if joined:
        await send_all(
            context.application,
            f"👤 <b>{escape(users.label(joined))}</b> открыл(а) бота — доступ предоставлен.",
            parse_mode=ParseMode.HTML,
            to=users.owners(),
        )


# --- Настройки автовзятия ----------------------------------------------------------
# Открываются кнопкой «⚡ Автовзятие» (или /auto и кнопкой под «📊 Статусом»).
# Настройки общие для всех пользователей и хранятся в /data/autotake.json.


def auto_panel() -> tuple[str, InlineKeyboardMarkup]:
    cfg = autotake.settings
    lines = [
        "⚡ <b>Автовзятие заявок</b>",
        "",
        f"Статус: <b>{'включено ✅' if cfg.enabled else 'выключено ⛔'}</b>",
        "",
        "Бот сам берёт в работу новую заявку, если она подходит под <b>оба</b> фильтра: "
        "способ оплаты и сумма. Остальные приходят как обычно — с кнопкой «Взять в работу».",
        "",
        f"<b>Способ оплаты:</b> {escape(autotake.types_label())}",
        f"<b>Суммы:</b> {escape(autotake.ranges_label())}",
    ]
    if not autotake.ready():
        lines += ["", "⚠️ Выбери способ оплаты и хотя бы один диапазон: иначе автовзятие ничего не возьмёт."]
    lines += [
        "",
        "<i>Нажимай на фильтры, чтобы включить или выключить их. "
        "Суммы считаются в целых рублях: заявка на 3 999,60 ₽ попадает в диапазон 3 000–3 999.</i>",
    ]

    def mark(on: bool) -> str:
        return "✅" if on else "⬜"

    rows = [
        [
            InlineKeyboardButton(f"{mark(kind in cfg.types)} {label}", callback_data=f"af:pay:{kind}")
            for kind, label in autotake.PAY_LABELS.items()
        ]
    ]
    buttons = [
        InlineKeyboardButton(
            f"{mark(rid in cfg.ranges)} {autotake.range_label(lo, hi)}", callback_data=f"af:r:{rid}"
        )
        for rid, (lo, hi) in autotake.RANGE_BY_ID.items()
    ]
    rows += [buttons[i : i + 2] for i in range(0, len(buttons), 2)]
    rows.append(
        [
            InlineKeyboardButton("☑️ Выбрать все", callback_data="af:all"),
            InlineKeyboardButton("🗑 Сбросить", callback_data="af:none"),
        ]
    )
    rows.append(
        [
            InlineKeyboardButton("🔴 Выключить автовзятие", callback_data="af:off")
            if cfg.enabled
            else InlineKeyboardButton("🟢 Включить автовзятие", callback_data="af:on")
        ]
    )
    return "\n".join(lines), InlineKeyboardMarkup(rows)


async def fast_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(session.fast.details())


async def auto_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text, markup = auto_panel()
    await update.message.reply_text(text, parse_mode=ParseMode.HTML, reply_markup=markup)


async def autotake_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    user = query.from_user
    if not users.is_allowed(user.id):
        await query.answer("Нет доступа", show_alert=True)
        return
    message = query.message
    if message is None:
        await query.answer()
        return

    app = context.application
    parts = (query.data or "").split(":")
    kind = parts[1] if len(parts) > 1 else ""
    arg = parts[2] if len(parts) > 2 else ""
    who = escape(user.full_name or "кто-то")

    if kind == "open":  # кнопка под «📊 Статусом»
        await query.answer()
        text, markup = auto_panel()
        await tg(
            app.bot.send_message,
            chat_id=message.chat.id,
            text=text,
            parse_mode=ParseMode.HTML,
            reply_markup=markup,
        )
        return

    toast = None
    if kind == "pay":
        autotake.toggle_type(arg)
    elif kind == "r":
        autotake.toggle_range(arg)
    elif kind == "all":
        autotake.select_all_ranges()
    elif kind == "none":
        autotake.clear_ranges()
    elif kind == "on":
        if not autotake.ready():
            await query.answer("Сначала выбери способ оплаты и хотя бы один диапазон сумм.", show_alert=True)
            return
        if not autotake.settings.enabled:
            autotake.set_enabled(True)
            toast = "⚡ Автовзятие включено"
            log.info("Автовзятие включено: %s (%s)", user.full_name, autotake.status_line())
            await send_all(
                app,
                f"⚡ {who} включил(а) автовзятие:\n"
                f"{escape(autotake.types_label())} · {escape(autotake.ranges_label())}",
                parse_mode=ParseMode.HTML,
                exclude={message.chat.id},
            )
    elif kind == "off":
        if autotake.settings.enabled:
            autotake.set_enabled(False)
            toast = "Автовзятие выключено"
            log.info("Автовзятие выключено: %s", user.full_name)
            await send_all(
                app,
                f"⚡ {who} выключил(а) автовзятие. Заявки снова приходят с кнопкой «Взять в работу».",
                parse_mode=ParseMode.HTML,
                exclude={message.chat.id},
            )

    await answer_quietly(query, toast)
    text, markup = auto_panel()
    try:
        await query.edit_message_text(text, parse_mode=ParseMode.HTML, reply_markup=markup)
    except Exception as exc:
        if "not modified" not in str(exc).lower():
            log.warning("Не удалось обновить панель автовзятия: %s", exc)


# --- Обычные команды --------------------------------------------------------------

HELP_TEXT = (
    "Команды:\n"
    "/list — список актуальных заявок (с кнопкой «Взять в работу»)\n"
    "/login — войти на сайт (логин, пароль, 2FA)\n"
    "/logout — удалить сохранённую сессию\n"
    "/auto — настройки автовзятия (фильтры, вкл/выкл)\n"
    "/fast — состояние быстрой линии (прямой доступ к API сайта)\n"
    "/status — статус мониторинга и управление пользователями\n"
    "/check — диагностика\n"
    "/screen — скриншот страницы глазами бота\n"
    "/adduser — добавить пользователя (основные аккаунты)\n"
    "/users — список пользователей (основные аккаунты)\n"
    "/stop, /run — пауза / продолжить\n"
    "/cancel — отменить вход или добавление"
)


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = "Монитор заявок запущен.\n"
    if not session.has_session:
        text += "⚠️ Сессии на сайте нет — нажми «🔐 Вход».\n"
    await update.message.reply_text(text + "\n" + HELP_TEXT, reply_markup=MENU)


async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(HELP_TEXT, reply_markup=MENU)


async def nothing_to_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    if chat_id in state["adding_user"]:
        state["adding_user"].discard(chat_id)
        await update.message.reply_text("Добавление отменено.", reply_markup=MENU)
        return
    await update.message.reply_text("Сейчас нечего отменять.", reply_markup=MENU)


async def interval_info(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        f"Страница проверяется примерно раз в {state['interval']:g} сек. Менять это не нужно.",
        reply_markup=MENU,
    )


async def test(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("✅ Telegram работает. Бот получил команду.")


def session_label() -> str:
    if state["authenticated"]:
        return "активна ✅"
    if session.has_session:
        return "слетела ⚠️ (нужен вход)"
    return "нет ❌ (нужен вход)"


async def check(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        f"Страница: {state['page_url']}\n"
        f"Заголовок: {state['page_title'] or 'неизвестен'}\n"
        f"Актуальных заявок: {len(registry.active())}\n"
        f"Последняя проверка: {state['last_check']}\n"
        f"Браузер готов: {'да' if session.browser_ready else 'нет'}\n"
        f"Сессия: {session_label()}\n"
        f"Ошибка: {state['last_error'] or 'нет'}"
    )


async def status(update: Update, context: ContextTypes.DEFAULT_TYPE):
    markup = status_markup(users.is_owner(update.effective_user.id))
    await update.message.reply_text(
        "📊 Статус мониторинга\n"
        f"Работает: {'да' if state['running'] else 'нет'}\n"
        f"Проверка страницы: каждые ~{state['interval']:g} сек.\n"
        f"⚡ Автовзятие: {autotake.status_line()}\n"
        f"🚀 Быстрая линия: {session.fast.describe()}\n"
        f"Сессия на сайте: {session_label()}\n"
        f"Актуальных заявок: {len(registry.active())}\n"
        f"Пользователей с доступом: {len(users.chat_ids())}\n"
        f"Последняя проверка: {state['last_check']}\n"
        f"Браузер готов: {'да' if session.browser_ready else 'нет'}\n"
        f"Ошибка: {state['last_error'] or 'нет'}",
        reply_markup=markup,
    )


async def stop_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    state["running"] = False
    save_settings()
    await update.message.reply_text("⏸ Проверка остановлена. Нажми «▶️ Запуск», чтобы продолжить.")
    await send_all(
        context.application,
        f"⏸ {person(update)} остановил(а) проверку.",
        exclude={update.effective_chat.id},
    )


async def run_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    state["running"] = True
    save_settings()
    wake.set()
    await update.message.reply_text("▶️ Проверка снова включена.")
    await send_all(
        context.application,
        f"▶️ {person(update)} включил(а) проверку.",
        exclude={update.effective_chat.id},
    )


async def screen_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        shot = await session.screenshot()
    except Exception as exc:
        await update.message.reply_text(f"Не удалось сделать скриншот: {exc}")
        return
    if not shot:
        await update.message.reply_text("Скриншота нет: сессии/страницы ещё нет. Нажми «🔐 Вход».")
        return
    await update.message.reply_photo(
        photo=shot,
        caption=f"{state['page_url']}\nПоследняя проверка: {state['last_check']}"[:1000],
    )


async def logout_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await close_flow()
    release_login()
    await session.logout()
    state["authenticated"] = False
    state["auth_alert_sent"] = False
    await clear_auth_alert(context.application)
    await update.message.reply_text(
        "🚪 Сессия удалена с сервера. Чтобы продолжить мониторинг, нажми «🔐 Вход».",
        reply_markup=MENU,
    )
    await send_all(
        context.application,
        f"🚪 {person(update)} удалил(а) сессию на сайте. Нужен новый вход («🔐 Вход»).",
        exclude={update.effective_chat.id},
    )


async def text_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = (update.message.text or "").strip()
    chat_id = update.effective_chat.id

    actions = {
        BTN_LIST: list_command,
        BTN_STATUS: status,
        BTN_AUTO: auto_command,
        BTN_SCREEN: screen_command,
        BTN_STOP: stop_command,
        BTN_RUN: run_command,
        LEGACY_INTERVAL_BUTTON: interval_info,
    }
    for legacy in LEGACY_LIST_BUTTONS:
        actions[legacy] = list_command

    action = actions.get(text)
    if action:
        state["adding_user"].discard(chat_id)  # нажали кнопку меню — добавление отменяется
        context.args = []
        await action(update, context)
        return

    if chat_id in state["adding_user"] and users.is_owner(update.effective_user.id):
        await add_user_input(context.application, chat_id, update.effective_user, text)


# --- Вход на сайт через бота -----------------------------------------------------
# Сессия на сайте одна на всех, поэтому одновременно входить может только один аккаунт.


def claim_left(chat_id: int) -> int:
    """Сколько секунд ещё занят вход другим аккаунтом (0 — свободно)."""
    owner = login_claim["chat"]
    if owner is None or owner == chat_id:
        return 0
    left = config.LOGIN_TIMEOUT - (time.monotonic() - login_claim["since"])
    if left <= 0:
        login_claim["chat"] = None
        return 0
    return int(left) + 1


def claim_login(chat_id: int) -> None:
    login_claim.update(chat=chat_id, since=time.monotonic())


def release_login(chat_id: int | None = None) -> None:
    if chat_id is None or login_claim["chat"] == chat_id:
        login_claim["chat"] = None


async def close_flow() -> None:
    global login_flow
    flow, login_flow = login_flow, None
    if flow is not None:
        await flow.close()


async def login_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Вход: из меню «🔐 Вход», командой /login или кнопкой под сообщением о слёте сессии."""
    chat = update.effective_chat
    query = update.callback_query

    if query is not None:  # нажали «🔐 Вход» под сообщением о слёте сессии
        if not users.is_allowed(update.effective_user.id):
            await query.answer("Нет доступа", show_alert=True)
            return ConversationHandler.END
        if state["authenticated"] and session.has_session:
            await query.answer("Сессия уже активна ✅")
            if query.message is not None:
                await _strip_one(context.application, chat.id, query.message.message_id)
            return ConversationHandler.END
        await query.answer()

    left = claim_left(chat.id)
    if left:
        await chat.send_message(
            "⏳ Вход на сайт сейчас проходит другой аккаунт. "
            f"Подожди около {left} сек. или проверь «📊 Статус» — возможно, сессия уже обновлена.",
            reply_markup=MENU,
        )
        return ConversationHandler.END

    await close_flow()
    claim_login(chat.id)
    state["adding_user"].discard(chat.id)
    context.user_data.clear()
    await chat.send_message(
        "🔐 Вход на сайт\n\n"
        "Сессия создаётся прямо на сервере бота — с его IP, поэтому она не слетит "
        "из-за смены адреса. Сессия общая для всех аккаунтов.\n"
        "Сообщения с логином и паролем я сразу удаляю из чата, на диск они не пишутся.\n\n"
        "Шаг 1. Отправь логин.\nОтмена: /cancel",
        reply_markup=LOGIN_MENU,
    )
    return LOGIN_STATE


async def got_login(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data["login"] = (update.message.text or "").strip()
    await delete_quietly(update.message)
    await update.effective_chat.send_message("Шаг 2. Отправь пароль (сообщение будет удалено).")
    return PASSWORD_STATE


async def login_success(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    global login_flow
    login_flow = None
    chat = update.effective_chat
    release_login(chat.id)
    state["authenticated"] = True
    state["auth_alert_sent"] = False
    state["last_error"] = ""
    await clear_auth_alert(context.application)
    wake.set()
    await chat.send_message(
        "✅ Вход выполнен. Сессия сохранена на сервере (в /data).\n"
        "Сейчас сразу запущу проверку заявок.",
        reply_markup=MENU,
    )
    await send_all(
        context.application,
        f"🔐 {person(update)} обновил(а) сессию на сайте. Мониторинг продолжается.",
        exclude={chat.id},
    )


async def login_fail(update: Update, flow: LoginFlow, error: Exception) -> None:
    shot = await flow.screenshot()
    await close_flow()
    chat = update.effective_chat
    release_login(chat.id)
    text = f"❌ Не удалось войти: {error}\nНажми «🔐 Вход», чтобы попробовать снова."
    if shot:
        await chat.send_photo(photo=shot, caption=text[:1000], reply_markup=MENU)
    else:
        await chat.send_message(text[:4000], reply_markup=MENU)


async def got_password(update: Update, context: ContextTypes.DEFAULT_TYPE):
    global login_flow
    password = update.message.text or ""
    await delete_quietly(update.message)
    username = context.user_data.pop("login", "")
    chat = update.effective_chat

    await chat.send_message("⏳ Открываю сайт и ввожу данные, это может занять до 30 секунд...")

    await close_flow()
    flow = LoginFlow(session)
    login_flow = flow
    claim_login(chat.id)
    try:
        result = await flow.start(username, password)
    except LoginError as exc:
        await login_fail(update, flow, exc)
        return ConversationHandler.END
    except Exception as exc:
        log.exception("Ошибка при входе")
        await login_fail(update, flow, RuntimeError(f"непредвиденная ошибка: {exc}"))
        return ConversationHandler.END
    finally:
        password = ""

    if result == "2fa":
        await chat.send_message(
            "Шаг 3. Сайт просит код 2FA.\n"
            "Отправь текущий код из приложения-аутентификатора "
            f"(у тебя {config.LOGIN_TIMEOUT // 60} мин.).\nОтмена: /cancel"
        )
        return CODE_STATE

    await login_success(update, context)
    return ConversationHandler.END


async def got_code(update: Update, context: ContextTypes.DEFAULT_TYPE):
    flow = login_flow
    chat = update.effective_chat
    code = re.sub(r"[\s-]+", "", update.message.text or "")
    await delete_quietly(update.message)

    if flow is None or flow.expired or login_claim["chat"] != chat.id:
        if login_claim["chat"] == chat.id:
            await close_flow()
            release_login(chat.id)
        await chat.send_message(
            "⌛ Вход уже не активен (время вышло или его начал другой аккаунт). "
            "Нажми «🔐 Вход» и начни заново.",
            reply_markup=MENU,
        )
        return ConversationHandler.END

    if not re.fullmatch(r"[0-9A-Za-z]{4,12}", code):
        await chat.send_message("Код выглядит странно. Отправь только цифры кода или /cancel.")
        return CODE_STATE

    await chat.send_message("⏳ Проверяю код...")
    try:
        await flow.submit_code(code)
    except LoginError as exc:
        if exc.retry and not flow.expired:
            shot = await flow.screenshot()
            text = f"❌ {exc}\nОтправь новый код (дождись обновления в приложении) или /cancel."
            if shot:
                await chat.send_photo(photo=shot, caption=text[:1000])
            else:
                await chat.send_message(text)
            return CODE_STATE
        await login_fail(update, flow, exc)
        return ConversationHandler.END
    except Exception as exc:
        log.exception("Ошибка при вводе кода")
        await login_fail(update, flow, RuntimeError(f"непредвиденная ошибка: {exc}"))
        return ConversationHandler.END

    await login_success(update, context)
    return ConversationHandler.END


async def login_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    if login_claim["chat"] == chat_id:
        await close_flow()
        release_login(chat_id)
    context.user_data.clear()
    await update.message.reply_text("Вход отменён.", reply_markup=MENU)
    return ConversationHandler.END


# --- Жизненный цикл ----------------------------------------------------------------


async def error_handler(update, context: ContextTypes.DEFAULT_TYPE):
    log.error("Ошибка в обработчике", exc_info=context.error)


async def _push_safely(app, pairs) -> None:
    try:
        await process_push(app, pairs)
    except Exception:
        log.exception("Ошибка обработки push-заявки")


async def post_init(app: Application) -> None:
    global monitor_task, fast_task
    load_settings()
    registry.load()
    users.load()
    autotake.load()

    startup_error = ""
    try:
        await session.start()
    except Exception as exc:
        startup_error = str(exc)
        log.exception("Не удалось запустить браузер")

    session.fast.on_new = lambda pairs: asyncio.ensure_future(_push_safely(app, pairs))
    monitor_task = asyncio.create_task(monitor_loop(app))
    fast_task = asyncio.create_task(fast_loop(app))

    if startup_error:
        text = f"❌ Браузер не запустился: {startup_error[:500]}"
    elif session.has_session:
        text = "🤖 Монитор запущен. Сессия восстановлена, проверяю страницу."
    else:
        text = "🤖 Монитор запущен."  # если сессии нет, сразу следом придёт сообщение с кнопкой «Вход»
    await send_all(app, text, reply_markup=MENU)


async def post_shutdown(app: Application) -> None:
    stop_event.set()
    wake.set()
    await close_flow()
    for task in (monitor_task, fast_task):
        if task:
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
    try:
        await session.save_state()
    except Exception:
        pass
    await session.close()


def build_application() -> Application:
    application = (
        Application.builder()
        .token(config.TOKEN)
        .post_init(post_init)
        .post_shutdown(post_shutdown)
        .build()
    )

    # Раньше всех: человек, добавленный по @username, нажимает /start — запоминаем его ID.
    application.add_handler(TypeHandler(Update, track_user), group=-1)

    # Диалог входа — первым, чтобы он перехватывал логин/пароль/код.
    application.add_handler(
        ConversationHandler(
            entry_points=[
                CommandHandler("login", login_start, filters=ALLOWED),
                MessageHandler(ALLOWED & LOGIN_BUTTON_FILTER, login_start),
                CallbackQueryHandler(login_start, pattern=r"^login$"),  # кнопка под «Сессия слетела»
            ],
            states={
                LOGIN_STATE: [MessageHandler(LOGIN_INPUT, got_login)],
                PASSWORD_STATE: [MessageHandler(LOGIN_INPUT, got_password)],
                CODE_STATE: [MessageHandler(LOGIN_INPUT, got_code)],
            },
            fallbacks=[
                CommandHandler("cancel", login_cancel, filters=ALLOWED),
                MessageHandler(ALLOWED & CANCEL_FILTER, login_cancel),
            ],
            allow_reentry=True,
        )
    )

    commands = {
        "start": start,
        "help": help_command,
        "test": test,
        "check": check,
        "list": list_command,
        "requests": list_command,  # старые команды остаются как синонимы
        "new": list_command,
        "status": status,
        "auto": auto_command,
        "fast": fast_command,
        "adduser": adduser_command,
        "users": users_command,
        "stop": stop_command,
        "run": run_command,
        "screen": screen_command,
        "logout": logout_command,
        "cancel": nothing_to_cancel,
    }
    for name, handler in commands.items():
        application.add_handler(CommandHandler(name, handler, filters=ALLOWED))

    # block=False: пока бот нажимает кнопку на сайте, остальные команды не ждут.
    application.add_handler(CallbackQueryHandler(take_callback, pattern=r"^take:", block=False))
    application.add_handler(CallbackQueryHandler(admin_callback, pattern=r"^adm:"))
    application.add_handler(CallbackQueryHandler(autotake_callback, pattern=r"^af:"))
    application.add_handler(MessageHandler(ALLOWED & filters.TEXT & ~filters.COMMAND, text_handler))
    application.add_error_handler(error_handler)
    return application


def main() -> None:
    if not config.TOKEN:
        raise RuntimeError(
            "Не задан TELEGRAM_BOT_TOKEN. Добавь токен в переменные окружения Amvera."
        )
    log.info("Запуск Telegram-бота. Основных аккаунтов: %s", len(config.CHAT_IDS))
    build_application().run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    main()
