"""Одна заглушка для каждого из трёх поставщиков."""
import asyncio
import os
import random

from fastapi import FastAPI, HTTPException
from common.flights import flights_for_day

app = FastAPI()
provider_id = os.getenv('PROVIDER_ID', '1')
delay = float(os.getenv('MEAN_DELAY_MS', '300')) / 1000
error_rate = float(os.getenv('ERROR_RATE', '0.05'))
slow_rate = float(os.getenv('SLOW_RATE', '0'))
count = int(os.getenv('FLIGHTS_PER_DAY', '500'))


@app.get('/health')
async def health():
    return {'status': 'ok', 'provider': provider_id}


@app.get('/flights')
async def get_flights(date: str):
    try:
        result = flights_for_day(date, 'P' + provider_id, count)
    except ValueError:
        raise HTTPException(422, 'Неверная дата')

    # Имитация сети: sleep НЕ блокирует другие запросы поставщика.
    if random.random() < slow_rate:
        await asyncio.sleep(3)
    else:
        await asyncio.sleep(random.uniform(delay * 0.7, delay * 1.3))

    if random.random() < error_rate:
        raise HTTPException(500, 'Ошибка поставщика')
    return {'flights': result}
