"""Подписчик событий: уведомления без базы данных."""
import asyncio
import json
import logging
import os
import random
from collections import defaultdict
from contextlib import asynccontextmanager, suppress

import aio_pika
from fastapi import FastAPI

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s")
RABBITMQ_URL = os.environ["RABBITMQ_URL"]
MIN_DELAY_MS = int(os.getenv("MIN_DELAY_MS", "200"))
MAX_DELAY_MS = int(os.getenv("MAX_DELAY_MS", "1500"))
UNSUBSCRIBED = {value.strip() for value in os.getenv("UNSUBSCRIBED", "").split(",") if value.strip()}

notifications = defaultdict(list)
seen_ids = set()
received_events = 0
sent_notifications = 0


async def consume(queue):
    global received_events, sent_notifications
    async with queue.iterator() as messages:
        async for message in messages:
            async with message.process(requeue=True):
                event = json.loads(message.body.decode("utf-8"))
                operation_id = event["operation_id"]
                if operation_id in seen_ids:
                    continue
                received_events += 1
                customer = event["customer_id"]
                if customer not in UNSUBSCRIBED:
                    if event["status"] == "rejected":
                        text = f"Операция отклонена: {event['reject_reason']}"
                    elif event["points_delta"] >= 0:
                        text = (f"Начислено {event['points_delta']} баллов, "
                                f"баланс {event['balance_after']}")
                    else:
                        text = (f"Списано {abs(event['points_delta'])} баллов, "
                                f"баланс {event['balance_after']}")
                    await asyncio.sleep(random.uniform(MIN_DELAY_MS, MAX_DELAY_MS) / 1000)
                    notifications[customer].append({
                        "operation_id": operation_id, "customer_id": customer,
                        "message": text, "processed_at": event["processed_at"],
                    })
                    sent_notifications += 1
                    logging.info("Уведомление %s: %s", customer, text)
                else:
                    logging.info("Покупатель %s отписался", customer)
                seen_ids.add(operation_id)


@asynccontextmanager
async def lifespan(app: FastAPI):
    rabbit = await aio_pika.connect_robust(RABBITMQ_URL)
    channel = await rabbit.channel()
    await channel.set_qos(prefetch_count=1)
    events = await channel.declare_exchange(
        "loyalty.events", aio_pika.ExchangeType.FANOUT, durable=True
    )
    queue = await channel.declare_queue(
        "notifier.events", durable=True, arguments={"x-queue-type": "quorum"}
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


app = FastAPI(title="Бонусная программа — уведомления", lifespan=lifespan)


@app.get("/notifications")
async def get_notifications(customer_id: str):
    return notifications[customer_id]


@app.get("/stats")
async def get_stats():
    return {"received_events": received_events, "sent_notifications": sent_notifications}
