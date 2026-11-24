"""Pure price math. Amounts are integer cents."""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal, ROUND_HALF_UP

from app.constants import MAX_MONEY_CENTS


@dataclass(frozen=True)
class Totals:
    subtotal_cents: int
    taxable_cents: int
    tax_cents: int
    discount_cents: int
    total_cents: int


def line_amount_cents(quantity: Decimal | int | str, unit_price_cents: int) -> int:
    qty = quantity if isinstance(quantity, Decimal) else Decimal(str(quantity))
    amount = (qty * Decimal(int(unit_price_cents))).quantize(Decimal("1"), rounding=ROUND_HALF_UP)
    cents = int(amount)
    if cents < 0 or cents > MAX_MONEY_CENTS:
        raise ValueError("Line total is out of range.")
    return cents


def compute_totals(lines, tax_rate_bps: int, tax_labor: bool, discount_cents: int) -> Totals:
    subtotal = 0
    taxable = 0
    rate = max(0, int(tax_rate_bps))
    for line in lines:
        amount = line_amount_cents(line.quantity, line.unit_price_cents)
        subtotal += amount
        if not (line.kind == "labor" and not tax_labor):
            taxable += amount
    tax = int(
        (Decimal(taxable) * Decimal(rate) / Decimal(10_000)).quantize(
            Decimal("1"), rounding=ROUND_HALF_UP
        )
    )
    requested = max(0, int(discount_cents))
    discount = min(requested, subtotal + tax)
    total = subtotal + tax - discount
    return Totals(
        subtotal_cents=subtotal,
        taxable_cents=taxable,
        tax_cents=tax,
        discount_cents=discount,
        total_cents=total,
    )
