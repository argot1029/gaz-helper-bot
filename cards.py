"""Разбор карточки заявки и оформление уведомлений."""
import hashlib
import re
from dataclasses import asdict, dataclass, field, fields
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation
from html import escape

CARD_LABEL = "Взять в работу"

_CUR = r"(?:₽|руб\.?|RUB|\$|€|USDT|USD|EUR|₸|KZT|₴|UAH|£|¥)"
CUR_RE = re.compile(rf"^{_CUR}$", re.I)
AMOUNT_RE = re.compile(r"^\d[\d ]*(?:[.,]\d+)?$")
AMOUNT_CUR_RE = re.compile(rf"^(\d[\d ]*(?:[.,]\d+)?)\s*({_CUR})$", re.I)
RATE_RE = re.compile(r"^курс\b", re.I)
RATE_ONLY_RE = re.compile(r"^курс[\s:]*$", re.I)

# Кружок перед названием банка. Название сравнивается без регистра, пробелов и дефисов,
# поэтому «Т-Банк», «Т‑Банк» и «Тбанк» — это одно и то же.
# В Telegram нет голубого кружка, поэтому у ВТБ и Озона один и тот же 🔵.
BANK_DOTS: list[tuple[tuple[str, ...], str]] = [
    (("тбанк", "тbank", "tbank", "тинькоф", "tinkoff"), "🟡"),
    (("сбер", "sber"), "🟢"),
    (("озон", "ozon"), "🔵"),
    (("втб", "vtb"), "🔵"),
    (("альфа", "alfa", "alpha"), "🔴"),
    (("псб", "промсвязь", "psb"), "🟣"),
    (("юмани", "юmoney", "юмoney", "yoomoney", "yoomani"), "🟣"),
]

# Латинские буквы, похожие на кириллические (и наоборот): «T-Банк» с латинской T тоже находится.
_LAT2CYR = str.maketrans("acekmohptxyb", "асекмонртхув")
_CYR2LAT = str.maketrans("асекмонртхув", "acekmohptxyb")


@dataclass
class Card:
    amount: int | None = None  # сумма без копеек
    amount_key: str = ""  # точная сумма для сравнения заявок
    currency: str = ""
    bank: str = ""
    requisite: str = ""  # телефон / номер карты
    extras: list[str] = field(default_factory=list)  # всё остальное, что есть в карточке
    lines: list[str] = field(default_factory=list)  # чистые строки (запасной вариант)

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict) -> "Card":
        names = {f.name for f in fields(cls)}
        return cls(**{k: v for k, v in data.items() if k in names})


def _decimal(text: str) -> Decimal | None:
    try:
        return Decimal(text.replace(" ", "").replace(",", "."))
    except InvalidOperation:
        return None


def _currency(text: str) -> str:
    value = text.strip().rstrip(".")
    if value.lower() in ("руб", "rub"):
        return "₽"
    return value.upper() if value.isalpha() else value


def _is_requisite(line: str) -> bool:
    digits = re.sub(r"\D", "", line)
    return len(digits) >= 10 and re.fullmatch(r"[\d\s+\-()]+", line) is not None


def parse_card(text: str) -> Card:
    # 1. Чистые строки без пустых и без служебных (кнопка, курс).
    lines = [re.sub(r"\s+", " ", raw).strip() for raw in text.splitlines()]
    lines = [line for line in lines if line]

    cleaned: list[str] = []
    i = 0
    while i < len(lines):
        line = lines[i]
        i += 1
        if line.lower() == CARD_LABEL.lower():
            continue
        if RATE_ONLY_RE.match(line):  # «Курс» и значение на отдельной строке
            if i < len(lines) and AMOUNT_RE.match(lines[i]):
                i += 1
            continue
        if RATE_RE.match(line):
            continue
        cleaned.append(line)

    # 2. Сумма и валюта.
    amount_idx: int | None = None
    cur_idx: int | None = None
    value: Decimal | None = None
    currency = ""

    for idx, line in enumerate(cleaned):
        match = AMOUNT_CUR_RE.match(line)
        if match:
            amount_idx, value, currency = idx, _decimal(match.group(1)), _currency(match.group(2))
            break

    if amount_idx is None:
        for idx, line in enumerate(cleaned):
            if idx > 0 and CUR_RE.match(line) and AMOUNT_RE.match(cleaned[idx - 1]):
                amount_idx, cur_idx = idx - 1, idx
                value, currency = _decimal(cleaned[idx - 1]), _currency(line)
                break

    if amount_idx is None:
        numeric = [n for n, line in enumerate(cleaned) if AMOUNT_RE.match(line) and not _is_requisite(line)]
        with_fraction = [n for n in numeric if re.search(r"[.,]\d", cleaned[n])]
        picked = (with_fraction or numeric or [None])[0]
        if picked is not None:
            amount_idx, value = picked, _decimal(cleaned[picked])

    if amount_idx is not None and not currency:
        for idx, line in enumerate(cleaned):
            if idx != amount_idx and CUR_RE.match(line):
                cur_idx, currency = idx, _currency(line)
                break

    used = {n for n in (amount_idx, cur_idx) if n is not None}
    remaining = [line for n, line in enumerate(cleaned) if n not in used]

    # 3. Реквизиты и банк.
    requisite = next((line for line in remaining if _is_requisite(line)), "")
    if requisite:
        remaining.remove(requisite)
    bank = next((line for line in remaining if re.search(r"[^\W\d_]", line)), "")
    if bank:
        remaining.remove(bank)

    card = Card(currency=currency, bank=bank, requisite=requisite, extras=remaining, lines=cleaned)
    if value is not None:
        card.amount = int(value.quantize(Decimal("1"), rounding=ROUND_HALF_UP))
        card.amount_key = str(value.quantize(Decimal("0.01")))
    return card


def _norm(text: str) -> str:
    return re.sub(r"[^0-9a-zа-яё]", "", text.lower()).replace("ё", "е")


def bank_dot(bank: str) -> str:
    """Цветной кружок банка или пустая строка, если банк не из списка."""
    name = _norm(bank)
    variants = (name, name.translate(_LAT2CYR), name.translate(_CYR2LAT))
    for needles, dot in BANK_DOTS:
        if any(needle in variant for needle in needles for variant in variants):
            return dot
    return ""


bank_heart = bank_dot  # старое имя


def signature(card: Card) -> str:
    """Что считается «той же самой заявкой». Курс и прочие «живые» поля не учитываются.

    Если есть реквизит (телефон или карта), банк не учитывается: страница и API сайта могут
    называть банк по-разному, а сумма и реквизит однозначно определяют заявку.
    """
    digits = re.sub(r"\D", "", card.requisite)
    parts = [card.amount_key, digits]
    if len(digits) < 6:
        parts.append(_norm(card.bank))
    if not any(parts):
        parts = [_norm(" ".join(card.lines))]
    return "|".join(parts)


def base_key(card: Card) -> str:
    return hashlib.sha256(signature(card).encode("utf-8")).hexdigest()[:16]


def keys_for(cards: list[Card]) -> list[str]:
    """Стабильные ключи заявок. Две одинаковые заявки на странице получают разные ключи."""
    seen: dict[str, int] = {}
    keys = []
    for card in cards:
        base = base_key(card)
        seen[base] = seen.get(base, 0) + 1
        keys.append(base if seen[base] == 1 else f"{base}-{seen[base]}")
    return keys


def format_amount(value: int) -> str:
    """2423 -> «2 423»."""
    return f"{value:,}".replace(",", " ")


def format_money(card: Card) -> str:
    """Сумма с копейками: 40000.00 -> «40 000,00», 2423.5 -> «2 423,50»."""
    value = _decimal(card.amount_key) if card.amount_key else None
    if value is None:  # в старых записях без точной суммы
        value = Decimal(card.amount or 0)
    whole, _, cents = f"{value.quantize(Decimal('0.01')):f}".partition(".")
    return f"{format_amount(int(whole))},{cents or '00'}"


def _luhn(digits: str) -> bool:
    total = 0
    for i, ch in enumerate(reversed(digits)):
        n = int(ch)
        if i % 2 == 1:
            n *= 2
            if n > 9:
                n -= 9
        total += n
    return total % 10 == 0


def _phone(digits: str) -> str:
    if len(digits) == 11 and digits[0] in "78":
        return "+7" + digits[1:]
    if len(digits) == 10 and digits[0] == "9":
        return "+7" + digits
    return "+" + digits


def classify_requisite(raw: str) -> tuple[str, str]:
    """Что за реквизит: ("phone" | "card" | "other", как его показать).

    Телефон показываем как +79064156725, карту — подряд цифрами.
    """
    text = raw.strip()
    digits = re.sub(r"\D", "", text)
    if not digits:
        return "other", text
    if text.startswith("+"):
        return "phone", "+" + digits
    count = len(digits)
    if 16 <= count <= 19 or (13 <= count <= 15 and _luhn(digits)):
        return "card", digits
    if count <= 15:
        return "phone", _phone(digits)
    return "other", digits


def format_card_html(card: Card, note: str = "") -> str:
    """Компактный текст заявки (HTML для Telegram).

    Способ оплаты: СБП
    📞 +79064156725
    💵 40 000,00 ₽
    🟢 Сбербанк
    """
    out: list[str] = []

    kind, shown = ("other", "")
    if card.requisite:
        kind, shown = classify_requisite(card.requisite)
    if kind == "phone":
        out.append("Способ оплаты: СБП")
    elif kind == "card":
        out.append("Способ оплаты: По номеру карты")

    if card.requisite:
        icon = "📞" if kind == "phone" else "💳"
        out.append(f"{icon} <code>{escape(shown)}</code>")  # по тапу копируется

    if card.amount is not None:
        head = f"{format_money(card)} {card.currency}".strip()
        out.append(f"💵 <b>{escape(head)}</b>")

    if card.bank:
        dot = bank_dot(card.bank)
        out.append(f"{dot} {escape(card.bank)}" if dot else escape(card.bank))

    out.extend(escape(line) for line in card.extras)

    if not out:
        out = [escape(line) for line in card.lines] or ["(пустая карточка)"]

    if note:
        out.append(f"<i>{escape(note)}</i>")
    return "\n".join(out)
