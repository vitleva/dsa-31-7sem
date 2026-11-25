"""События изменения состояния заказа в одном процессе приложения"""
import asyncio
from collections import defaultdict
from dataclasses import dataclass

from app.db import OrderStatus


@dataclass(frozen=True)
class OrderStatusChanged:
    order_id: int
    status: OrderStatus


_queues: dict[int, set[asyncio.Queue]] = defaultdict(set)


def subscribe(order_id: int) -> asyncio.Queue:
    queue = asyncio.Queue()
    _queues[order_id].add(queue)
    return queue


def unsubscribe(order_id: int, queue: asyncio.Queue):
    _queues[order_id].discard(queue)
    if not _queues[order_id]:
        del _queues[order_id]


def publish(event: OrderStatusChanged):
    for queue in list(_queues.get(event.order_id, ())):
        queue.put_nowait(event)
