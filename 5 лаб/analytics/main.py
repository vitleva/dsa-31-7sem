"""Подписчик событий: статистика каждого магазина в памяти."""
import asyncio
import json
import logging
import os
from contextlib import asynccontextmanager, suppress

import aio_pika
from fastapi import FastAPI, HTTPException

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s")
RABBITMQ_URL = os.environ["RABBITMQ_URL"]

stores = {}
seen_ids = set()
received_events = 0


def empty_store(store_id):
    return {
        "store_id": store_id, "purchase_count": 0, "return_count": 0,
        "redeem_count": 0, "rejected_count": 0,
        "points_accrued": 0, "points_returned": 0, "points_redeemed": 0,
    }


async def consume(queue):
    global received_events
    async with queue.iterator() as messages:
        async for message in messages:
            async with message.process(requeue=True):
                event = json.loads(message.body.decode("utf-8"))
                operation_id = event["operation_id"]
                if operation_id in seen_ids:
                    continue
                received_events += 1
                store = stores.setdefault(event["store_id"], empty_store(event["store_id"]))
                if event["status"] == "rejected":
                    store["rejected_count"] += 1
                elif event["type"] == "purchase":
                    store["purchase_count"] += 1
                    store["points_accrued"] += event["points_delta"]
                elif event["type"] == "return":
                    store["return_count"] += 1
                    store["points_returned"] += abs(event["points_delta"])
                else:
                    store["redeem_count"] += 1
                    store["points_redeemed"] += abs(event["points_delta"])
                seen_ids.add(operation_id)
                logging.info("Статистика магазина %s обновлена", event["store_id"])


@asynccontextmanager
async def lifespan(app: FastAPI):
    rabbit = await aio_pika.connect_robust(RABBITMQ_URL)
    channel = await rabbit.channel()
    await channel.set_qos(prefetch_count=1)
    events = await channel.declare_exchange(
        "loyalty.events", aio_pika.ExchangeType.FANOUT, durable=True
    )
    queue = await channel.declare_queue(
        "analytics.events", durable=True, arguments={"x-queue-type": "quorum"}
    )
    await queue.bind(events)
    task = asyncio.create_task(consume(queue))
    task.add_done_callback(lambda t: logging.error("Получатель остановлен: %s", t.exception())
                           if not t.cancelled() and t.exception() else None)
    try:
        yield
    finally:
        task.cancel()
        with suppress(asyncio.CancelledError):
            await task
        await rabbit.close()


app = FastAPI(title="Бонусная программа — аналитика", lifespan=lifespan)


@app.get("/stores")
async def get_stores():
    return [stores[key] for key in sorted(stores)]


@app.get("/stores/{store_id}")
async def get_store(store_id: str):
    if store_id not in stores:
        raise HTTPException(status_code=404, detail="Событий магазина ещё не было")
    return stores[store_id]


@app.get("/stats")
async def get_stats():
    return {"received_events": received_events}
