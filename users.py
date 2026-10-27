"""Кто может пользоваться ботом.

Основные аккаунты (они же администраторы) заданы в config.CHAT_IDS. Остальных добавляют
прямо в боте: «📊 Статус» → «➕ Добавить пользователя». Список хранится в /data/users.json
и переживает перезапуски.

Добавить можно по числовому ID (доступ сразу) или по @username. Telegram не позволяет
боту найти человека по имени заранее, поэтому @username ждёт, пока этот человек сам
откроет бота и нажмёт /start, и тогда бот запоминает его ID.
"""
import logging
import re
from datetime import datetime, timedelta, timezone

import config
from storage import read_json, write_json

log = logging.getLogger("users")

USERS_FILE = getattr(config, "USERS_FILE", config.DATA_DIR / "users.json")
PENDING_DAYS = 7  # сколько дней ждём, пока человек по @username нажмёт /start

_users: dict[int, dict] = {}    # добавленные: id -> {id, username, name, added_by, added_at}
_pending: dict[str, dict] = {}  # ждут /start: username в нижнем регистре -> {...}


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _stamp() -> str:
    return _now().isoformat()


def _save() -> None:
    write_json(
        USERS_FILE,
        {"users": list(_users.values()), "pending": list(_pending.values())},
    )


def _expired(item: dict) -> bool:
    try:
        added = datetime.fromisoformat(item.get("added_at") or "")
    except ValueError:
        return False
    return _now() - added > timedelta(days=PENDING_DAYS)


def _prune() -> bool:
    stale = [key for key, item in _pending.items() if _expired(item)]
    for key in stale:
        _pending.pop(key, None)
    return bool(stale)


def load() -> None:
    data = read_json(USERS_FILE, {}) or {}
    if not isinstance(data, dict):
        data = {}
    _users.clear()
    _pending.clear()
    for item in data.get("users", []):
        try:
            uid = int(item["id"])
        except (KeyError, TypeError, ValueError):
            continue
        _users[uid] = {
            "id": uid,
            "username": item.get("username") or "",
            "name": item.get("name") or "",
            "added_by": item.get("added_by") or "",
            "added_at": item.get("added_at") or "",
        }
    for item in data.get("pending", []):
        name = str(item.get("username") or "").lstrip("@")
        if name:
            _pending[name.lower()] = {
                "username": name,
                "added_by": item.get("added_by") or "",
                "added_at": item.get("added_at") or "",
            }
    if _prune():
        _save()


# --- Кто есть кто -------------------------------------------------------------


def owners() -> list[int]:
    """Основные аккаунты из config.CHAT_IDS: у них есть управление пользователями."""
    return list(dict.fromkeys(config.CHAT_IDS))


def is_owner(uid: int) -> bool:
    return uid in config.CHAT_IDS


def is_allowed(uid: int) -> bool:
    return uid in config.CHAT_IDS or uid in _users


def chat_ids() -> list[int]:
    """Кому слать уведомления: основные аккаунты и добавленные."""
    return list(dict.fromkeys([*config.CHAT_IDS, *_users]))


def added() -> list[dict]:
    return sorted(_users.values(), key=lambda e: e.get("added_at") or "")


def pending() -> list[dict]:
    if _prune():
        _save()
    return sorted(_pending.values(), key=lambda e: e.get("added_at") or "")


def label(entry: dict) -> str:
    name = (entry.get("name") or "").strip()
    username = entry.get("username") or ""
    if name and username:
        return f"{name} (@{username})"
    if name:
        return name
    if username:
        return f"@{username}"
    return str(entry.get("id", "?"))


# --- Ввод от администратора ----------------------------------------------------


def parse_target(text: str) -> tuple[str, int | str]:
    """("id", 123) | ("username", "name") | ("error", «что не так»)."""
    raw = (text or "").strip()
    if re.fullmatch(r"-\d+", raw):
        return "error", "Это ID группы или канала. Нужен ID личного аккаунта (положительное число)."
    cleaned = re.sub(r"^(?:https?://)?(?:www\.)?t\.me/", "", raw, flags=re.I).strip("/")
    cleaned = cleaned.lstrip("@")
    if re.fullmatch(r"\d{5,15}", cleaned):
        return "id", int(cleaned)
    if re.fullmatch(r"[A-Za-z][A-Za-z0-9_]{4,31}", cleaned):
        return "username", cleaned
    return "error", "Не похоже ни на @username, ни на числовой ID."


def add_by_id(uid: int, added_by: str = "") -> str:
    """"exists" — доступ уже есть, "added" — добавлен."""
    if is_allowed(uid):
        return "exists"
    _users[uid] = {
        "id": uid,
        "username": "",
        "name": "",
        "added_by": added_by,
        "added_at": _stamp(),
    }
    _save()
    return "added"


def add_by_username(username: str, added_by: str = "") -> str:
    """"exists" — у этого человека уже есть доступ, "waiting" — уже в ожидании, "pending"."""
    name = username.lstrip("@")
    key = name.lower()
    if any((e.get("username") or "").lower() == key for e in _users.values()):
        return "exists"
    _prune()
    if key in _pending:
        return "waiting"
    _pending[key] = {"username": name, "added_by": added_by, "added_at": _stamp()}
    _save()
    return "pending"


def remove_user(uid: int) -> bool:
    if uid in config.CHAT_IDS or uid not in _users:
        return False
    del _users[uid]
    _save()
    return True


def remove_pending(username: str) -> bool:
    if _pending.pop(username.lstrip("@").lower(), None) is None:
        return False
    _save()
    return True


# --- Входящие сообщения ----------------------------------------------------------


def seen(user) -> dict | None:
    """Вызывается для каждого пользователя, который что-то написал боту.

    Обновляет имя у добавленных и превращает @username из ожидания в настоящий доступ.
    Возвращает запись, если человек только что подключился.
    """
    username = (user.username or "").lstrip("@")
    name = user.full_name or ""

    entry = _users.get(user.id)
    if entry is not None:
        if entry.get("username") != username or entry.get("name") != name:
            entry.update(username=username, name=name)
            _save()
        return None

    key = username.lower()
    if not key or key not in _pending:
        return None
    item = _pending.pop(key)
    if user.id in config.CHAT_IDS or _expired(item):
        _save()
        return None
    entry = {
        "id": user.id,
        "username": username,
        "name": name,
        "added_by": item.get("added_by") or "",
        "added_at": _stamp(),
    }
    _users[user.id] = entry
    _save()
    log.info("Пользователь %s (@%s) подключился по приглашению", user.id, username)
    return entry
