# Четыре версии одного поиска, переменная SERVICE_VERSION
import asyncio
import os
import time
from concurrent.futures import ProcessPoolExecutor
from contextlib import asynccontextmanager
from datetime import date

import httpx
import requests
from fastapi import FastAPI, HTTPException, Request
from sqlalchemy import create_engine
from sqlalchemy.ext.asyncio import create_async_engine

from common.db import ASYNC_URL, SYNC_URL, read_async, read_sync
from common.flights import AIRPORTS, build_routes

VERSION = int(os.getenv('SERVICE_VERSION', '1'))
if VERSION not in (1, 2, 3, 4):
    raise ValueError('SERVICE_VERSION должен быть от 1 до 4')

PROVIDERS = [os.getenv(f'PROVIDER_{i}_URL', f'http://provider{i}:8000') for i in range(1, 4)]


@asynccontextmanager
async def lifespan(app):
    # Подключения создаются один раз при запуске приложения
    if VERSION <= 2:
        app.state.db = create_engine(SYNC_URL)
    else:
        app.state.db = create_async_engine(ASYNC_URL)
        app.state.http = httpx.AsyncClient(timeout=None)
    if VERSION == 4:
        app.state.pool = ProcessPoolExecutor(max_workers=2)

    yield

    if VERSION <= 2:
        app.state.db.dispose()
    else:
        await app.state.http.aclose()
        await app.state.db.dispose()
    if VERSION == 4:
        app.state.pool.shutdown()


app = FastAPI(lifespan=lifespan)


def provider_sync(url, day):
    # requests + time.sleep, максимум 2 секунды на поставщика
    deadline = time.monotonic() + 2
    for attempt in range(2):
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            break
        try:
            response = requests.get(url + '/flights', params={'date': day}, timeout=remaining)
            response.raise_for_status()
            return response.json()['flights']
        except (requests.RequestException, ValueError, KeyError):
            if attempt == 0 and deadline - time.monotonic() > 0.2:
                time.sleep(0.2)
    return None


async def provider_async(client, url, day):
    # httpx + asyncio.sleep, один общий тайм-аут на обе попытки
    try:
        async with asyncio.timeout(2):
            for attempt in range(2):
                try:
                    response = await client.get(url + '/flights', params={'date': day})
                    response.raise_for_status()
                    return response.json()['flights']
                except (httpx.HTTPError, ValueError, KeyError):
                    if attempt == 0:
                        await asyncio.sleep(0.2)
    except TimeoutError:
        pass
    return None


def check_parameters(origin, destination, day):
    if origin not in AIRPORTS or destination not in AIRPORTS or origin == destination:
        raise HTTPException(422, 'Неверные аэропорты')
    try:
        date.fromisoformat(day)
    except ValueError:
        raise HTTPException(422, 'Дата должна иметь формат YYYY-MM-DD')


def answer(start, routes, failed, own):
    return {
        'routes': routes,
        'failed_providers': failed,
        'temporarily_unavailable': len(failed) == 3 and not own,
        'processing_time_ms': round((time.perf_counter() - start) * 1000, 2),
        'version': VERSION,
    }


def search_sync(request, origin, destination, day):
    # Версия 1 и намеренно блокирующая версия 2
    start = time.perf_counter()
    own = read_sync(request.app.state.db, day)
    all_flights = own.copy()
    failed = []

    for i, url in enumerate(PROVIDERS, 1):
        flights = provider_sync(url, day)
        if flights is None:
            failed.append(f'P{i}')
        else:
            all_flights.extend(flights)

    routes = build_routes(all_flights, origin, destination, day)
    return answer(start, routes, failed, own)


async def search_async(request, origin, destination, day):
    # Версии 3 и 4: БД и поставщики ожидаются одновременно
    start = time.perf_counter()
    tasks = [read_async(request.app.state.db, day)]
    tasks += [provider_async(request.app.state.http, url, day) for url in PROVIDERS]
    results = await asyncio.gather(*tasks, return_exceptions=True)

    if isinstance(results[0], Exception):
        raise HTTPException(503, 'База данных не отвечает')

    own = results[0]
    all_flights = own.copy()
    failed = []
    for i, flights in enumerate(results[1:], 1):
        if isinstance(flights, Exception) or flights is None:
            failed.append(f'P{i}')
        else:
            all_flights.extend(flights)

    if VERSION == 4:
        loop = asyncio.get_running_loop()
        routes = await loop.run_in_executor(
            request.app.state.pool, build_routes, all_flights, origin, destination, day
        )
    else:
        routes = build_routes(all_flights, origin, destination, day)
    return answer(start, routes, failed, own)


# Версия 2: только def - async def, остальные блокировки оставлены специально.
if VERSION == 1:
    @app.get('/search')
    def search(request: Request, origin: str, destination: str, date: str):
        check_parameters(origin, destination, date)
        return search_sync(request, origin, destination, date)
else:
    @app.get('/search')
    async def search(request: Request, origin: str, destination: str, date: str):
        check_parameters(origin, destination, date)
        if VERSION == 2:
            return search_sync(request, origin, destination, date)
        return await search_async(request, origin, destination, date)


# В первой версии все обработчики объявлены обычным def
if VERSION == 1:
    @app.get('/health')
    def health():
        return {'status': 'ok', 'version': VERSION}
else:
    @app.get('/health')
    async def health():
        return {'status': 'ok', 'version': VERSION}
