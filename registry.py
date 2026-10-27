"""Реестр заявок: актуальные и устаревшие. Хранится в /data.

Взятая в работу заявка из реестра просто удаляется — как будто её и не было."""
import logging
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path

import config
from cards import Card, base_key
from storage import read_json, write_json

log = logging.getLogger("registry")

ACTIVE = "active"
EXPIRED = "expired"


def _now() -> datetime:
    return datetime.now(timezone.utc)


@dataclass
class Diff:
    new: list[dict] = field(default_factory=list)
    expired: list[dict] = field(default_factory=list)


class Registry:
    def __init__(self, path: Path) -> None:
        self.path = path
        self.entries: dict[str, dict] = {}
        # True, пока не было ни одного успешного сканирования: тогда всё, что уже
        # висит на странице, считается «старым» и уведомлений не вызывает.
        self.fresh = True
        # Прошлые версии видели только заявки, нарисованные на странице. Первый полный снимок API
        # запоминается тихо, иначе все остальные заявки списка пришли бы как «новые».
        self.api_baselined = False
        self._saved_at = 0.0

    # -- хранение --

    def load(self) -> None:
        data = read_json(self.path, None)
        self.fresh = not isinstance(data, dict)
        self.api_baselined = bool(isinstance(data, dict) and data.get("api_baselined"))
        self.entries = {}
        if isinstance(data, dict):
            for item in data.get("entries", []):
                if isinstance(item, dict) and item.get("key") and isinstance(item.get("card"), dict):
                    self.entries[item["key"]] = item
        self._migrate()

    def save(self) -> None:
        self._saved_at = time.monotonic()
        write_json(
            self.path,
            {"version": 1, "api_baselined": self.api_baselined, "entries": list(self.entries.values())},
        )

    def _save_soon(self, force: bool = False) -> None:
        """Реестр обновляется при каждой проверке: на диск сразу пишем только значимые изменения."""
        if force or time.monotonic() - self._saved_at > 5.0:
            self.save()

    def _migrate(self) -> None:
        """Ключ заявки не зависит от названия банка (страница и API называют банки по-разному):
        пересчитываем ключи, сохранённые прошлой версией."""
        fresh: dict[str, dict] = {}
        changed = False
        for key, entry in self.entries.items():
            try:
                base = base_key(Card.from_dict(entry["card"]))
            except Exception:
                fresh[key] = entry
                continue
            suffix = key.split("-", 1)[1] if "-" in key else ""
            new_key = base + (f"-{suffix}" if suffix else "")
            n = 1
            while new_key in fresh:
                n += 1
                new_key = f"{base}-{n}"
            if new_key != key:
                entry["key"] = new_key
                changed = True
            fresh[new_key] = entry
        self.entries = fresh
        if changed:
            self.save()

    # -- чтение --

    def get(self, key: str) -> dict | None:
        return self.entries.get(key)

    def active(self) -> list[dict]:
        items = [e for e in self.entries.values() if e["status"] == ACTIVE]
        return sorted(items, key=lambda e: e.get("pos", 0))

    def inactive(self) -> list[dict]:
        items = [e for e in self.entries.values() if e["status"] != ACTIVE]
        return sorted(items, key=lambda e: e.get("ended_at") or "", reverse=True)

    def pending(self) -> list[dict]:
        """Актуальные заявки, о которых ещё не удалось уведомить."""
        return [e for e in self.active() if not e.get("notified")]

    # -- изменение --

    def update(self, scan: list[tuple[str, Card]], first: bool = False, src: str = "dom") -> Diff:
        """Применяет полный снимок заявок (страница или список API): добавляет новые,
        отмечает пропавшие и помечает устаревшими."""
        now = _now().isoformat()
        diff = Diff()
        present = dict(scan)
        order = {key: pos for pos, (key, _) in enumerate(scan)}

        for key, card in present.items():
            entry = self.entries.get(key)
            if entry and entry["status"] == ACTIVE:
                entry.update(last_seen=now, missed=0, pos=order[key])
                if src == "api" or entry.get("src") != "api":  # точные данные API страницей не затираем
                    entry["card"] = card.to_dict()
                continue
            # Нет в реестре, либо уже была устаревшей/взятой, а в списке появилась снова —
            # это новая заявка (например, повторная выплата тем же реквизитам).
            entry = {
                "key": key,
                "card": card.to_dict(),
                "first_seen": now,
                "last_seen": now,
                "status": ACTIVE,
                "missed": 0,
                "notified": first,
                "pos": order[key],
                "ended_at": None,
                "src": src,
            }
            self.entries[key] = entry
            diff.new.append(entry)

        for key, entry in self.entries.items():
            if entry["status"] != ACTIVE or key in present:
                continue
            entry["missed"] = entry.get("missed", 0) + 1
            # Заявка должна пропасть из нескольких проверок подряд — это защита
            # от случайного сбоя загрузки страницы.
            if entry["missed"] >= config.EXPIRE_AFTER_MISSES:
                entry.update(status=EXPIRED, ended_at=now)
                diff.expired.append(entry)

        self.fresh = False
        self._prune()
        self._save_soon(force=bool(diff.new or diff.expired))
        return diff

    def add_new(self, scan: list[tuple[str, Card]], src: str = "api") -> list[dict]:
        """Push по WebSocket: добавляет только новые заявки. Ничего не удаляет и не считает
        пропуски — устаревание остаётся за полным снимком."""
        now = _now().isoformat()
        pos = max((e.get("pos", 0) for e in self.entries.values()), default=-1)
        added = []
        for key, card in scan:
            entry = self.entries.get(key)
            if entry and entry["status"] == ACTIVE:
                continue
            pos += 1
            entry = {
                "key": key,
                "card": card.to_dict(),
                "first_seen": now,
                "last_seen": now,
                "status": ACTIVE,
                "missed": 0,
                "notified": False,
                "pos": pos,
                "ended_at": None,
                "src": src,
            }
            self.entries[key] = entry
            added.append(entry)
        if added:
            self.save()
        return added

    def set_notified(self, key: str) -> None:
        entry = self.entries.get(key)
        if entry and not entry.get("notified"):
            entry["notified"] = True
            self.save()

    def mark(self, key: str, status: str) -> None:
        entry = self.entries.get(key)
        if not entry:
            return
        entry.update(status=status, ended_at=_now().isoformat(), missed=0)
        self._prune()
        self.save()

    def remove(self, key: str) -> None:
        """Заявка взята в работу: убираем из списка совсем."""
        if self.entries.pop(key, None) is not None:
            self.save()

    def _prune(self) -> None:
        cutoff = (_now() - timedelta(hours=config.EXPIRED_TTL_HOURS)).isoformat()
        inactive = self.inactive()  # сначала самые свежие
        fresh = [e for e in inactive if (e.get("ended_at") or "") >= cutoff]
        keep = {e["key"] for e in fresh[: config.EXPIRED_KEEP]}
        for entry in inactive:
            if entry["key"] not in keep:
                self.entries.pop(entry["key"], None)
