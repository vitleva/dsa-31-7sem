"""Последовательная обработка очереди операций и публикация событий."""
import asyncio
import json
import logging
import os
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path

import aio_pika
from sqlalchemy import DateTime, ForeignKey, Integer, Numeric, String, select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

from domain import calculate

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s")
DATABASE_URL = os.environ["DATABASE_URL"]
RABBITMQ_URL = os.environ["RABBITMQ_URL"]


class Base(DeclarativeBase):
    pass


class Account(Base):
    __tablename__ = "accounts"
    customer_id: Mapped[str] = mapped_column(String, primary_key=True)
    balance: Mapped[int] = mapped_column(Integer, nullable=False, default=0)


class Operation(Base):
    __tablename__ = "operations"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    operation_id: Mapped[str] = mapped_column(String, unique=True, nullable=False)
    type: Mapped[str] = mapped_column(String, nullable=False)
    customer_id: Mapped[str] = mapped_column(ForeignKey("accounts.customer_id"), nullable=False)
    store_id: Mapped[str] = mapped_column(String, nullable=False)
    check_number: Mapped[str] = mapped_column(String, nullable=False)
    amount: Mapped[Decimal | None] = mapped_column(Numeric, nullable=True)
    points_delta: Mapped[int] = mapped_column(Integer, nullable=False)
    balance_after: Mapped[int] = mapped_column(Integer, nullable=False)
    status: Mapped[str] = mapped_column(String, nullable=False)
    reject_reason: Mapped[str | None] = mapped_column(String)
    occurred_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    processed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


def event_from_operation(op: Operation) -> dict:
    return {
        "operation_id": op.operation_id, "type": op.type,
        "customer_id": op.customer_id, "store_id": op.store_id,
        "status": op.status, "points_delta": op.points_delta,
        "balance_after": op.balance_after, "reject_reason": op.reject_reason,
        "processed_at": op.processed_at.isoformat(),
    }


async def save_operation(data: dict, sessions) -> dict:
    # Одна транзакция для изменения баланса и записи операции.
    async with sessions() as session:
        async with session.begin():
            previous = await session.scalar(select(Operation).where(
                Operation.operation_id == data["operation_id"]
            ))
            if previous is not None:
                # Повторная доставка не должна повторно изменять баланс.
                return event_from_operation(previous)

            account = await session.get(Account, data["customer_id"], with_for_update=True)
            if account is None:
                account = Account(customer_id=data["customer_id"], balance=0)
                session.add(account)
                await session.flush()

            purchase_row = None
            was_returned = False
            if data["type"] in ("purchase", "return"):
                purchase_row = await session.scalar(select(Operation).where(
                    Operation.type == "purchase",
                    Operation.check_number == data["check_number"],
                    Operation.status == "applied",
                ).order_by(Operation.id).limit(1))

            if data["type"] == "return" and purchase_row is not None:
                was_returned = await session.scalar(select(Operation.id).where(
                    Operation.type == "return",
                    Operation.check_number == data["check_number"],
                    Operation.status == "applied",
                ).limit(1)) is not None

            purchase = None
            if purchase_row is not None:
                purchase = {
                    "customer_id": purchase_row.customer_id,
                    "points_delta": purchase_row.points_delta,
                }
            status, delta, reason = calculate(data, account.balance, purchase, was_returned)
            if status == "applied":
                account.balance += delta

            operation = Operation(
                operation_id=data["operation_id"], type=data["type"],
                customer_id=data["customer_id"], store_id=data["store_id"],
                check_number=data["check_number"],
                amount=Decimal(data["amount"]) if "amount" in data and data["amount"] is not None else None,
                points_delta=delta, balance_after=account.balance,
                status=status, reject_reason=reason,
                occurred_at=datetime.fromisoformat(data["occurred_at"]),
                processed_at=datetime.now(timezone.utc),
            )
            session.add(operation)
            await session.flush()
            event = event_from_operation(operation)
    # К этому моменту транзакция уже зафиксирована.
    return event


async def main():
    engine = create_async_engine(DATABASE_URL)
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    sessions = async_sessionmaker(engine, expire_on_commit=False)

    rabbit = await aio_pika.connect_robust(RABBITMQ_URL)
    try:
        channel = await rabbit.channel(publisher_confirms=True, on_return_raises=True)
        await channel.set_qos(prefetch_count=1)
        direct = await channel.declare_exchange(
            "loyalty", aio_pika.ExchangeType.DIRECT, durable=True
        )
        queue = await channel.declare_queue(
            "operations", durable=True, arguments={"x-queue-type": "quorum"}
        )
        await queue.bind(direct, routing_key="operation")

        # Обе очереди событий создаются ДО первой публикации.
        events = await channel.declare_exchange(
            "loyalty.events", aio_pika.ExchangeType.FANOUT, durable=True
        )
        for name in ("notifier.events", "analytics.events"):
            subscriber = await channel.declare_queue(
                name, durable=True, arguments={"x-queue-type": "quorum"}
            )
            await subscriber.bind(events)

        Path("/tmp/worker-ready").touch()
        logging.info("Worker готов. Ожидание операций...")
        async with queue.iterator() as messages:
            async for message in messages:
                try:
                    data = json.loads(message.body.decode("utf-8"))
                    event = await save_operation(data, sessions)
                    notification = aio_pika.Message(
                        body=json.dumps(event, ensure_ascii=False).encode("utf-8"),
                        content_type="application/json",
                        message_id=event["operation_id"],
                        delivery_mode=aio_pika.DeliveryMode.PERSISTENT,
                    )
                    confirmation = await events.publish(
                        notification, routing_key="", mandatory=True
                    )
                    if confirmation is False:
                        raise RuntimeError("Не получено подтверждение события")
                    await message.ack()
                    logging.info("%s: %s, баланс %s", event["operation_id"],
                                 event["status"], event["balance_after"])
                except Exception:
                    logging.exception("Ошибка; операция будет повторена")
                    await asyncio.sleep(1)
                    if not message.processed:
                        await message.nack(requeue=True)
    finally:
        Path("/tmp/worker-ready").unlink(missing_ok=True)
        await rabbit.close()
        await engine.dispose()


if __name__ == "__main__":
    asyncio.run(main())
