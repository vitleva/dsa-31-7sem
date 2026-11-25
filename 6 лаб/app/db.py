"""Таблицы PostgreSQL и функции чтения данных"""
import enum
import os
from datetime import datetime
from decimal import Decimal

from sqlalchemy import DateTime, ForeignKey, Integer, Numeric, String, Text, select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class OrderStatus(str, enum.Enum):
    CREATED = "CREATED"
    ACCEPTED = "ACCEPTED"
    READY = "READY"
    DELIVERED = "DELIVERED"
    CANCELLED = "CANCELLED"


class Base(DeclarativeBase):
    pass


class Restaurant(Base):
    __tablename__ = "restaurants"
    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(100))
    address: Mapped[str] = mapped_column(String(200))


class Dish(Base):
    __tablename__ = "dishes"
    id: Mapped[int] = mapped_column(primary_key=True)
    restaurant_id: Mapped[int] = mapped_column(ForeignKey("restaurants.id"))
    name: Mapped[str] = mapped_column(String(100))
    price: Mapped[Decimal] = mapped_column(Numeric(10, 2))


class Order(Base):
    __tablename__ = "orders"
    id: Mapped[int] = mapped_column(primary_key=True)
    restaurant_id: Mapped[int] = mapped_column(ForeignKey("restaurants.id"))
    customer_name: Mapped[str] = mapped_column(String(100))
    delivery_address: Mapped[str] = mapped_column(String(200))
    status: Mapped[OrderStatus] = mapped_column(default=OrderStatus.CREATED)
    cancel_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    total: Mapped[Decimal] = mapped_column(Numeric(12, 2))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class OrderItem(Base):
    __tablename__ = "order_items"
    id: Mapped[int] = mapped_column(primary_key=True)
    order_id: Mapped[int] = mapped_column(ForeignKey("orders.id"))
    dish_id: Mapped[int] = mapped_column(ForeignKey("dishes.id"))
    quantity: Mapped[int] = mapped_column(Integer)
    price: Mapped[Decimal] = mapped_column(Numeric(10, 2))


DATABASE_URL = os.getenv(
    "DATABASE_URL", "postgresql+asyncpg://food:food@postgres:5432/food"
)
engine = create_async_engine(DATABASE_URL, echo=True)
SessionLocal = async_sessionmaker(engine, expire_on_commit=False)


async def create_tables():
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)


async def get_restaurant(id: int):
    async with SessionLocal() as session:
        return await session.get(Restaurant, id)


async def get_dish(id: int):
    async with SessionLocal() as session:
        return await session.get(Dish, id)


async def get_order(id: int):
    async with SessionLocal() as session:
        return await session.get(Order, id)


async def get_items(order_id: int):
    async with SessionLocal() as session:
        result = await session.execute(
            select(OrderItem).where(OrderItem.order_id == order_id).order_by(OrderItem.id)
        )
        return list(result.scalars())


async def get_restaurants(ids: list[int]):
    if not ids:
        return {}
    async with SessionLocal() as session:
        result = await session.execute(select(Restaurant).where(Restaurant.id.in_(ids)))
        return {row.id: row for row in result.scalars()}


async def get_dishes(ids: list[int]):
    if not ids:
        return {}
    async with SessionLocal() as session:
        result = await session.execute(select(Dish).where(Dish.id.in_(ids)))
        return {row.id: row for row in result.scalars()}


async def get_items_by_orders(order_ids: list[int]):
    if not order_ids:
        return {}
    async with SessionLocal() as session:
        result = await session.execute(
            select(OrderItem)
            .where(OrderItem.order_id.in_(order_ids))
            .order_by(OrderItem.id)
        )
        grouped = {order_id: [] for order_id in order_ids}
        for row in result.scalars():
            grouped[row.order_id].append(row)
        return grouped


async def get_dishes_by_restaurant(restaurant_id: int):
    async with SessionLocal() as session:
        result = await session.execute(
            select(Dish).where(Dish.restaurant_id == restaurant_id).order_by(Dish.id)
        )
        return list(result.scalars())


async def list_restaurants():
    async with SessionLocal() as session:
        result = await session.execute(select(Restaurant).order_by(Restaurant.id))
        return list(result.scalars())


async def list_orders(status: OrderStatus | None = None):
    async with SessionLocal() as session:
        query = select(Order)
        if status is not None:
            query = query.where(Order.status == status)
        result = await session.execute(query.order_by(Order.created_at.desc(), Order.id.desc()))
        return list(result.scalars())
