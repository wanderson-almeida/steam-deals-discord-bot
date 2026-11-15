"""Price parsing/formatting helpers."""

from __future__ import annotations

import re
from decimal import Decimal, InvalidOperation

_NUMBER = re.compile(r"\d[\d.,\s\u00a0]*")


def parse_price_text(text: str | None) -> int | None:
    """Convert a formatted price such as ``R$66,99`` or ``R$ 1.234,56`` to cents."""
    if not text:
        return None
    match = _NUMBER.search(text)
    if not match:
        return None
    number = re.sub(r"[\s\u00a0]", "", match.group(0)).strip(".,")
    if not number:
        return None
    # Decimal separator = last '.' or ',' followed by 1-2 digits.
    sep = max(number.rfind(","), number.rfind("."))
    if sep != -1 and 1 <= len(number) - sep - 1 <= 2:
        integer = re.sub(r"[.,]", "", number[:sep])
        fraction = number[sep + 1 :]
        normalized = f"{integer or '0'}.{fraction}"
    else:
        normalized = re.sub(r"[.,]", "", number)
    try:
        return int((Decimal(normalized) * 100).to_integral_value())
    except InvalidOperation:
        return None


def format_brl(cents: int | None) -> str:
    """Format cents as Brazilian Real, e.g. ``R$ 1.234,56``."""
    if cents is None:
        return "—"
    reais, centavos = divmod(int(cents), 100)
    return f"R$ {reais:,}".replace(",", ".") + f",{centavos:02d}"
