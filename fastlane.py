"""Быстрая линия: прямой доступ к API сайта вместо перезагрузки страницы и клика по кнопке.

Как устроен сайт (по его публичному коду) и что делает бот
---------------------------------------------------------
* Список заявок: ``GET {API}/account/payouts/available`` -> ``{items: [...], currentPage, totalPages}``.
* «Взять в работу»: ``PATCH {API}/account/payouts/{id}/assign``. Ответ 2xx — заявка за вами.
  Ошибки приходят как ``{"message": "..."}``.
* Новые заявки сайт присылает по WebSocket (``wss://ws-trader.<домен>?Authorization=<токен>``,
  канал ``@traders/available_payouts``, события ``ADD`` с полной заявкой и ``REMOVE``).
* Авторизация: ``Authorization: Bearer <idToken>`` (AWS Cognito). Токен лежит в localStorage
  браузера, в котором бот уже вошёл на сайт, и сайт сам его обновляет.

Бот делает то же самое, но без отрисовки страницы: держит в браузере отдельную лёгкую вкладку
(пустая заглушка на домене сайта), из неё слушает WebSocket и отправляет запросы. Это те же cookie,
тот же браузер и тот же Origin, что у самого сайта, поэтому для сервера это обычный клиент.
Токен бот берёт из localStorage и при необходимости обновляет тем же способом, что и сайт
(refresh-токен Cognito).

Если что-то не сходится (токен не найден, сайт ответил отказом, формат изменился), быстрая линия
выключается, а бот продолжает работать обычным способом: страница и клик по кнопке.
"""
import asyncio
import base64
import json
import logging
import re
import time
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation
from urllib.parse import parse_qsl, quote, urlencode, urlparse, urlunparse

import config
from cards import Card, base_key

log = logging.getLogger("fastlane")

FAST_ON = bool(getattr(config, "FAST_LANE", True))
LIST_PATH = "/account/payouts/available"
ASSIGN_PATH = "/account/payouts/{id}/assign"
CHANNEL = "@traders/available_payouts"
STUB_PATH = "/__gz_api__"
MAX_PAGES = 10
RECENT_SECONDS = 6.0  # столько секунд заявка, пришедшая по push, считается «уже в списке»

# Известные тексты ошибок сайта -> по-русски. Второй элемент: заявку уже забрали (gone).
KNOWN_ERRORS = {
    "payout is already assigned": ("заявку уже взял другой трейдер", True),
    "payout assignment is not allowed for blocked trader": ("ваш аккаунт заблокирован для взятия заявок", False),
    "trader is not connected to payout merchant": ("ваш аккаунт не подключён к мерчанту этой заявки", False),
    "amount is over the limit": ("сумма заявки превышает ваш лимит", False),
    "weekly usdt payout volume cannot exceed deposit volume": (
        "недельный объём выплат не может превышать объём пополнений",
        False,
    ),
}

# --- Код внутри браузера -------------------------------------------------------------------------

# Запрос из страницы. credentials: 'omit' — как у самого сайта (cookie на другой домен не шлём).
FETCH_JS = r"""
async (a) => {
  const ctrl = new AbortController();
  const timer = setTimeout(() => ctrl.abort(), a.timeout || 5000);
  try {
    const init = { method: a.method, headers: a.headers || {}, credentials: 'omit', signal: ctrl.signal };
    if (a.body) init.body = a.body;
    const res = await fetch(a.url, init);
    const text = await res.text();
    return { status: res.status, text: text.length > 2000000 ? '' : text };
  } catch (e) {
    return { status: 0, text: '', error: String(e) };
  } finally { clearTimeout(timer); }
}
"""

# Весь список заявок: первая страница, затем остальные страницы параллельно.
LIST_JS = r"""
async (a) => {
  const get = async (url) => {
    const ctrl = new AbortController();
    const timer = setTimeout(() => ctrl.abort(), a.timeout || 4000);
    try {
      const res = await fetch(url, { method: 'GET', headers: a.headers, credentials: 'omit', signal: ctrl.signal });
      return { status: res.status, text: await res.text() };
    } catch (e) { return { status: 0, text: '', error: String(e) }; }
    finally { clearTimeout(timer); }
  };
  const first = await get(a.url);
  if (first.status !== 200) return { status: first.status, text: first.text, error: first.error, pages: [] };
  let data;
  try { data = JSON.parse(first.text); } catch (e) { return { status: 200, bad: true, text: first.text.slice(0, 300), pages: [] }; }
  const pages = [data.items || []];
  const rawTotal = Number(data.totalPages) || 1;
  const total = Math.min(rawTotal, a.maxPages);
  if (total > 1) {
    const urls = [];
    for (let n = 2; n <= total; n++) { const u = new URL(a.url); u.searchParams.set('page', String(n)); urls.push(u.toString()); }
    const rest = await Promise.all(urls.map(get));
    for (const r of rest) {
      if (r.status !== 200) return { status: r.status, text: r.text, error: r.error, pages: [] };
      try { pages.push(JSON.parse(r.text).items || []); } catch (e) { return { status: 200, bad: true, text: r.text.slice(0, 300), pages: [] }; }
    }
  }
  return { status: 200, pages: pages, total: total, rawTotal: rawTotal };
}
"""

READ_TOKENS_JS = r"""
() => {
  const ls = window.localStorage;
  let prefix = null, user = null;
  for (let i = 0; i < ls.length; i++) {
    const k = ls.key(i);
    const m = k && k.match(/^(CognitoIdentityServiceProvider\.[^.]+)\.LastAuthUser$/);
    if (m) { prefix = m[1]; user = ls.getItem(k); break; }
  }
  if (!prefix || !user) return null;
  const g = (n) => ls.getItem(prefix + '.' + user + '.' + n);
  return { prefix: prefix, user: user, idToken: g('idToken'), accessToken: g('accessToken'), refreshToken: g('refreshToken') };
}
"""

WRITE_TOKENS_JS = r"""
(a) => {
  const ls = window.localStorage, base = a.prefix + '.' + a.user + '.';
  if (a.idToken) ls.setItem(base + 'idToken', a.idToken);
  if (a.accessToken) ls.setItem(base + 'accessToken', a.accessToken);
}
"""

# Обновление токена тем же способом, что и сам сайт (Amplify): refresh-токен Cognito.
REFRESH_JS = r"""
async (a) => {
  try {
    const res = await fetch(a.url, {
      method: 'POST',
      headers: { 'Content-Type': 'application/x-amz-json-1.1', 'X-Amz-Target': 'AWSCognitoIdentityProviderService.InitiateAuth' },
      body: JSON.stringify({ AuthFlow: 'REFRESH_TOKEN_AUTH', ClientId: a.clientId, AuthParameters: { REFRESH_TOKEN: a.refresh } }),
      credentials: 'omit',
    });
    return { status: res.status, text: await res.text() };
  } catch (e) { return { status: 0, text: '', error: String(e) }; }
}
"""

# WebSocket сайта: подписка на канал новых заявок. Токен берётся у бота при каждом (пере)подключении.
WS_JS = r"""
(cfg) => {
  if (window.__gzWs) window.__gzWs.stop();
  const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
  let stopped = false, sock = null;
  const state = (window.__gzWs = { stop: () => { stopped = true; try { sock && sock.close(); } catch (e) {} } });
  (async () => {
    while (!stopped) {
      let token = null;
      try { token = await window.__gzToken(); } catch (e) {}
      if (!token) { await sleep(2000); continue; }
      await new Promise((resolve) => {
        let done = false, ping = null;
        const finish = () => { if (done) return; done = true; clearInterval(ping); try { window.__gzWsEvent({ type: 'closed' }); } catch (e) {} resolve(); };
        try { sock = new WebSocket(cfg.url + (cfg.url.includes('?') ? '&' : '?') + 'Authorization=' + token); } catch (e) { finish(); return; }
        sock.onopen = () => {
          sock.send(JSON.stringify({ action: 'subscribe', channel: cfg.channel }));
          ping = setInterval(() => { try { sock.send(JSON.stringify({ action: 'ping', channel: 'TEST' })); } catch (e) {} }, 240000);
        };
        sock.onmessage = (e) => {
          let m; try { m = JSON.parse(e.data); } catch (x) { return; }
          if (m.channel !== cfg.channel) return;
          if (m.action === 'ack') window.__gzWsEvent({ type: 'ack', status: m.status });
          else if (m.action === 'message') window.__gzWsEvent({ type: 'message', payload: m.payload });
        };
        sock.onclose = finish;
        sock.onerror = finish;
      });
      if (!stopped) await sleep(1500);
    }
  })();
}
"""

# --- Вспомогательное -------------------------------------------------------------------------------


def jwt_payload(token: str) -> dict:
    try:
        part = token.split(".")[1]
        part += "=" * (-len(part) % 4)
        data = json.loads(base64.urlsafe_b64decode(part.encode()))
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def card_from_item(item: dict) -> Card | None:
    """Заявка из API -> карточка (те же поля, что на странице)."""
    try:
        out = Decimal(str(item["outAmount"]))
        q = out.quantize(Decimal("0.01"))
        whole = int(out.quantize(Decimal("1"), rounding=ROUND_HALF_UP))
    except (KeyError, InvalidOperation, ValueError):
        return None
    cred = item.get("credentials") if isinstance(item.get("credentials"), dict) else {}
    requisite = cred.get("phone") or cred.get("phoneNumber") or cred.get("cardNumber") or ""
    cur = str(item.get("outCurrency") or "").strip()
    if cur.upper() in ("RUB", "RUR", "₽"):
        cur = "₽"
    return Card(
        amount=whole,
        amount_key=str(q),
        currency=cur,
        bank=str(cred.get("bank") or "").strip(),
        requisite=str(requisite).strip(),
        extras=[],
        lines=[],
    )


def error_text(text: str) -> tuple[str, bool]:
    """Текст ошибки сайта по-русски и признак «заявку уже забрали»."""
    raw = (text or "").strip()
    message = ""
    if raw and not raw.startswith("<"):
        try:
            data = json.loads(raw)
            if isinstance(data, dict):
                message = str(data.get("message") or data.get("error") or "")
            elif isinstance(data, str):
                message = data
        except ValueError:
            message = raw[:200]
    known = KNOWN_ERRORS.get(message.strip().lower())
    if known:
        return known[0], known[1]
    return message.strip()[:200], "already" in message.lower() and "assign" in message.lower()


def _strip_page(url: str) -> str:
    parts = urlparse(url)
    query = [(k, v) for k, v in parse_qsl(parts.query, keep_blank_values=True) if k not in ("page", "currentPage")]
    return urlunparse(parts._replace(query=urlencode(query)))


# --- Быстрая линия -------------------------------------------------------------------------------


class FastLane:
    """Состояние и действия быстрой линии. Не использует страницу, которую сканирует бот,
    поэтому не мешает обычной проверке и не ждёт её."""

    def __init__(self) -> None:
        self.context = None
        self.page = None
        self.attached = False
        self.ready = False
        self.reason = "ещё не запускалась"
        self.error = ""
        self.disabled_note = ""
        self.base = config.API_BASE
        self.ws_url = config.WS_URL
        self.list_url = ""  # адрес списка, подсмотренный у самой страницы (с её параметрами)
        self.ws_open = False
        self.last_ms = 0.0
        self.last_at = 0.0
        self.last_keys: set[str] | None = None
        self.complete = True  # список получен целиком (страниц не больше MAX_PAGES)
        self.ids: dict[str, str] = {}  # ключ заявки -> id в API
        self.id2key: dict[str, str] = {}
        self.known_ids: set[str] = set()
        self.recent: dict[str, tuple[str, Card, float]] = {}  # push: id -> (ключ, карточка, когда)
        self.backoff_until = 0.0
        self.cooldown_until = 0.0
        self.mismatches = 0
        self.on_new = None  # вызывается с [(ключ, карточка)] при push-событии ADD
        self._token = ""
        self._token_exp = 0.0
        self._tlock = asyncio.Lock()

    # -- подключение --

    def reset(self) -> None:
        """Контекст браузера закрыт или заменён: страница быстрой линии больше недоступна."""
        self.attached = False
        self.ready = False
        self.page = None
        self.context = None
        self.ws_open = False
        self._token = ""
        self._token_exp = 0.0

    async def attach(self, context) -> None:
        self.reset()
        self.context = context
        page = await context.new_page()
        await page.expose_function("__gzToken", self._js_token)
        await page.expose_function("__gzWsEvent", self._js_event)
        await page.route(
            f"**{STUB_PATH}",
            lambda route: route.fulfill(status=200, content_type="text/html", body="<!doctype html><title>.</title>"),
        )
        await page.goto(config.SITE_ORIGIN + STUB_PATH, wait_until="domcontentloaded")
        self.page = page
        self.attached = True
        self.reason = "подключается"
        await self._start_ws()
        log.info("Быстрая линия: подключена (API %s, WebSocket %s)", self.base, self.ws_url or "—")

    async def _start_ws(self) -> None:
        if self.page is None or not self.ws_url:
            return
        try:
            await self.page.evaluate(WS_JS, {"url": self.ws_url, "channel": CHANNEL})
        except Exception:
            log.exception("Быстрая линия: не удалось запустить WebSocket")

    def note_request(self, url: str) -> None:
        """Запрос самой страницы сайта: запоминаем адрес списка (там могут быть свои параметры)."""
        if not url:
            return
        parts = urlparse(url)
        if parts.path.rstrip("/").endswith(LIST_PATH):
            cleaned = _strip_page(url)
            if cleaned != self.list_url:
                self.list_url = cleaned
                self.base = f"{parts.scheme}://{parts.netloc}"

    def note_websocket(self, url: str) -> None:
        """WebSocket самой страницы сайта: берём его адрес без токена."""
        parts = urlparse(url or "")
        if parts.scheme not in ("ws", "wss") or not parts.netloc:
            return
        clean = f"{parts.scheme}://{parts.netloc}{parts.path if parts.path not in ('', '/') else ''}"
        if clean != self.ws_url:
            self.ws_url = clean
            if self.attached:
                asyncio.ensure_future(self._start_ws())

    # -- токен --

    async def _js_token(self) -> str | None:
        return await self.token()

    async def token(self, force: bool = False) -> str | None:
        """Свежий idToken из localStorage; если скоро истечёт, обновляется через refresh-токен."""
        async with self._tlock:
            if self.page is None or self.page.is_closed():
                return None
            now = time.time()
            if not force and self._token and self._token_exp - now > 90:
                return self._token
            try:
                info = await self.page.evaluate(READ_TOKENS_JS)
            except Exception:
                return None
            if not info or not info.get("idToken"):
                self.error = "в браузере нет токена сайта (нужен вход)"
                return None
            token = info["idToken"]
            payload = jwt_payload(token)
            exp = float(payload.get("exp") or 0)
            if exp - now > 90 and not force:
                self._token, self._token_exp = token, exp
                return token
            refreshed = await self._refresh(info, payload)
            if refreshed:
                return refreshed
            if exp - now > 5:  # обновить не вышло, но этот токен ещё действует
                self._token, self._token_exp = token, exp
                return token
            self.error = "токен сайта истёк и не обновился (нужен вход)"
            return None

    async def _refresh(self, info: dict, payload: dict) -> str | None:
        refresh = info.get("refreshToken")
        client_id = payload.get("aud") or payload.get("client_id") or str(info.get("prefix", "")).split(".")[-1]
        iss = str(payload.get("iss") or "")
        url = config.COGNITO_URL or (f"https://{iss.split('/')[2]}/" if iss.startswith("https://") else "")
        if not (refresh and client_id and url):
            return None
        try:
            res = await self.page.evaluate(REFRESH_JS, {"url": url, "clientId": client_id, "refresh": refresh})
            if res.get("status") != 200:
                log.warning("Быстрая линия: токен не обновился (HTTP %s)", res.get("status"))
                return None
            result = (json.loads(res["text"]).get("AuthenticationResult")) or {}
            new = result.get("IdToken")
            if not new:
                return None
            await self.page.evaluate(
                WRITE_TOKENS_JS,
                {"prefix": info["prefix"], "user": info["user"], "idToken": new, "accessToken": result.get("AccessToken")},
            )
            self._token, self._token_exp = new, float(jwt_payload(new).get("exp") or 0)
            log.info("Быстрая линия: токен сайта обновлён")
            return new
        except Exception:
            log.exception("Быстрая линия: ошибка обновления токена")
            return None

    # -- события WebSocket --

    async def _js_event(self, event: dict) -> None:
        kind = (event or {}).get("type")
        if kind == "ack":
            self.ws_open = event.get("status") == "success"
            log.info("Быстрая линия: push по WebSocket %s", "подключён" if self.ws_open else "отклонён")
        elif kind == "closed":
            if self.ws_open:
                log.warning("Быстрая линия: WebSocket отключился, переподключаюсь")
            self.ws_open = False
        elif kind == "message":
            payload = event.get("payload") or {}
            item = payload.get("item") if isinstance(payload.get("item"), dict) else None
            if item is None:
                return
            if payload.get("action") == "ADD":
                pairs = self.ingest([item])
                if pairs and self.on_new:
                    self.on_new(pairs)
            elif payload.get("action") == "REMOVE":
                pid = str(item.get("id") or "")
                key = self.id2key.pop(pid, None)
                if key:
                    self.ids.pop(key, None)
                self.known_ids.discard(pid)
                self.recent.pop(pid, None)

    def ingest(self, items: list[dict]) -> list[tuple[str, Card]]:
        """Заявки из push: возвращает только те, которых бот ещё не видел."""
        pairs = []
        now = time.monotonic()
        for item in items:
            pid = str(item.get("id") or "")
            if not pid or pid in self.known_ids:
                continue
            if item.get("status") not in (None, "AVAILABLE"):
                continue
            card = card_from_item(item)
            if card is None:
                continue
            key = self.id2key.get(pid)
            if key is None:
                base, n = base_key(card), 1
                key = base
                while key in self.ids:
                    n += 1
                    key = f"{base}-{n}"
                self.id2key[pid] = key
                self.ids[key] = pid
            self.known_ids.add(pid)
            self.recent[pid] = (key, card, now)
            pairs.append((key, card))
        return pairs

    # -- опрос списка --

    def poll_interval(self) -> float:
        return config.FAST_PUSH_POLL if self.ws_open else config.FAST_INTERVAL

    def _headers(self, token: str) -> dict:
        return {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}

    async def poll(self) -> list[tuple[str, Card]] | None:
        """Все актуальные заявки из API: [(ключ, карточка)]. None — не получилось (см. self.error)."""
        if self.page is None or self.page.is_closed():
            return None
        token = await self.token()
        if not token:
            self.ready = False
            return None
        url = self.list_url or f"{self.base}{LIST_PATH}"
        started = time.monotonic()
        res = await self._list(url, token)
        if int(res.get("status") or 0) in (401, 403):  # токен мог протухнуть раньше срока
            token = await self.token(force=True)
            if token:
                res = await self._list(url, token)
        self.last_ms = (time.monotonic() - started) * 1000
        status = int(res.get("status") or 0)

        if status in (401, 403):
            self.ready = False
            self.error = f"сайт отказал в доступе ({status})"
            self.cooldown_until = time.monotonic() + 30.0
            return None
        if status == 429:
            self.error = "сайт просит реже (429)"
            self.backoff_until = time.monotonic() + 30.0
            return None
        if status == 0 or status >= 500:
            self.error = f"сайт не ответил ({status or res.get('error', 'ошибка сети')})"
            self.backoff_until = time.monotonic() + 3.0
            return None
        if status != 200 or res.get("bad"):
            return self._shut(f"список заявок ответил {status}: {(res.get('text') or '')[:120]}", 60.0)

        rows: list[tuple[str, Card]] = []
        seen: set[str] = set()
        for page_items in res.get("pages") or []:
            for item in page_items or []:
                if not isinstance(item, dict):
                    continue
                pid = str(item.get("id") or "")
                card = card_from_item(item)
                if not pid or card is None or pid in seen:
                    continue
                seen.add(pid)
                rows.append((pid, card))
        if any(res.get("pages") or []) and not rows:
            return self._shut("не получилось разобрать заявки из ответа сайта", 60.0)

        now = time.monotonic()
        for pid, (key, card, ts) in list(self.recent.items()):
            if now - ts > RECENT_SECONDS:
                del self.recent[pid]
            elif pid not in seen:  # пришла по push, а снимок списка ещё старый
                rows.append((pid, card))
                seen.add(pid)

        ids: dict[str, str] = {}
        id2key: dict[str, str] = {}
        pairs: list[tuple[str, Card]] = []
        for pid, card in rows:
            key = self.id2key.get(pid)
            if key is None or key in ids:
                base, n = base_key(card), 1
                key = base
                while key in ids:
                    n += 1
                    key = f"{base}-{n}"
            ids[key], id2key[pid] = pid, key
            pairs.append((key, card))
        self.ids, self.id2key, self.known_ids = ids, id2key, set(id2key)
        self.last_keys = set(ids)
        self.complete = int(res.get("rawTotal") or 1) <= MAX_PAGES
        self.last_at = time.monotonic()
        self.error = ""
        if not self.ready:
            self.ready = True
            self.reason = "работает"
            self.disabled_note = ""
            log.info("Быстрая линия: список заявок получен напрямую (%s шт., %.0f мс)", len(pairs), self.last_ms)
        return pairs

    async def _list(self, url: str, token: str) -> dict:
        try:
            return await self.page.evaluate(
                LIST_JS, {"url": url, "headers": self._headers(token), "maxPages": MAX_PAGES, "timeout": 4000}
            )
        except Exception as exc:
            return {"status": 0, "text": "", "error": str(exc), "pages": []}

    def _shut(self, reason: str, cooldown: float = 600.0):
        log.warning("Быстрая линия отключена: %s", reason)
        self.ready = False
        self.reason = self.disabled_note = reason
        self.error = reason
        self.cooldown_until = time.monotonic() + cooldown
        return None

    # -- взятие --

    async def take(self, key: str) -> tuple[str, str, bool] | None:
        """Берёт заявку запросом сайта. Возвращает (статус, сообщение, ошибка_от_сайта)
        или None, если надо идти обычным путём (нажать кнопку на странице)."""
        if not (self.attached and self.page is not None and not self.page.is_closed()):
            return None
        pid = self.ids.get(key)
        if pid is None:
            return None
        token = await self.token()
        if not token:
            return None
        url = f"{self.base}{ASSIGN_PATH.format(id=quote(pid, safe=''))}"
        started = time.monotonic()
        res = await self._assign(url, token)
        if int(res.get("status") or 0) == 401:
            token = await self.token(force=True)
            if token:
                res = await self._assign(url, token)
        status = int(res.get("status") or 0)
        log.info("Быстрая линия: взятие %s -> HTTP %s за %.0f мс", pid, status or "ошибка сети", (time.monotonic() - started) * 1000)

        if 200 <= status < 300:
            self._forget(pid)
            return "ok", "", False
        if status == 0:
            return None  # сеть не ответила: пусть попробует кнопка
        message, already = error_text(res.get("text", ""))
        log.warning("Быстрая линия: взятие отклонено (%s): %s", status, message or res.get("text", "")[:120])
        if status in (404, 410) or already:
            self._forget(pid)
            return "gone", message or "заявки уже нет", False
        if status == 401:
            return "need_login", "Сессия на сайте слетела.", False
        if status == 429:
            return "failed", "Сайт просит реже (429), попробуй ещё раз.", True
        return "failed", message or f"Сайт ответил ошибкой {status}.", True

    async def _assign(self, url: str, token: str) -> dict:
        try:
            return await self.page.evaluate(
                FETCH_JS, {"url": url, "method": "PATCH", "headers": self._headers(token), "body": "", "timeout": 6000}
            )
        except Exception as exc:
            return {"status": 0, "text": "", "error": str(exc)}

    def _forget(self, pid: str) -> None:
        key = self.id2key.pop(pid, None)
        if key:
            self.ids.pop(key, None)
        self.known_ids.discard(pid)
        self.recent.pop(pid, None)

    # -- сверка и описание --

    def audit(self, dom_keys: set[str]) -> str:
        """Сверка со страницей: всё, что нарисовано, должно быть и в списке API.
        Если расхождение не проходит несколько проверок подряд, API — не тот формат."""
        if not self.ready or self.last_keys is None or time.monotonic() - self.last_at > 3.0:
            return ""
        if not dom_keys or dom_keys <= self.last_keys:
            self.mismatches = 0
            return ""
        self.mismatches += 1
        if self.mismatches < 4:
            return ""
        self._shut("данные API четыре проверки подряд не совпали со страницей")
        return self.disabled_note

    def describe(self) -> str:
        if not FAST_ON:
            return "выключена (FAST_LANE=0)"
        if self.ready:
            push = "push ✅" if self.ws_open else "push ⏳, пока только опрос"
            return f"активна ✅ (API, {push}, опрос ~{self.poll_interval():g} с, ответ ~{self.last_ms:.0f} мс)"
        if self.error:
            return f"недоступна ⚠️ ({self.error})"
        return f"запускается ⏳ ({self.reason})"

    def details(self) -> str:
        lines = [f"⚡ Быстрая линия: {self.describe()}"]
        lines.append(f"API: {self.base}")
        lines.append(f"Список: GET {urlparse(self.list_url).path or LIST_PATH}")
        lines.append(f"Взятие: PATCH {ASSIGN_PATH}")
        lines.append(f"WebSocket: {self.ws_url or '—'} ({'подключён' if self.ws_open else 'не подключён'})")
        if self.attached:
            exp = self._token_exp - time.time()
            lines.append(f"Токен: {'действует ещё ' + str(max(int(exp), 0) // 60) + ' мин' if exp > 0 else 'нет'}")
        if self.error:
            lines.append(f"Последняя ошибка: {self.error}")
        return "\n".join(lines)
