"""Настройки бота.

Всё, что может понадобиться менять, читается из переменных окружения
(в Amvera: вкладка «Переменные»). Обязательна только TELEGRAM_BOT_TOKEN.
"""
import os
import re
from pathlib import Path
from urllib.parse import urlparse


def _int(name: str, default: int) -> int:
    try:
        return int(os.getenv(name, str(default)))
    except ValueError:
        return default


def _float(name: str, default: float) -> float:
    try:
        return float(os.getenv(name, str(default)).replace(",", "."))
    except ValueError:
        return default


def _ids(raw: str) -> list[int]:
    return [int(x) for x in re.findall(r"-?\d+", raw)]


# --- Telegram -------------------------------------------------------------
TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()

# Все эти аккаунты получают уведомления и могут управлять ботом.
# Сессия на сайте у них общая. Список можно переопределить переменной
# CHAT_IDS (id через запятую). Каждый аккаунт должен один раз нажать /start у бота.
DEFAULT_CHAT_IDS = [8580346032, 1409335470, 6739304697]
_chat_ids = _ids(os.getenv("CHAT_IDS", "")) or DEFAULT_CHAT_IDS
_chat_ids += _ids(os.getenv("CHAT_ID", ""))  # старое имя переменной
CHAT_IDS: list[int] = list(dict.fromkeys(_chat_ids))

# --- Сайт -----------------------------------------------------------------
URL = os.getenv("SITE_URL", "https://trader.gaz.trade/payouts/available").strip()
# Если сайт сам редиректит на форму входа — LOGIN_URL можно не задавать.
LOGIN_URL = os.getenv("LOGIN_URL", "").strip()
_parsed = urlparse(URL)
SITE_ORIGIN = f"{_parsed.scheme}://{_parsed.netloc}"

# --- Хранилище (в Amvera это постоянная папка /data) -----------------------
DATA_DIR = Path(os.getenv("DATA_DIR", "/data"))
AUTH_FILE = DATA_DIR / "auth_state.json"            # cookies + localStorage
SESSION_STORAGE_FILE = DATA_DIR / "session_storage.json"  # sessionStorage
PROFILE_FILE = DATA_DIR / "browser_profile.json"    # закреплённый User-Agent
REGISTRY_FILE = DATA_DIR / "requests.json"          # список заявок и их статусы
SETTINGS_FILE = DATA_DIR / "settings.json"          # интервал / вкл-выкл
AUTOTAKE_FILE = DATA_DIR / "autotake.json"          # настройки автовзятия

# --- Мониторинг -----------------------------------------------------------
# Период проверки страницы в секундах. Считается от НАЧАЛА проверки, а сама проверка занимает
# 1–3 секунды, так что страница обновляется примерно раз в SCAN_INTERVAL секунд.
# Если сайт начнёт ограничивать запросы, увеличь до 10 переменной SCAN_INTERVAL.
SCAN_INTERVAL = max(2.0, min(60.0, _float("SCAN_INTERVAL", 5.0)))
DEFAULT_INTERVAL = SCAN_INTERVAL
UNAUTH_INTERVAL = 60           # как часто проверять, пока сессии нет
AUTH_REMINDER_SECONDS = 1800   # (не используется: о слёте сессии сообщаем один раз)
STATE_SAVE_EVERY = 60          # как часто сохранять cookies на диск

# --- Быстрая линия (fastlane.py): прямой доступ к API сайта --------------------------------
# Сайт отдаёт список заявок по API, новые заявки присылает по WebSocket, а «Взять в работу» — это
# один запрос PATCH. Бот делает то же самое напрямую, без отрисовки страницы и клика.
# FAST_LANE=0 выключает её целиком (останется обычная проверка страницы и клик).
FAST_LANE = os.getenv("FAST_LANE", "1").strip().lower() not in ("0", "false", "no", "off", "")
_host = urlparse(URL).hostname or ""
_labels = _host.split(".")
SITE_DOMAIN = ".".join(_labels[1:]) if len(_labels) >= 3 else _host  # trader.gaz.trade -> gaz.trade
# Адреса по умолчанию взяты из кода сайта. Если сайт переедет, задай их переменными.
API_BASE = os.getenv("API_BASE", f"https://api-trader3.{SITE_DOMAIN}").strip().rstrip("/")
WS_URL = os.getenv("WS_URL", f"wss://ws-trader.{SITE_DOMAIN}").strip()
COGNITO_URL = os.getenv("COGNITO_URL", "").strip()  # пусто = адрес из токена (AWS Cognito)
# Как часто опрашивать список заявок, сек. Пока работает push по WebSocket, опрос только страхует
# (FAST_PUSH_POLL), а без push опрашиваем с периодом FAST_INTERVAL.
FAST_INTERVAL = max(0.3, min(10.0, _float("FAST_INTERVAL", 1.0)))
FAST_PUSH_POLL = max(1.0, min(30.0, _float("FAST_PUSH_POLL", 3.0)))
# Как часто перепроверять страницу целиком (сверка с API, вход): это уже не основной путь.
FAST_AUDIT_INTERVAL = max(3.0, min(300.0, _float("FAST_AUDIT_INTERVAL", 15.0)))

# --- Список заявок ----------------------------------------------------------
# Заявка считается устаревшей, если пропала с страницы в N проверках подряд.
EXPIRE_AFTER_MISSES = 2
LIST_MAX = _int("LIST_MAX", 30)                    # сколько актуальных показывать в списке
EXPIRED_KEEP = _int("EXPIRED_KEEP", 10)            # сколько устаревших показывать
EXPIRED_TTL_HOURS = _int("EXPIRED_TTL_HOURS", 24)  # через сколько часов забывать

# --- Браузер --------------------------------------------------------------
LOCALE = os.getenv("LOCALE", "ru-RU")
TIMEZONE = os.getenv("TIMEZONE", "Europe/Moscow")
# Пусто = авто (Chrome без слова Headless, версия как у установленного браузера).
USER_AGENT = os.getenv("USER_AGENT", "").strip()
# "chromium" включает «новый headless» (ближе к обычному Chrome, но тяжелее по RAM).
BROWSER_CHANNEL = os.getenv("BROWSER_CHANNEL", "").strip()

# --- Вход через бота ------------------------------------------------------
LOGIN_TIMEOUT = _int("LOGIN_TIMEOUT", 300)  # сколько секунд ждём код 2FA

# Если автоопределение формы не подошло — задай CSS-селекторы вручную.
SEL_LOGIN = os.getenv("SEL_LOGIN", "").strip()
SEL_PASSWORD = os.getenv("SEL_PASSWORD", "").strip()
SEL_OTP = os.getenv("SEL_OTP", "").strip()
SEL_SUBMIT = os.getenv("SEL_SUBMIT", "").strip()
