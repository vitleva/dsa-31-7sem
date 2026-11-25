"""Типы, запросы, мутации, загрузчики и подписка GraphQL"""
import logging
import os
from datetime import datetime
from decimal import Decimal
from typing import AsyncGenerator

import strawberry
from graphql import GraphQLError
from strawberry.dataloader import DataLoader
from strawberry.types import Info

from app import db, events, services

log = logging.getLogger(__name__)
OrderStatus = strawberry.enum(db.OrderStatus)


@strawberry.type
class Restaurant:
    id: strawberry.ID
    name: str
    address: str

    @strawberry.field
    async def dishes(self) -> list["Dish"]:
        rows = await db.get_dishes_by_restaurant(int(self.id))
        return [to_dish(row) for row in rows]


@strawberry.type
class Dish:
    id: strawberry.ID
    name: str
    price: Decimal
    restaurant_id: strawberry.Private[int]

    @strawberry.field
    async def restaurant(self, info: Info) -> Restaurant:
        row = await load_restaurant(info, self.restaurant_id)
        return to_restaurant(row)


@strawberry.type
class OrderItem:
    dish_id: strawberry.Private[int]
    quantity: int
    price: Decimal
    amount: Decimal

    @strawberry.field
    async def dish(self, info: Info) -> Dish:
        row = await load_dish(info, self.dish_id)
        return to_dish(row)


@strawberry.type
class Order:
    id: strawberry.ID
    status: OrderStatus
    total: Decimal
    created_at: datetime
    customer_name: str
    delivery_address: str
    cancel_reason: str | None
    restaurant_id: strawberry.Private[int]

    @strawberry.field
    async def restaurant(self, info: Info) -> Restaurant:
        row = await load_restaurant(info, self.restaurant_id)
        return to_restaurant(row)

    @strawberry.field
    async def items(self, info: Info) -> list[OrderItem]:
        if info.context["use_loaders"]:
            rows = await info.context["items"].load(int(self.id))
        else:
            rows = await db.get_items(int(self.id))
        return [to_item(row) for row in rows]


def to_restaurant(row: db.Restaurant) -> Restaurant:
    return Restaurant(id=strawberry.ID(str(row.id)), name=row.name, address=row.address)


def to_dish(row: db.Dish) -> Dish:
    return Dish(id=strawberry.ID(str(row.id)), name=row.name,
                price=row.price, restaurant_id=row.restaurant_id)


def to_item(row: db.OrderItem) -> OrderItem:
    return OrderItem(dish_id=row.dish_id, quantity=row.quantity,
                     price=row.price, amount=row.price * row.quantity)


def to_order(row: db.Order) -> Order:
    return Order(id=strawberry.ID(str(row.id)), status=row.status, total=row.total,
                 created_at=row.created_at, customer_name=row.customer_name,
                 delivery_address=row.delivery_address, cancel_reason=row.cancel_reason,
                 restaurant_id=row.restaurant_id)


async def load_restaurant(info: Info, id: int):
    if info.context["use_loaders"]:
        return await info.context["restaurants"].load(id)
    return await db.get_restaurant(id)


async def load_dish(info: Info, id: int):
    if info.context["use_loaders"]:
        return await info.context["dishes"].load(id)
    return await db.get_dish(id)


@strawberry.input
class CreateOrderItemInput:
    dish_id: strawberry.ID
    quantity: int


@strawberry.input
class CreateOrderInput:
    restaurant_id: strawberry.ID
    customer_name: str
    delivery_address: str
    items: list[CreateOrderItemInput]


@strawberry.type
class Query:
    @strawberry.field
    async def order(self, id: strawberry.ID) -> Order | None:
        row = await db.get_order(int(id))
        return to_order(row) if row else None

    @strawberry.field
    async def orders(self, status: OrderStatus | None = None) -> list[Order]:
        return [to_order(row) for row in await db.list_orders(status)]

    @strawberry.field
    async def restaurants(self) -> list[Restaurant]:
        return [to_restaurant(row) for row in await db.list_restaurants()]


ERROR_CODES = {
    services.NotFound: "NOT_FOUND",
    services.TransitionNotAllowed: "TRANSITION_NOT_ALLOWED",
    services.OrderValidationError: "VALIDATION_ERROR",
}


async def call(operation):
    try:
        return await operation
    except tuple(ERROR_CODES) as exc:
        raise GraphQLError(str(exc), extensions={"code": ERROR_CODES[type(exc)]}) from exc
    except Exception as exc:
        log.exception("Ошибка бизнес-операции")
        raise GraphQLError("Internal error", extensions={"code": "INTERNAL_ERROR"}) from exc


@strawberry.type
class Mutation:
    @strawberry.mutation
    async def create_order(self, input: CreateOrderInput) -> Order | None:
        row = await call(services.create_order(
            input.restaurant_id, input.customer_name, input.delivery_address,
            [(item.dish_id, item.quantity) for item in input.items]
        ))
        return to_order(row)

    @strawberry.mutation
    async def accept_order(self, id: strawberry.ID) -> Order | None:
        return to_order(await call(services.accept(id)))

    @strawberry.mutation
    async def mark_order_ready(self, id: strawberry.ID) -> Order | None:
        return to_order(await call(services.mark_ready(id)))

    @strawberry.mutation
    async def mark_order_delivered(self, id: strawberry.ID) -> Order | None:
        return to_order(await call(services.mark_delivered(id)))

    @strawberry.mutation
    async def cancel_order(self, id: strawberry.ID, reason: str) -> Order | None:
        return to_order(await call(services.cancel(id, reason)))


async def batch_restaurants(ids: list[int]):
    found = await db.get_restaurants(ids)
    return [found.get(id) for id in ids]


async def batch_dishes(ids: list[int]):
    found = await db.get_dishes(ids)
    return [found.get(id) for id in ids]


async def batch_items(order_ids: list[int]):
    found = await db.get_items_by_orders(order_ids)
    return [found.get(id, []) for id in order_ids]


async def get_context() -> dict:
    return {
        "use_loaders": os.getenv("USE_DATALOADER", "1") == "1",
        "restaurants": DataLoader(load_fn=batch_restaurants),
        "dishes": DataLoader(load_fn=batch_dishes),
        "items": DataLoader(load_fn=batch_items),
    }


@strawberry.type
class Subscription:
    @strawberry.subscription
    async def order_status_changed(
        self, order_id: strawberry.ID
    ) -> AsyncGenerator[Order, None]:
        id = int(order_id)
        queue = events.subscribe(id)
        try:
            row = await db.get_order(id)
            if row is None:
                raise GraphQLError(f"Заказ {id} не найден",
                                   extensions={"code": "NOT_FOUND"})
            yield to_order(row)  # текущее состояние при подключении
            status = row.status
            while status not in (db.OrderStatus.DELIVERED, db.OrderStatus.CANCELLED):
                event = await queue.get()
                status = event.status
                row = await db.get_order(id)
                result = to_order(row)
                result.status = status  # берём статус именно из события
                yield result
        finally:
            events.unsubscribe(id, queue)


schema = strawberry.Schema(query=Query, mutation=Mutation, subscription=Subscription)
