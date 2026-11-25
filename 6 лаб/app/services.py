"""Бизнес-правила оформления заказа и переходов между состояниями"""
from datetime import datetime, timezone
from decimal import Decimal

from sqlalchemy import select

from app import db, events


class NotFound(Exception):
    pass


class TransitionNotAllowed(Exception):
    pass


class OrderValidationError(Exception):
    pass


async def create_order(restaurant_id: int, customer_name: str,
                       delivery_address: str, items: list[tuple[int, int]]):
    restaurant_id = int(restaurant_id)
    items = [(int(dish_id), quantity) for dish_id, quantity in items]
    name = customer_name.strip()
    address = delivery_address.strip()
    if not name or not address or len(name) > 100 or len(address) > 200:
        raise OrderValidationError("Укажите имя клиента и адрес доставки")
    if not 1 <= len(items) <= 10:
        raise OrderValidationError("В заказе должно быть от 1 до 10 позиций")
    if any(not 1 <= quantity <= 20 for _, quantity in items):
        raise OrderValidationError("Количество порций должно быть от 1 до 20")

    async with db.SessionLocal() as session:
        async with session.begin():
            restaurant = await session.get(db.Restaurant, restaurant_id)
            if restaurant is None:
                raise NotFound(f"Ресторан {restaurant_id} не найден")

            dish_ids = [dish_id for dish_id, _ in items]
            result = await session.execute(
                select(db.Dish).where(db.Dish.id.in_(dish_ids))
            )
            dishes = {dish.id: dish for dish in result.scalars()}
            total = Decimal("0.00")
            for dish_id, quantity in items:
                dish = dishes.get(dish_id)
                if dish is None:
                    raise NotFound(f"Блюдо {dish_id} не найдено")
                if dish.restaurant_id != restaurant_id:
                    raise OrderValidationError("Все блюда должны принадлежать выбранному ресторану")
                total += dish.price * quantity

            order = db.Order(restaurant_id=restaurant_id, customer_name=name,
                             delivery_address=address, status=db.OrderStatus.CREATED,
                             total=total, created_at=datetime.now(timezone.utc))
            session.add(order)
            await session.flush()
            for dish_id, quantity in items:
                session.add(db.OrderItem(order_id=order.id, dish_id=dish_id,
                                         quantity=quantity, price=dishes[dish_id].price))
        return order


async def _change_status(order_id: int, event: str, expected: tuple[db.OrderStatus, ...],
                         new_status: db.OrderStatus, reason: str | None = None):
    order_id = int(order_id)
    async with db.SessionLocal() as session:
        async with session.begin():
            order = await session.get(db.Order, order_id, with_for_update=True)
            if order is None:
                raise NotFound(f"Заказ {order_id} не найден")
            if order.status not in expected:
                raise TransitionNotAllowed(
                    f"Событие {event} недопустимо в состоянии {order.status.value}"
                )
            order.status = new_status
            if reason is not None:
                order.cancel_reason = reason
    # Событие отправляется только после успешной фиксации транзакции.
    events.publish(events.OrderStatusChanged(order.id, new_status))
    return order


async def accept(order_id: int):
    return await _change_status(order_id, "accept", (db.OrderStatus.CREATED,),
                                db.OrderStatus.ACCEPTED)


async def mark_ready(order_id: int):
    return await _change_status(order_id, "mark_ready", (db.OrderStatus.ACCEPTED,),
                                db.OrderStatus.READY)


async def mark_delivered(order_id: int):
    return await _change_status(order_id, "mark_delivered", (db.OrderStatus.READY,),
                                db.OrderStatus.DELIVERED)


async def cancel(order_id: int, reason: str):
    cleaned = reason.strip()
    if not 3 <= len(cleaned) <= 200:
        raise OrderValidationError("Причина отмены должна содержать от 3 до 200 символов")
    return await _change_status(
        order_id, "cancel", (db.OrderStatus.CREATED, db.OrderStatus.ACCEPTED),
        db.OrderStatus.CANCELLED, cleaned
    )
