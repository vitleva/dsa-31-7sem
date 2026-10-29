"""Приём операций и чтение результатов. Баллы здесь не рассчитываются."""
import asyncio
import json
import os
from contextlib import asynccontextmanager
from datetime import datetime
from decimal import Decimal
from typing import Literal

import aio_pika
from fastapi import FastAPI, HTTPException
from pydantic import BaseModel, ConfigDict, Field, StrictInt, model_validator
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

DATABASE_URL = os.environ["DATABASE_URL"]
RABBITMQ_URL = os.environ["RABBITMQ_URL"]


class OperationInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    operation_id: str = Field(min_length=1)
    type: Literal["purchase", "return", "redeem"]
    customer_id: str = Field(min_length=1)
    store_id: str = Field(min_length=1)
    check_number: str = Field(min_length=1)
    occurred_at: datetime
    amount: Decimal | None = None
    points: StrictInt | None = None

    @model_validator(mode="after")
    def check_fields(self):
        if self.occurred_at.utcoffset() is None:
            raise ValueError("occurred_at должен содержать часовой пояс")
        if self.type == "return":
            if "amount" in self.model_fields_set or "points" in self.model_fields_set:
                raise ValueError("для return поля amount и points отсутствуют")
        elif self.type == "purchase":
            if (self.amount is None or not self.amount.is_finite() or self.amount <= 0
                    or "points" in self.model_fields_set):
                raise ValueError("для purchase нужен положительный amount без points")
        else:
            if (self.amount is None or not self.amount.is_finite() or self.amount <= 0
                    or self.points is None or self.points <= 0):
                raise ValueError("для redeem нужны положительные amount и points")
        return self


@asynccontextmanager
async def lifespan(app: FastAPI):
    app.state.engine = create_async_engine(DATABASE_URL)
    app.state.rabbit = await aio_pika.connect_robust(RABBITMQ_URL)
    app.state.channel = await app.state.rabbit.channel(
        publisher_confirms=True, on_return_raises=True
    )
    app.state.exchange = await app.state.channel.declare_exchange(
        "loyalty", aio_pika.ExchangeType.DIRECT, durable=True
    )
    queue = await app.state.channel.declare_queue(
        "operations", durable=True, arguments={"x-queue-type": "quorum"}
    )
    await queue.bind(app.state.exchange, routing_key="operation")
    try:
        yield
    finally:
        await app.state.rabbit.close()
        await app.state.engine.dispose()


app = FastAPI(title="Бонусная программа — API", lifespan=lifespan)


@app.post("/operations", status_code=202)
async def submit_operation(operation: OperationInput):
    payload = operation.model_dump(mode="json")
    message = aio_pika.Message(
        body=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
        content_type="application/json",
        message_id=operation.operation_id,
        delivery_mode=aio_pika.DeliveryMode.PERSISTENT,
    )
    try:
        confirmation = await asyncio.wait_for(
            app.state.exchange.publish(
                message, routing_key="operation", mandatory=True
            ), timeout=3
        )
        if confirmation is False:
            raise RuntimeError("Брокер не подтвердил публикацию")
    except Exception as exc:
        raise HTTPException(status_code=503, detail="RabbitMQ не подтвердил публикацию") from exc
    return {"operation_id": operation.operation_id}


@app.get("/operations/{operation_id}")
async def operation_result(operation_id: str):
    async with app.state.engine.connect() as connection:
        row = (await connection.execute(text("""
            SELECT operation_id, type, customer_id, store_id, status,
                   points_delta, balance_after, reject_reason, processed_at
            FROM operations WHERE operation_id = :operation_id
        """), {"operation_id": operation_id})).mappings().first()
    if row is None:
        raise HTTPException(status_code=404, detail="Операция ещё не обработана")
    return {
        "operation_id": row["operation_id"],
        "type": row["type"], "customer_id": row["customer_id"],
        "store_id": row["store_id"], "status": row["status"],
        "points_delta": row["points_delta"], "balance_after": row["balance_after"],
        "reject_reason": row["reject_reason"],
        "processed_at": row["processed_at"].isoformat(),
    }


@app.get("/accounts/{customer_id}")
async def account_result(customer_id: str):
    async with app.state.engine.connect() as connection:
        balance = (await connection.execute(text(
            "SELECT balance FROM accounts WHERE customer_id = :customer_id"
        ), {"customer_id": customer_id})).scalar_one_or_none()
        if balance is None:
            raise HTTPException(status_code=404, detail="Счёт не найден")
        rows = (await connection.execute(text("""
            SELECT operation_id, type, store_id, check_number, amount,
                   points_delta, balance_after, status, reject_reason,
                   occurred_at, processed_at
            FROM operations WHERE customer_id = :customer_id
            ORDER BY processed_at DESC, id DESC LIMIT 20
        """), {"customer_id": customer_id})).mappings().all()
    return {
        "customer_id": customer_id,
        "balance": balance,
        "operations": [
            {**dict(row), "amount": str(row["amount"]) if row["amount"] is not None else None,
             "occurred_at": row["occurred_at"].isoformat(),
             "processed_at": row["processed_at"].isoformat()}
            for row in rows
        ],
    }


@app.get("/health")
async def health():
    try:
        async with app.state.engine.connect() as connection:
            await asyncio.wait_for(connection.execute(text("SELECT 1")), timeout=2)
        await asyncio.wait_for(app.state.rabbit.ready(), timeout=2)
        if app.state.channel.is_closed:
            raise RuntimeError("Канал RabbitMQ закрыт")
    except Exception as exc:
        raise HTTPException(status_code=503, detail="База или брокер недоступны") from exc
    return {"status": "ok"}
