"""Автовзятие заявок: фильтры (способ оплаты + диапазоны сумм), хранение и проверка заявки.

Бот берёт заявку сам, только если автовзятие включено и заявка подходит под ОБА фильтра:
  1) способ оплаты — СБП (телефон) и/или «По номеру карты»;
  2) сумма — попадает хотя бы в один из выбранных диапазонов.
Всё, что не подошло, приходит как обычное уведомление с ручной кнопкой «Взять в работу».

Суммы сравниваются в целых рублях без копеек: заявка на 3 999,60 ₽ попадает
в диапазон 3 000–3 999. Границы включительные, поэтому 10 000 ₽ подходит и под
«9 000–10 000», и под «10 000–15 000» (как в твоём списке).

Если не выбран способ оплаты или диапазон сумм, автовзятие ничего не берёт.
Настройки хранятся в /data/autotake.json и переживают перезапуски.
"""
import logging
from decimal import Decimal, InvalidOperation

import config
from cards import Card, classify_requisite
from storage import read_json, write_json

log = logging.getLogger("autotake")

FILE = getattr(config, "AUTOTAKE_FILE", config.DATA_DIR / "autotake.json")

PAY_SBP = "sbp"
PAY_CARD = "card"
PAY_LABELS = {PAY_SBP: "СБП", PAY_CARD: "По номеру карты"}

# Диапазоны сумм в рублях, границы включительные.
RANGES: list[tuple[int, int]] = [
    (3000, 3999),
    (4000, 4999),
    (5000, 5999),
    (6000, 6999),
    (7000, 7999),
    (8000, 8999),
    (9000, 10000),
    (10000, 15000),
    (15000, 20000),
]


def range_id(lo: int, hi: int) -> str:
    return f"{lo}-{hi}"


RANGE_BY_ID: dict[str, tuple[int, int]] = {range_id(lo, hi): (lo, hi) for lo, hi in RANGES}


def spaced(value: int) -> str:
    """3000 -> «3 000»."""
    return f"{value:,}".replace(",", " ")


def range_label(lo: int, hi: int) -> str:
    return f"{spaced(lo)}–{spaced(hi)}"


class Settings:
    def __init__(self) -> None:
        self.enabled = False
        self.types: set[str] = set(PAY_LABELS)  # по умолчанию выбраны оба способа
        self.ranges: set[str] = set()


settings = Settings()


# --- Хранение ---------------------------------------------------------------------


def save() -> None:
    write_json(
        FILE,
        {
            "enabled": settings.enabled,
            "types": [t for t in PAY_LABELS if t in settings.types],
            "ranges": [r for r in RANGE_BY_ID if r in settings.ranges],
        },
    )


def load() -> None:
    data = read_json(FILE, None)
    if not isinstance(data, dict):
        return
    settings.enabled = bool(data.get("enabled"))
    types = data.get("types")
    if isinstance(types, list):
        settings.types = {t for t in types if t in PAY_LABELS}
    ranges = data.get("ranges")
    if isinstance(ranges, list):
        settings.ranges = {r for r in ranges if r in RANGE_BY_ID}


# --- Изменение настроек -------------------------------------------------------------


def ready() -> bool:
    """Фильтры заданы: выбран способ оплаты и хотя бы один диапазон."""
    return bool(settings.types and settings.ranges)


def set_enabled(value: bool) -> None:
    settings.enabled = value
    save()


def toggle_type(kind: str) -> bool:
    if kind not in PAY_LABELS:
        return False
    settings.types ^= {kind}
    save()
    return True


def toggle_range(rid: str) -> bool:
    if rid not in RANGE_BY_ID:
        return False
    settings.ranges ^= {rid}
    save()
    return True


def select_all_ranges() -> None:
    settings.ranges = set(RANGE_BY_ID)
    save()


def clear_ranges() -> None:
    settings.ranges = set()
    save()


# --- Проверка заявки ------------------------------------------------------------------


def payment_kind(card: Card) -> str | None:
    """«sbp» для телефона, «card» для номера карты, иначе None (тогда вручную)."""
    if not card.requisite:
        return None
    kind, _ = classify_requisite(card.requisite)
    return {"phone": PAY_SBP, "card": PAY_CARD}.get(kind)


def whole_rubles(card: Card) -> int | None:
    """Сумма в целых рублях без копеек (3999,60 -> 3999)."""
    try:
        if card.amount_key:
            return int(Decimal(card.amount_key))
    except (InvalidOperation, ValueError):
        pass
    return card.amount


def in_selected_ranges(rubles: int) -> bool:
    return any(
        lo <= rubles <= hi
        for rid, (lo, hi) in RANGE_BY_ID.items()
        if rid in settings.ranges
    )


def check(card: Card) -> tuple[bool, str]:
    """(подходит ли заявка под фильтры, пояснение для лога). Не зависит от включения."""
    if card.currency and card.currency != "₽":
        return False, f"валюта {card.currency}, не рубли"
    kind = payment_kind(card)
    if kind is None:
        return False, "способ оплаты не определён"
    if kind not in settings.types:
        return False, f"способ оплаты {PAY_LABELS[kind]} не выбран"
    rubles = whole_rubles(card)
    if rubles is None:
        return False, "сумма не определена"
    if not in_selected_ranges(rubles):
        return False, f"сумма {rubles} вне выбранных диапазонов"
    return True, f"{PAY_LABELS[kind]}, {rubles} ₽"


def should_auto(card: Card) -> tuple[bool, str]:
    """Брать ли заявку автоматически."""
    if not settings.enabled:
        return False, "автовзятие выключено"
    if not ready():
        return False, "фильтры не заданы"
    return check(card)


# --- Подписи для интерфейса --------------------------------------------------------------


def types_label() -> str:
    chosen = [PAY_LABELS[t] for t in PAY_LABELS if t in settings.types]
    return ", ".join(chosen) if chosen else "не выбран"


def ranges_label() -> str:
    """Выбранные диапазоны; соседние склеиваются: 3 000–3 999 + 4 000–4 999 -> 3 000–4 999."""
    chosen = sorted(RANGE_BY_ID[r] for r in RANGE_BY_ID if r in settings.ranges)
    merged: list[list[int]] = []
    for lo, hi in chosen:
        if merged and lo <= merged[-1][1] + 1:
            merged[-1][1] = max(merged[-1][1], hi)
        else:
            merged.append([lo, hi])
    if not merged:
        return "не выбраны"
    return ", ".join(f"{range_label(lo, hi)} ₽" for lo, hi in merged)


def status_line() -> str:
    """Строка для «📊 Статуса»."""
    if settings.enabled and not ready():
        return "включено, но фильтры не выбраны ⚠️"
    filters = f"{types_label()} · {ranges_label()}"
    if settings.enabled:
        return f"включено ✅\n   {filters}"
    if ready():
        return f"выключено ⛔\n   Настроено: {filters}"
    return "выключено ⛔"
