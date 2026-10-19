import datetime as dt
from zoneinfo import ZoneInfo
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP

import discord

from config import CURRENCY, STAFF_ROLE_IDS, TIMEZONE


# ---------- money (stored as integer cents everywhere) ----------
def to_cents(value) -> int:
    try:
        d = Decimal(str(value).replace(",", "").strip())
        if not d.is_finite() or abs(d) > 10_000_000_000:
            raise InvalidOperation
        return int((d * 100).quantize(Decimal(1), rounding=ROUND_HALF_UP))
    except InvalidOperation:
        raise ValueError(f"Invalid amount: {value!r}")


SYMBOLS = {"INR": "₹", "USD": "$", "EUR": "€", "GBP": "£", "AED": "AED ", "AUD": "A$", "CAD": "C$"}


def _group(whole: int, cur: str) -> str:
    s = str(whole)
    if cur != "INR" or len(s) <= 3:
        return f"{whole:,}" if cur != "INR" else s
    head, tail = s[:-3], s[-3:]
    parts = []
    while len(head) > 2:
        parts.insert(0, head[-2:])
        head = head[:-2]
    if head:
        parts.insert(0, head)
    return ",".join(parts) + "," + tail


def fmt(cents: int, cur: str = CURRENCY) -> str:
    """₹500, ₹1,250.50, $12.50 ... (drops .00; Indian grouping for INR)."""
    sign = "-" if cents < 0 else ""
    whole, frac = divmod(abs(cents), 100)
    num = _group(whole, cur) + (f".{frac:02d}" if frac else "")
    return f"{sign}{SYMBOLS.get(cur, cur + ' ')}{num}"


def parse_items(text: str) -> list[tuple[str, float, int, str]]:
    """Each line: item name | quantity | unit price | spec; spec; spec   (specs optional)"""
    items = []
    for n, line in enumerate(text.splitlines(), 1):
        line = line.strip()
        if not line:
            continue
        parts = [p.strip() for p in line.split("|", 3)]
        if len(parts) < 3 or not parts[0]:
            raise ValueError(f"Line {n}: use `name | quantity | unit price | spec; spec` (specs optional)")
        desc, qty_s, price_s = parts[:3]
        specs = [s.strip()[:120] for s in (parts[3] if len(parts) == 4 else "").split(";") if s.strip()][:15]
        try:
            qty = Decimal(qty_s)
        except InvalidOperation:
            raise ValueError(f"Line {n}: invalid quantity {qty_s!r}")
        if not qty.is_finite() or qty > 1_000_000:
            raise ValueError(f"Line {n}: invalid quantity {qty_s!r}")
        if qty <= 0:
            raise ValueError(f"Line {n}: quantity must be above zero")
        price = to_cents(price_s)
        if price < 0:
            raise ValueError(f"Line {n}: price cannot be negative")
        items.append((desc[:200], float(qty), price, "\n".join(specs)))
    if not items:
        raise ValueError("Add at least one item.")
    if len(items) > 50:
        raise ValueError("Maximum 50 items per invoice.")
    return items


# ---------- dates (UTC) ----------
def _tz():
    try:
        return ZoneInfo(TIMEZONE)
    except Exception:  # unknown zone or tzdata missing in the container
        return dt.timezone.utc


def today() -> dt.date:
    return dt.datetime.now(_tz()).date()


def current_month() -> str:
    return today().strftime("%Y-%m")


def valid_month(value: str | None) -> str:
    if not value:
        return current_month()
    try:
        return dt.datetime.strptime(value.strip(), "%Y-%m").strftime("%Y-%m")
    except ValueError:
        raise ValueError("Month must look like `2026-10`.")


def parse_date(value: str | None) -> str:
    if not value:
        return today().isoformat()
    try:
        return dt.datetime.strptime(value.strip(), "%Y-%m-%d").date().isoformat()
    except ValueError:
        raise ValueError("Date must look like `2026-10-17`.")


def shift_month(month: str, delta: int) -> str:
    y, m = map(int, month.split("-"))
    idx = y * 12 + (m - 1) + delta
    return f"{idx // 12:04d}-{idx % 12 + 1:02d}"


# ---------- permissions ----------
def is_staff(user) -> bool:
    if not isinstance(user, discord.Member):
        return False
    return user.guild_permissions.administrator or any(
        r.id in STAFF_ROLE_IDS for r in user.roles
    )


async def staff_interaction_check(interaction: discord.Interaction) -> bool:
    if is_staff(interaction.user):
        return True
    await interaction.response.send_message(
        "🚫 You need a staff role to use this.", ephemeral=True
    )
    return False


# ---------- amount in words ----------
_ONES = ["", "One", "Two", "Three", "Four", "Five", "Six", "Seven", "Eight", "Nine", "Ten", "Eleven", "Twelve",
         "Thirteen", "Fourteen", "Fifteen", "Sixteen", "Seventeen", "Eighteen", "Nineteen"]
_TENS = ["", "", "Twenty", "Thirty", "Forty", "Fifty", "Sixty", "Seventy", "Eighty", "Ninety"]


def _below_100(n: int) -> str:
    return _ONES[n] if n < 20 else _TENS[n // 10] + (" " + _ONES[n % 10] if n % 10 else "")


def _below_1000(n: int) -> str:
    h, r = divmod(n, 100)
    return " ".join(p for p in ((_ONES[h] + " Hundred") if h else "", _below_100(r) if r else "") if p)


def _int_words(n: int, indian: bool) -> str:
    if n == 0:
        return "Zero"
    parts = []
    scales = ((10**7, "Crore"), (10**5, "Lakh"), (10**3, "Thousand")) if indian else \
             ((10**9, "Billion"), (10**6, "Million"), (10**3, "Thousand"))
    for div, name in scales:
        q, n = divmod(n, div)
        if q:
            parts.append((_int_words(q, True) if (indian and div == 10**7) else (_below_100(q) if indian else _below_1000(q))) + " " + name)
    if n:
        parts.append(_below_1000(n))
    return " ".join(parts)


def amount_in_words(cents: int, cur: str = CURRENCY) -> str:
    names = {"INR": ("Rupees", "Paise"), "USD": ("Dollars", "Cents"), "EUR": ("Euros", "Cents"),
             "GBP": ("Pounds", "Pence"), "AUD": ("Dollars", "Cents"), "CAD": ("Dollars", "Cents")}
    major_name, minor_name = names.get(cur, (cur, "Cents"))
    major, minor = divmod(abs(cents), 100)
    if major == 0 and minor:
        return f"{_int_words(minor, False)} {minor_name} Only"
    text = f"{major_name} {_int_words(major, cur == 'INR')}"
    if minor:
        text += f" and {_int_words(minor, False)} {minor_name}"
    return text + " Only"
