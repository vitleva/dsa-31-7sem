"""Демонстрационные данные: 5 ресторанов, 50 блюд, 100 заказов"""
import asyncio
from datetime import datetime, timedelta, timezone
from decimal import Decimal

from sqlalchemy import func, select

from app import db


async def main():
    await db.create_tables()
    async with db.SessionLocal() as session:
        async with session.begin():
            count = await session.scalar(select(func.count()).select_from(db.Restaurant))
            if count:
                print("Данные уже есть. Повторное заполнение не требуется.")
                return

            restaurants = [
                db.Restaurant(name=f"Ресторан {i}", address=f"Новосибирск, улица {i}")
                for i in range(1, 6)
            ]
            session.add_all(restaurants)
            await session.flush()

            dishes_by_restaurant = {}
            for restaurant in restaurants:
                menu = [db.Dish(
                    restaurant_id=restaurant.id,
                    name=f"Блюдо {restaurant.id}-{j}",
                    price=Decimal(190 + 30 * restaurant.id + 25 * j)
                ) for j in range(1, 11)]
                session.add_all(menu)
                dishes_by_restaurant[restaurant.id] = menu
            await session.flush()

            states = list(db.OrderStatus)
            start = datetime(2026, 11, 1, 10, 0, tzinfo=timezone.utc)
            for i in range(100):
                restaurant = restaurants[i % 5]
                menu = dishes_by_restaurant[restaurant.id]
                first = menu[(i // 5) % 10]
                second = menu[((i // 5) + 1) % 10]
                quantity1, quantity2 = 2, 1
                status = states[i % 5]
                order = db.Order(
                    restaurant_id=restaurant.id,
                    customer_name=f"Клиент {i + 1:03}",
                    delivery_address=f"Новосибирск, адрес доставки {i + 1}",
                    status=status,
                    cancel_reason="Отмена клиентом" if status == db.OrderStatus.CANCELLED else None,
                    total=first.price * quantity1 + second.price * quantity2,
                    created_at=start + timedelta(minutes=i)
                )
                session.add(order)
                await session.flush()
                session.add_all([
                    db.OrderItem(order_id=order.id, dish_id=first.id,
                                 quantity=quantity1, price=first.price),
                    db.OrderItem(order_id=order.id, dish_id=second.id,
                                 quantity=quantity2, price=second.price),
                ])
    print("Создано 5 ресторанов, 50 блюд, 100 заказов (по 20 каждого состояния).")
    await db.engine.dispose()


if __name__ == "__main__":
    asyncio.run(main())
