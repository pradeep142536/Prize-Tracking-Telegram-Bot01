"""Price formatting and the rules that decide when to alert a user."""
from __future__ import annotations

import html

MIN_DROP_PERCENT = 0.5  # ignore tiny fluctuations

_SYMBOLS = {"INR": "₹", "USD": "$", "EUR": "€", "GBP": "£"}


def _indian_group(n: int) -> str:
    s = str(n)
    if len(s) <= 3:
        return s
    head, tail = s[:-3], s[-3:]
    parts: list[str] = []
    while len(head) > 2:
        parts.insert(0, head[-2:])
        head = head[:-2]
    if head:
        parts.insert(0, head)
    return ",".join(parts + [tail])


def format_price(amount: float, currency: str = "INR") -> str:
    symbol = _SYMBOLS.get(currency.upper(), currency.upper() + " ")
    whole = int(amount)
    cents = round((amount - whole) * 100)
    if cents == 100:
        whole, cents = whole + 1, 0
    if currency.upper() == "INR":
        text = _indian_group(whole)
    else:
        text = f"{whole:,}"
    if cents:
        text += f".{cents:02d}"
    return symbol + text


def should_alert(old: float | None, new: float, target: float | None) -> bool:
    """True when this price change deserves a notification for one tracker."""
    if target is not None:
        if new > target:
            return False
        # hit the target for the first time, or dropped further below it
        return old is None or old > target or new < old
    if old is None or new >= old:
        return False
    return (old - new) / old * 100 >= MIN_DROP_PERCENT


def format_drop_message(
    title: str, old: float | None, new: float, currency: str, target: float | None = None
) -> str:
    """Format a friendly alert; product links live only in the inline keyboard."""
    safe_title = html.escape(title, quote=False)
    lines = ["🎉 <b>GREAT NEWS! PRICE DROPPED</b>", "", safe_title]
    if old is not None and old > 0:
        pct = (old - new) / old * 100
        saved = old - new
        lines.extend(
            [
                "",
                f"Previous price: {format_price(old, currency)}",
                f"🔥 <b>New price: {format_price(new, currency)}</b>",
                f"💰 You saved {format_price(saved, currency)} (-{pct:.0f}%)",
            ]
        )
    else:
        lines.extend(["", f"🔥 <b>New price: {format_price(new, currency)}</b>"])
    if target is not None and new <= target:
        lines.append(f"🎯 Reached your target of {format_price(target, currency)}")
    return "\n".join(lines)


def format_target_message(title: str, current: float, target: float, currency: str) -> str:
    """A distinct one-time message for a target condition, not a generic drop."""
    return "\n".join(
        [
            "🎉 <b>PRICE TARGET REACHED!</b>",
            "",
            html.escape(title, quote=False),
            "",
            f"Current price: <b>{format_price(current, currency)}</b>",
            f"Your target: {format_price(target, currency)}",
            "The current price is below your target! Reached your target.",
        ]
    )
