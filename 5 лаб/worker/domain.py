"""Правила бонусной программы: без RabbitMQ и SQLAlchemy."""
from decimal import Decimal, ROUND_DOWN


def calculate(data: dict, balance: int, purchase: dict | None,
              was_returned: bool) -> tuple[str, int, str | None]:
    kind = data["type"]
    if kind == "purchase":
        if purchase is not None:
            return "rejected", 0, "duplicate_purchase"
        points = int((Decimal(data["amount"]) * Decimal("0.05")).to_integral_value(
            rounding=ROUND_DOWN
        ))
        return "applied", points, None

    if kind == "return":
        if purchase is None:
            return "rejected", 0, "purchase_not_found"
        if purchase["customer_id"] != data["customer_id"]:
            return "rejected", 0, "customer_mismatch"
        if was_returned:
            return "rejected", 0, "already_returned"
        return "applied", -purchase["points_delta"], None

    points = data["points"]
    if Decimal(points) > Decimal(data["amount"]) * Decimal("0.30"):
        return "rejected", 0, "limit_exceeded"
    if points > balance:
        return "rejected", 0, "insufficient_balance"
    return "applied", -points, None
