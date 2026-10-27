"""Нагрузка: closed (C клиентов), open (поток запросов); /health раз в 100 мс."""
import argparse
import asyncio
import math
import random
import time
from datetime import date, timedelta

import httpx
from common.flights import AIRPORTS


def percentile(values, percent):
    """Перцентиль методом ближайшего ранга из дополнительных материалов."""
    if not values:
        return 0
    ordered = sorted(values)
    position = math.ceil(len(ordered) * percent / 100) - 1
    return round(ordered[position], 2)


def print_result(name, measurements, seconds):
    durations = [item[0] for item in measurements]
    errors = sum(item[1] for item in measurements)
    print(name)
    print('  Запросов:', len(durations))
    print('  Запросов/с:', round(len(durations) / seconds, 2))
    print('  p50, мс:', percentile(durations, 50))
    print('  p95, мс:', percentile(durations, 95))
    print('  p99, мс:', percentile(durations, 99))
    print('  Макс., мс:', round(max(durations, default=0), 2))
    print('  Ошибки, %:', round(errors / len(durations) * 100, 2) if durations else 0)


async def main(args):
    rnd = random.Random(42)  # Один набор случайных параметров для сравнений
    search_times = []
    health_times = []
    started = time.monotonic()
    measurement_start = started + args.warmup
    measurement_end = measurement_start + args.duration

    async with httpx.AsyncClient(
        timeout=args.timeout,
        limits=httpx.Limits(max_connections=2000, max_keepalive_connections=200),
    ) as client:
        async def request(path, storage, params=None):
            start = time.monotonic()
            try:
                response = await client.get(args.url + path, params=params)
                error = response.status_code >= 400
            except httpx.HTTPError:
                error = True
            elapsed = (time.monotonic() - start) * 1000
            if start >= measurement_start:
                storage.append((elapsed, error))

        def search_params():
            origin, destination = rnd.sample(AIRPORTS, 2)
            day = date(2026, 10, 1) + timedelta(days=rnd.randrange(120))
            return {'origin': origin, 'destination': destination, 'date': day.isoformat()}

        async def closed_client():
            while time.monotonic() < measurement_end:
                await request('/search', search_times, search_params())

        async def open_clients():
            tasks = []
            while time.monotonic() < measurement_end:
                tasks.append(asyncio.create_task(request('/search', search_times, search_params())))
                await asyncio.sleep(rnd.expovariate(args.rate))
            await asyncio.gather(*tasks)

        async def check_health():
            tasks = []
            next_request = time.monotonic()
            while time.monotonic() < measurement_end:
                tasks.append(asyncio.create_task(request('/health', health_times)))
                next_request += 0.1
                await asyncio.sleep(max(0, next_request - time.monotonic()))
            await asyncio.gather(*tasks)

        health_task = asyncio.create_task(check_health())
        if args.mode == 'closed':
            await asyncio.gather(*(closed_client() for _ in range(args.clients)))
        else:
            await open_clients()
        await health_task

    print_result('GET /search', search_times, args.duration)
    print_result('GET /health', health_times, args.duration)


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--url', default='http://localhost:8000')
    parser.add_argument('--mode', choices=['closed', 'open'], default='closed')
    parser.add_argument('--clients', type=int, default=10)
    parser.add_argument('--rate', type=float, default=5)
    parser.add_argument('--duration', type=float, default=60)
    parser.add_argument('--warmup', type=float, default=10)
    parser.add_argument('--timeout', type=float, default=30)
    asyncio.run(main(parser.parse_args()))
