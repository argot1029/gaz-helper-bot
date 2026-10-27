"""Браузер: хранение сессии сайта, сканирование заявок и вход через Telegram."""
import asyncio
import json
import logging
import os
import re
import time
from dataclasses import dataclass, field
from urllib.parse import urlparse

from playwright.async_api import (
    Browser,
    BrowserContext,
    Locator,
    Page,
    Playwright,
    TimeoutError as PlaywrightTimeoutError,
    async_playwright,
)

import config
from cards import CARD_LABEL, Card, keys_for, parse_card
from fastlane import FAST_ON, FastLane
from storage import read_json, write_json

log = logging.getLogger("browser")


# Находит карточки по кнопке «Взять в работу» (саму кнопку НЕ нажимает).
# Поднимается вверх от кнопки до первого контейнера с достаточным текстом,
# но не выше, чем контейнер, в который попала вторая кнопка (граница карточки).
EXTRACT_JS = """
(label) => {
  document.querySelectorAll('[data-gzbot]').forEach((el) => el.removeAttribute('data-gzbot'));
  const visible = (el) => !!(el.offsetWidth || el.offsetHeight || el.getClientRects().length);
  const same = (el) => (el.textContent || '').trim() === label;
  const buttons = Array.from(document.querySelectorAll('body *')).filter(
    (el) => same(el) && visible(el) && !Array.from(el.children).some(same)
  );
  const result = [];
  for (const btn of buttons) {
    let node = btn;
    let card = '';
    for (let level = 0; level < 7 && node.parentElement; level++) {
      node = node.parentElement;
      const holder = node;
      if (buttons.filter((b) => holder.contains(b)).length > 1) break;
      const text = (node.innerText || '').trim();
      if (text.replace(label, '').trim().length >= 15) { card = text; break; }
    }
    if (!card) {
      card = ((btn.parentElement && btn.parentElement.innerText) || btn.innerText || '').trim();
    }
    if (!card) continue;
    // Помечаем кнопку, чтобы потом нажать именно её.
    const target = btn.closest('button, a, [role="button"]') || btn;
    const idx = result.length;
    target.setAttribute('data-gzbot', String(idx));
    result.push({ text: card, idx: idx });
  }
  return result;
}
"""

# Восстанавливает sessionStorage (его не сохраняет storage_state).
RESTORE_JS = """
(() => {
  try {
    if (location.origin !== __ORIGIN__) return;
    const data = __DATA__;
    for (const key of Object.keys(data)) {
      if (window.sessionStorage.getItem(key) === null) {
        window.sessionStorage.setItem(key, data[key]);
      }
    }
  } catch (e) {}
})();
"""

LOGIN_URL_RE = re.compile(
    r"/(login|signin|sign-in|sign_in|auth|authorization|2fa|mfa|otp)(/|$|\?|#)",
    re.I,
)
LOGIN_TEXT_RE = re.compile(
    r"сессия истекла|войти в аккаунт|вход в аккаунт|"
    r"sign in to your account|log in to your account",
    re.I,
)
TWO_FA_TEXT_RE = re.compile(
    r"2fa|two[- ]?factor|двухфактор|двухэтап|одноразов|authenticator|"
    r"код подтверждения|код из приложения|verification code|one[- ]time",
    re.I,
)
ERROR_TEXT_RE = re.compile(
    r"неверн|неправильн|некорректн|ошибк|не найден|заблокирован|слишком много|"
    r"incorrect|invalid|wrong|failed|too many|blocked|expired",
    re.I,
)

USER_SELECTORS = [
    'input[autocomplete="username"]',
    'input[type="email"]',
    'input[name*="email" i]',
    'input[name*="login" i]',
    'input[name*="user" i]',
    'input[name*="phone" i]',
    'input[id*="email" i]',
    'input[id*="login" i]',
    'input[id*="user" i]',
    'input[placeholder*="mail" i]',
    'input[placeholder*="логин" i]',
    'input[placeholder*="login" i]',
    'input[type="text"]',
    'input[type="tel"]',
    "input:not([type])",
]
PASSWORD_SELECTORS = ['input[type="password"]']
OTP_SELECTORS = [
    'input[autocomplete="one-time-code"]',
    'input[name*="otp" i]',
    'input[id*="otp" i]',
    'input[name*="2fa" i]',
    'input[id*="2fa" i]',
    'input[name*="totp" i]',
    'input[name*="code" i]',
    'input[id*="code" i]',
    'input[placeholder*="код" i]',
    'input[placeholder*="code" i]',
    'input[inputmode="numeric"]',
    'input[maxlength="6"]',
    'input[maxlength="1"]',
]
SUBMIT_SELECTORS = [
    'button[type="submit"]',
    'input[type="submit"]',
    'button:has-text("Войти")',
    'button:has-text("Вход")',
    'button:has-text("Log in")',
    'button:has-text("Login")',
    'button:has-text("Sign in")',
    'button:has-text("Продолжить")',
    'button:has-text("Подтвердить")',
    'button:has-text("Далее")',
    'button:has-text("Отправить")',
    'button:has-text("Continue")',
    'button:has-text("Verify")',
]
ENTER_LINK_SELECTORS = [
    'a:has-text("Войти")',
    'button:has-text("Войти")',
    'a:has-text("Login")',
    'button:has-text("Login")',
    'a:has-text("Sign in")',
    'button:has-text("Sign in")',
]
ANY_INPUT_SELECTOR = (
    'input:not([type="hidden"]):not([type="checkbox"])'
    ':not([type="radio"]):not([type="password"])'
)


def _with_override(override: str, defaults: list[str]) -> list[str]:
    return ([override] if override else []) + defaults


class LoginError(Exception):
    """Ошибка входа. retry=True — можно повторить ввод (например, код 2FA)."""

    def __init__(self, message: str, retry: bool = False):
        super().__init__(message)
        self.retry = retry


@dataclass
class ScanResult:
    status: str  # "ok" | "need_login" | "no_session"
    requests: list[str] = field(default_factory=list)
    title: str = ""
    url: str = ""
    message: str = ""


@dataclass
class TakeResult:
    # ok | gone | need_login | failed | no_session
    status: str
    message: str = ""
    screenshot: bytes | None = None
    site_error: bool = False  # message — это текст ошибки с самого сайта


FRESH_PAGE_SECONDS = 3  # столько секунд страница считается «свежей»

CONFIRM_RE = re.compile(
    r"^\s*(да|подтвердить|подтверждаю|ок|ok|взять|взять в работу|принять|продолжить|confirm|yes)[\s.!]*$",
    re.I,
)
MODAL_SELECTOR = (
    '[role="dialog"], [role="alertdialog"], dialog[open], '
    '[class*="modal" i], [class*="dialog" i], [class*="popup" i]'
)

TAKE_WAIT_SECONDS = 6  # сколько ждём, пока заявка пропадёт из списка

# Признаки ошибки в сообщениях сайта (всплывашки, alert и т.п.).
ERROR_HINT_RE = re.compile(
    r"ошибк|не удалось|невозможно|нельзя|недостаточно|недоступн|лимит|превыш|отклон|запрещ|"
    r"уже (?:взят|в работе|занят|обработан)|не найден|истек|истёк|"
    r"error|fail|unable|cannot|can't|not enough|already|forbidden|denied|limit|expired|invalid",
    re.I,
)
SUCCESS_HINT_RE = re.compile(r"успешн|success|принят|готово", re.I)

# Запоминает всплывающие сообщения сайта, даже если они быстро исчезают.
WATCH_JS = r"""
() => {
  if (window.__gzWatch) window.__gzWatch.stop();
  const sel = '[role="alert"], [role="status"], [class*="toast" i], [class*="snack" i], ' +
    '[class*="notif" i], [class*="alert" i], [class*="error" i], [class*="message" i], [class*="flash" i]';
  const errSel = '[class*="error" i], [class*="danger" i], [class*="fail" i], [class*="invalid" i]';
  const found = [];
  const seen = new Set();
  const add = (el) => {
    const text = (el.innerText || el.textContent || '').trim().replace(/\s+/g, ' ');
    if (!text || text.length > 300 || seen.has(text)) return;
    seen.add(text);
    found.push({ text: text, flagged: !!(el.matches(errSel) || el.closest(errSel)) });
  };
  const visit = (node) => {
    if (node && node.nodeType === 3) node = node.parentElement;
    if (!node || node.nodeType !== 1) return;
    const holder = node.closest(sel);
    if (holder) add(holder);
    if (node.querySelectorAll) node.querySelectorAll(sel).forEach(add);
  };
  const obs = new MutationObserver((list) => {
    for (const m of list) {
      m.addedNodes.forEach(visit);
      if (m.type === 'characterData') visit(m.target);
    }
  });
  obs.observe(document.body, { childList: true, subtree: true, characterData: true });
  window.__gzWatch = { found: found, stop: () => obs.disconnect() };
}
"""


# --- Вспомогательные функции -------------------------------------------------


async def first_visible(
    page: Page, selectors: list[str], enabled: bool = False
) -> Locator | None:
    for selector in selectors:
        try:
            loc = page.locator(selector)
            count = min(await loc.count(), 20)
            for i in range(count):
                item = loc.nth(i)
                if not await item.is_visible():
                    continue
                if enabled and not await item.is_enabled():
                    continue
                return item
        except Exception:
            continue
    return None


async def page_text(page: Page, limit: int = 6000) -> str:
    try:
        return (await page.locator("body").inner_text(timeout=5000))[:limit]
    except Exception:
        return ""


async def settle(page: Page, idle_timeout: int = 10000, extra: int = 1500) -> None:
    try:
        await page.wait_for_load_state("networkidle", timeout=idle_timeout)
    except PlaywrightTimeoutError:
        pass
    await page.wait_for_timeout(extra)


# Сколько кнопок «Взять в работу» видно, сколько текста на странице и не «Loading…» ли это.
SIGNATURE_JS = """
(label) => {
  const visible = (el) => !!(el.offsetWidth || el.offsetHeight || el.getClientRects().length);
  const same = (el) => (el.textContent || '').trim() === label;
  let buttons = 0;
  for (const el of document.querySelectorAll('body *')) {
    if (same(el) && visible(el) && !Array.from(el.children).some(same)) buttons++;
  }
  const text = ((document.body && document.body.innerText) || '').trim();
  const loading = text.length < 60 && /loading|загрузк/i.test(text);
  return [buttons, text.length, loading ? 1 : 0];
}
"""


class NetWatch:
    """Следит за XHR/fetch-запросами страницы: пока они идут, данные ещё не пришли.

    Некоторые сайты держат «вечный» запрос (long-polling), из-за него страница никогда не
    становится «тихой», и старое ожидание networkidle каждый раз упиралось в таймаут 10 секунд.
    Такие запросы бот запоминает по адресу и больше не ждёт: это запрос, который всё ещё висит,
    когда карточки уже на экране, или который дожил до конца ожидания.
    """

    def __init__(self, page: Page) -> None:
        self.pending: dict = {}  # запрос -> когда начался
        self.persistent: set[str] = set()  # адреса «вечных» запросов
        page.on("request", self._started)
        page.on("requestfinished", self._done)
        page.on("requestfailed", self._done)

    @staticmethod
    def _path(request) -> str:
        return urlparse(request.url).path

    def _started(self, request) -> None:
        if request.resource_type in ("xhr", "fetch"):
            self.pending[request] = time.monotonic()

    def _done(self, request) -> None:
        self.pending.pop(request, None)

    def busy(self) -> bool:
        """Идёт ли запрос, результата которого стоит дождаться."""
        return any(self._path(r) not in self.persistent for r in self.pending)

    def learn(self, min_age: float) -> None:
        """Запросы, которые висят дольше min_age секунд, считаем «вечными»."""
        now = time.monotonic()
        for request, started in list(self.pending.items()):
            if now - started >= min_age:
                self.persistent.add(self._path(request))


async def wait_for_content(page: Page, watch: "NetWatch | None" = None, max_wait: float = 6.0) -> None:
    """Ждёт, пока страница прорисуется, и возвращается сразу, как только она устоялась.

    * Кнопки «Взять в работу» появились и 0.35 с не меняются — готово.
    * Кнопок нет: ждём, пока не закончатся запросы к API (кроме «вечных») и страница не
      перестанет меняться (нужно минимум ~1 с), — тогда список действительно пуст.
    Вместо фиксированных 10–11 секунд обычно уходит 0.5–2 секунды.
    """
    started = time.monotonic()
    last = None
    changed_at = started
    while True:
        now = time.monotonic()
        try:
            sig = tuple(await page.evaluate(SIGNATURE_JS, CARD_LABEL))
        except Exception:  # страница как раз перезагружается
            sig = None
        if sig != last:
            last, changed_at = sig, now
        if sig is not None:
            buttons, length, loading = sig
            quiet = now - changed_at
            if buttons and quiet >= 0.35:
                if watch is not None:
                    watch.learn(min_age=1.0)  # карточки уже есть, а запрос всё ещё висит — он «вечный»
                return
            if (
                not buttons
                and length
                and not loading
                and quiet >= 0.8
                and now - started >= 1.0
                and not (watch is not None and watch.busy())
            ):
                return
        if now - started >= max_wait:
            if watch is not None:
                watch.learn(min_age=0.0)  # дождались таймаута — всё, что висит, считаем «вечным»
            return
        await page.wait_for_timeout(100)


async def is_login_page(page: Page, text: str | None = None) -> bool:
    if LOGIN_URL_RE.search(page.url):
        return True
    password_selectors = _with_override(config.SEL_PASSWORD, PASSWORD_SELECTORS)
    if await first_visible(page, password_selectors) is not None:
        return True
    if text is None:
        text = await page_text(page)
    return bool(LOGIN_TEXT_RE.search(text[:5000]))


def find_error(text: str) -> str:
    for line in text.splitlines():
        line = line.strip()
        if line and len(line) <= 200 and ERROR_TEXT_RE.search(line):
            return line
    return ""


# --- Сессия сайта -------------------------------------------------------------


class SiteSession:
    """Один постоянно работающий браузер с сессией сайта."""

    def __init__(self) -> None:
        self.pw: Playwright | None = None
        self.browser: Browser | None = None
        self.context: BrowserContext | None = None
        self.page: Page | None = None
        self.lock = asyncio.Lock()
        self.user_agent = ""
        self._last_save = 0.0
        self._scanned_at = 0.0  # когда страница в последний раз читалась целиком
        self._watch: NetWatch | None = None
        self.fast = FastLane()
        self._watch_page: Page | None = None

    @property
    def has_session(self) -> bool:
        return self.context is not None and self.page is not None

    @property
    def browser_ready(self) -> bool:
        return self.browser is not None and self.browser.is_connected()

    # -- запуск / остановка --

    async def start(self) -> None:
        config.DATA_DIR.mkdir(parents=True, exist_ok=True)
        self.pw = await async_playwright().start()
        launch_kwargs = dict(
            headless=True,
            args=[
                "--no-sandbox",
                "--disable-dev-shm-usage",
                "--disable-blink-features=AutomationControlled",
            ],
        )
        if config.BROWSER_CHANNEL:
            launch_kwargs["channel"] = config.BROWSER_CHANNEL
        self.browser = await self.pw.chromium.launch(**launch_kwargs)
        self.user_agent = self._resolve_user_agent()

        self.context = None
        self.page = None
        if config.AUTH_FILE.exists():
            try:
                self.context = await self.new_context(config.AUTH_FILE, restore=True)
                self.page = await self.context.new_page()
                log.info("Сессия восстановлена из %s", config.AUTH_FILE)
            except Exception:
                log.exception("Файл сессии повреждён, нужен новый вход")
                self.context = None
                self.page = None

    def _resolve_user_agent(self) -> str:
        """User-Agent закрепляется навсегда: сайт видит один и тот же браузер."""
        if config.USER_AGENT:
            return config.USER_AGENT
        saved = read_json(config.PROFILE_FILE, {}) or {}
        if saved.get("user_agent"):
            return saved["user_agent"]
        major = (self.browser.version if self.browser else "131").split(".")[0]
        agent = (
            "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
            f"(KHTML, like Gecko) Chrome/{major}.0.0.0 Safari/537.36"
        )
        write_json(config.PROFILE_FILE, {"user_agent": agent})
        return agent

    async def new_context(
        self, storage_path=None, restore: bool = False
    ) -> BrowserContext:
        assert self.browser is not None
        kwargs = dict(
            user_agent=self.user_agent,
            viewport={"width": 1440, "height": 1000},
            locale=config.LOCALE,
            timezone_id=config.TIMEZONE,
        )
        if storage_path:
            kwargs["storage_state"] = str(storage_path)
        context = await self.browser.new_context(**kwargs)
        context.set_default_timeout(30000)
        if restore:
            data = read_json(config.SESSION_STORAGE_FILE, {}) or {}
            if data:
                script = RESTORE_JS.replace(
                    "__ORIGIN__", json.dumps(config.SITE_ORIGIN)
                ).replace("__DATA__", json.dumps(data))
                await context.add_init_script(script)
        return context

    async def _close_all(self) -> None:
        self.fast.reset()
        for obj in (self.context, self.browser):
            if obj is not None:
                try:
                    await obj.close()
                except Exception:
                    pass
        if self.pw is not None:
            try:
                await self.pw.stop()
            except Exception:
                pass
        self.context = self.page = self.browser = self.pw = None

    async def close(self) -> None:
        async with self.lock:
            await self._close_all()

    async def restart(self) -> None:
        async with self.lock:
            await self._close_all()
            await self.start()

    # -- сохранение сессии --

    async def save_state(self) -> None:
        if self.context is None:
            return
        state = await self.context.storage_state()
        write_json(config.AUTH_FILE, state)
        try:
            if self.page is not None and not self.page.is_closed():
                data = await self.page.evaluate(
                    "() => Object.fromEntries(Object.entries(window.sessionStorage))"
                )
                if data:
                    write_json(config.SESSION_STORAGE_FILE, data)
        except Exception:
            pass
        self._last_save = time.monotonic()

    async def adopt(self, context: BrowserContext, page: Page) -> None:
        """Делает свежезалогиненный контекст основным и сохраняет сессию."""
        async with self.lock:
            old = self.context
            self.context, self.page = context, page
            await self.save_state()
        if old is not None:
            try:
                await old.close()
            except Exception:
                pass

    async def logout(self) -> None:
        async with self.lock:
            self.fast.reset()
            if self.context is not None:
                try:
                    await self.context.close()
                except Exception:
                    pass
            self.context = None
            self.page = None
            for path in (config.AUTH_FILE, config.SESSION_STORAGE_FILE):
                try:
                    path.unlink()
                except FileNotFoundError:
                    pass

    # -- работа со страницей --

    def _net(self, page: Page) -> NetWatch:
        if self._watch is None or self._watch_page is not page:
            self._watch, self._watch_page = NetWatch(page), page
            if FAST_ON:  # какими адресами пользуется сама страница: список заявок и WebSocket
                page.on("request", lambda request: self.fast.note_request(request.url))
                page.on("websocket", lambda ws: self.fast.note_websocket(ws.url))
        return self._watch

    async def scan(self) -> ScanResult:
        async with self.lock:
            if self.context is None or self.page is None:
                return ScanResult("no_session", message="Сессии нет — нужен вход.")

            page = self.page
            watch = self._net(page)
            await page.goto(config.URL, wait_until="domcontentloaded", timeout=60000)
            await wait_for_content(page, watch)

            title = await page.title()
            url = page.url
            text = await page_text(page)

            if await is_login_page(page, text):
                self.fast.ready = False
                return ScanResult(
                    "need_login",
                    title=title,
                    url=url,
                    message="Сайт требует авторизации (сессия слетела).",
                )

            raw = await self._extract(page)
            cards = [item["text"] for item in raw]
            self._scanned_at = time.monotonic()

            if time.monotonic() - self._last_save > config.STATE_SAVE_EVERY:
                await self.save_state()

            return ScanResult("ok", requests=cards, title=title, url=url)

    async def screenshot(self) -> bytes | None:
        async with self.lock:
            if self.page is None or self.page.is_closed():
                return None
            return await self.page.screenshot(type="png")

    # -- «Взять в работу» на сайте --

    async def _extract(self, page: Page) -> list[dict]:
        raw = await page.evaluate(EXTRACT_JS, CARD_LABEL)
        for item in raw:
            item["text"] = re.sub(r"\n{3,}", "\n\n", item["text"]).strip()
        return raw[:200]

    async def _wait_for_cards(self, page: Page) -> None:
        """Ждём, пока страница прорисуется (быстро, без ожидания «тишины в сети»)."""
        await wait_for_content(page, self._net(page), max_wait=5.0)

    async def _find(self, page: Page, key: str) -> tuple[int | None, int]:
        """(номер кнопки нужной заявки, сколько таких заявок сейчас на странице)."""
        raw = await self._extract(page)
        keys = keys_for([parse_card(item["text"]) for item in raw])
        base = key.split("-")[0]
        same = sum(1 for k in keys if k.split("-")[0] == base)
        for item_key, item in zip(keys, raw):
            if item_key == key:
                return item["idx"], same
        return None, same

    async def _count(self, page: Page, base: str) -> int | None:
        """Сколько заявок этого вида на странице. None — страница сейчас перезагружается."""
        try:
            raw = await self._extract(page)
        except Exception:
            return None
        keys = keys_for([parse_card(item["text"]) for item in raw])
        return sum(1 for k in keys if k.split("-")[0] == base)

    async def _site_error(self, page: Page, native: list[str]) -> str:
        """Текст ошибки, которую сайт показал после нажатия (alert или всплывашка)."""
        for text in native:
            if ERROR_HINT_RE.search(text) or not SUCCESS_HINT_RE.search(text):
                return text.strip()[:300]
        try:
            found = await page.evaluate("() => window.__gzWatch ? window.__gzWatch.found.slice() : []")
        except Exception:
            return ""
        for item in found:
            text = item["text"]
            if ERROR_HINT_RE.search(text) or (item["flagged"] and not SUCCESS_HINT_RE.search(text)):
                return text
        return ""

    @staticmethod
    def _on_target(page: Page) -> bool:
        if page.is_closed():
            return False
        return urlparse(page.url).path.rstrip("/") == urlparse(config.URL).path.rstrip("/")

    async def _shot(self, page: Page) -> bytes | None:
        try:
            return await page.screenshot(type="png")
        except Exception:
            return None

    async def _confirm_dialog(self, page: Page) -> None:
        """Если сайт спросил подтверждение в модальном окне — подтверждаем."""
        try:
            modals = page.locator(MODAL_SELECTOR)
            for i in range(min(await modals.count(), 10)):
                modal = modals.nth(i)
                if not await modal.is_visible():
                    continue
                buttons = modal.locator('button, [role="button"], a').filter(has_text=CONFIRM_RE)
                for j in range(min(await buttons.count(), 5)):
                    button = buttons.nth(j)
                    if await button.is_visible() and await button.is_enabled():
                        await button.click(timeout=5000)
                        await page.wait_for_timeout(600)
                        return
        except Exception:
            log.debug("Окно подтверждения не обработано", exc_info=True)

    async def take(self, key: str) -> TakeResult:
        """Нажимает «Взять в работу».

        Сначала пробует API сайта (один запрос, без замка страницы и без клика). Если это невозможно,
        нажимает кнопку на странице: успех — заявка пропала из списка на сайте, не взялась —
        возвращаем ошибку, которую показал сайт (или «заявка осталась в списке»).
        """
        if FAST_ON:
            try:
                outcome = await self.fast.take(key)
            except Exception:
                log.exception("Быстрое взятие не удалось, беру кнопкой")
                outcome = None
            if outcome is not None:
                status, message, site_error = outcome
                self._scanned_at = 0.0  # страница на экране уже устарела
                if status == "ok":
                    return TakeResult("ok")
                return TakeResult(status, message, None, site_error=site_error)
        async with self.lock:
            if self.context is None or self.page is None:
                return TakeResult("no_session", "Сессии нет — нужен вход.")
            page = self.page
            native: list[str] = []  # тексты системных alert() сайта

            def on_dialog(dialog):
                if dialog.type == "alert":
                    native.append(dialog.message)
                asyncio.ensure_future(dialog.accept())  # confirm() принимаем

            page.on("dialog", on_dialog)
            try:
                return await self._take(page, key, native)
            finally:
                page.remove_listener("dialog", on_dialog)

    async def _take(self, page: Page, key: str, native: list[str]) -> TakeResult:
        idx, before = None, 0
        # Быстрый путь без перезагрузки — только если страницу читали секунду назад.
        # Иначе заявку могли уже забрать, а на странице она ещё висит.
        if self._on_target(page) and time.monotonic() - self._scanned_at < FRESH_PAGE_SECONDS:
            idx, before = await self._find(page, key)
        if idx is None:
            await page.goto(config.URL, wait_until="domcontentloaded", timeout=60000)
            await self._wait_for_cards(page)
            if await is_login_page(page):
                return TakeResult("need_login", "Сессия на сайте слетела.")
            idx, before = await self._find(page, key)
        if idx is None:
            return TakeResult("gone", "Заявки уже нет в списке на сайте.")

        base = key.split("-")[0]
        try:
            await page.evaluate(WATCH_JS)
        except Exception:
            log.debug("Не удалось включить слежение за сообщениями сайта", exc_info=True)

        button = page.locator(f'[data-gzbot="{idx}"]').first
        try:
            await button.scroll_into_view_if_needed(timeout=5000)
            await button.click(timeout=10000)
        except Exception:
            log.exception("Не удалось нажать «%s»", CARD_LABEL)
            return TakeResult("failed", "Не получилось нажать кнопку на сайте.", await self._shot(page))

        await page.wait_for_timeout(250)
        await self._confirm_dialog(page)

        started = time.monotonic()
        error, error_at, gone_at = "", 0.0, None
        while True:
            now = time.monotonic()
            if not error:
                error = await self._site_error(page, native)
                if error:
                    error_at = now
            if LOGIN_URL_RE.search(page.url):
                return TakeResult("need_login", "После нажатия сайт запросил вход.")

            left = await self._count(page, base)
            if left is not None and left < before:
                # Заявка пропала. Чуть меньше секунды ждём, что сайт не вернёт её обратно (откат).
                if gone_at is None:
                    gone_at = now
                if now - gone_at >= 0.8:
                    if await is_login_page(page):
                        return TakeResult("need_login", "После нажатия сайт запросил вход.")
                    self._scanned_at = time.monotonic()
                    await self.save_state()
                    return TakeResult("ok")
            else:
                gone_at = None
                if error and now - error_at >= 1.5:
                    break  # сайт показал ошибку, заявка на месте
            if now - started >= TAKE_WAIT_SECONDS:
                break
            await page.wait_for_timeout(250)

        # Страница могла не обновиться сама — перезагружаем и смотрим ещё раз.
        shot = await self._shot(page)
        await page.goto(config.URL, wait_until="domcontentloaded", timeout=60000)
        await self._wait_for_cards(page)
        self._scanned_at = time.monotonic()
        if await is_login_page(page):
            return TakeResult("need_login", "После нажатия сайт запросил вход.", shot)
        left = await self._count(page, base)
        if left is not None and left < before:
            await self.save_state()
            return TakeResult("ok")
        if error:
            return TakeResult("failed", error, shot, site_error=True)
        return TakeResult("failed", "Сайт не взял заявку: она осталась в списке.", shot)


# --- Вход через бота ------------------------------------------------------------


class LoginFlow:
    """Пошаговый вход: логин+пароль -> (код 2FA) -> сессия сохранена."""

    def __init__(self, session: SiteSession) -> None:
        self.session = session
        self.context: BrowserContext | None = None
        self.page: Page | None = None
        self.closed = False
        self.created = time.monotonic()
        self.code_attempts = 0
        self._watchdog: asyncio.Task | None = None

    @property
    def expired(self) -> bool:
        return self.closed or time.monotonic() - self.created > config.LOGIN_TIMEOUT

    async def _expire_later(self) -> None:
        try:
            await asyncio.sleep(config.LOGIN_TIMEOUT)
            await self.close()
        except asyncio.CancelledError:
            pass

    async def close(self) -> None:
        self.closed = True
        if self._watchdog and self._watchdog is not asyncio.current_task():
            self._watchdog.cancel()
        context, self.context, self.page = self.context, None, None
        if context is not None:
            try:
                await context.close()
            except Exception:
                pass

    async def screenshot(self) -> bytes | None:
        if self.page is None or self.page.is_closed():
            return None
        try:
            return await self.page.screenshot(type="png")
        except Exception:
            return None

    # -- шаги --

    async def start(self, username: str, password: str) -> str:
        """Возвращает "ok" (вошли) или "2fa" (нужен код). Иначе LoginError."""
        if not self.session.browser_ready:
            raise LoginError("Браузер не запущен. Перезапусти приложение в Amvera.")

        # Чистый контекст: старые (протухшие) cookies не мешают.
        self.context = await self.session.new_context()
        self.page = await self.context.new_page()
        self._watchdog = asyncio.create_task(self._expire_later())
        page = self.page

        try:
            await page.goto(
                config.LOGIN_URL or config.URL,
                wait_until="domcontentloaded",
                timeout=60000,
            )
        except PlaywrightTimeoutError:
            raise LoginError("Сайт не открылся за 60 секунд.")
        await settle(page)

        user_sel = _with_override(config.SEL_LOGIN, USER_SELECTORS)
        pass_sel = _with_override(config.SEL_PASSWORD, PASSWORD_SELECTORS)

        async def find_form():
            found = await first_visible(page, pass_sel)
            return found or await first_visible(page, user_sel)

        if await self._wait_for(find_form, 20) is None:
            # Возможно, это лендинг — пробуем нажать «Войти».
            link = await first_visible(page, ENTER_LINK_SELECTORS)
            if link is not None:
                try:
                    await link.click()
                    await settle(page, 5000, 800)
                except Exception:
                    pass
            if await self._wait_for(find_form, 10) is None:
                raise LoginError(
                    "Не нашёл форму входа на странице "
                    f"{page.url}. Задай LOGIN_URL или селекторы SEL_* в переменных."
                )

        user_field = await first_visible(page, user_sel)
        pass_field = await first_visible(page, pass_sel)

        if user_field is not None:
            await self._fill(user_field, username)

        if pass_field is None:
            # Двухшаговая форма: сначала логин, потом пароль.
            await self._submit()
            pass_field = await self._wait_for(
                lambda: first_visible(page, pass_sel), 12
            )
            if pass_field is None:
                err = find_error(await page_text(page))
                raise LoginError(err or "Поле пароля не появилось после ввода логина.")

        await self._fill(pass_field, password)
        await self._submit()

        kind, message = await self._wait_outcome()
        if kind == "2fa":
            return "2fa"
        if kind == "ok":
            await self._finalize()
            return "ok"
        raise LoginError(message)

    async def submit_code(self, code: str) -> None:
        """Вводит код 2FA. При успехе сохраняет сессию, иначе LoginError."""
        page = self.page
        if page is None or self.expired:
            raise LoginError("Время ожидания кода вышло.")
        self.code_attempts += 1
        can_retry = self.code_attempts < 3

        field = await self._find_otp()
        if field is None:
            field = await first_visible(page, [ANY_INPUT_SELECTOR])
        if field is None:
            raise LoginError("Не нашёл поле для кода 2FA. Задай SEL_OTP.")

        # Сегментированный ввод (по цифре в ячейке) — печатаем с клавиатуры.
        segments = page.locator('input[maxlength="1"]:visible')
        segmented = not config.SEL_OTP and await segments.count() >= 4
        if segmented:
            await segments.first.click()
            await page.keyboard.type(code, delay=80)
        else:
            await self._fill(field, code)

        button = await first_visible(page, SUBMIT_SELECTORS, enabled=True)
        if button is not None:
            try:
                await button.click(timeout=5000)
            except Exception:
                pass
        elif not segmented:
            await page.keyboard.press("Enter")

        kind, message = await self._wait_outcome(after_otp=True)
        if kind == "ok":
            await self._finalize()
            return
        raise LoginError(message, retry=can_retry)

    # -- внутреннее --

    async def _wait_for(self, finder, timeout: float):
        assert self.page is not None
        deadline = time.monotonic() + timeout
        while True:
            result = await finder()
            if result is not None:
                return result
            if time.monotonic() >= deadline:
                return None
            await self.page.wait_for_timeout(400)

    async def _fill(self, locator: Locator, value: str) -> None:
        await locator.click()
        await locator.fill(value)
        try:
            current = await locator.input_value()
        except Exception:
            current = value
        if current != value:  # маски/React иногда сбрасывают значение
            await locator.fill("")
            await locator.press_sequentially(value, delay=30)

    async def _submit(self) -> None:
        assert self.page is not None
        selectors = _with_override(config.SEL_SUBMIT, SUBMIT_SELECTORS)
        button = await first_visible(self.page, selectors, enabled=True)
        if button is not None:
            try:
                await button.click(timeout=5000)
                return
            except Exception:
                pass
        await self.page.keyboard.press("Enter")

    async def _find_otp(self) -> Locator | None:
        assert self.page is not None
        pass_sel = _with_override(config.SEL_PASSWORD, PASSWORD_SELECTORS)
        if not config.SEL_OTP and await first_visible(self.page, pass_sel):
            return None  # ещё на шаге пароля
        return await first_visible(
            self.page, _with_override(config.SEL_OTP, OTP_SELECTORS)
        )

    async def _wait_outcome(
        self, after_otp: bool = False, timeout: float = 25.0
    ) -> tuple[str, str]:
        """Ждёт результат отправки формы: ok / 2fa / error / unknown."""
        page = self.page
        assert page is not None
        pass_sel = _with_override(config.SEL_PASSWORD, PASSWORD_SELECTORS)
        started = time.monotonic()
        await page.wait_for_timeout(1200)

        while True:
            text = await page_text(page)
            otp = await self._find_otp()
            login_page = await is_login_page(page, text)

            if not login_page and otp is None:
                return "ok", ""

            error = find_error(text)
            elapsed = time.monotonic() - started
            if after_otp:
                if error and elapsed > 2:
                    return "error", error
            else:
                password_visible = await first_visible(page, pass_sel) is not None
                if otp is not None or (
                    TWO_FA_TEXT_RE.search(text) and not password_visible
                ):
                    return "2fa", ""
                if error and elapsed > 2.5 and password_visible:
                    return "error", error

            if elapsed >= timeout:
                break
            await page.wait_for_timeout(500)

        if after_otp:
            return "error", "Сайт не принял код или не ответил."
        return (
            "unknown",
            "Страница не изменилась после отправки формы "
            "(возможна капча или нестандартная форма).",
        )

    async def _finalize(self) -> None:
        page = self.page
        assert page is not None and self.context is not None
        await page.goto(config.URL, wait_until="domcontentloaded", timeout=60000)
        await settle(page)
        if await is_login_page(page):
            raise LoginError("После входа сайт снова показывает форму входа.")
        await self.session.adopt(self.context, page)
        # Контекст теперь принадлежит SiteSession — не закрываем его здесь.
        self.context = None
        self.page = None
        self.closed = True
        if self._watchdog and self._watchdog is not asyncio.current_task():
            self._watchdog.cancel()
