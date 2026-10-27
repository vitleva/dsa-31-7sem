# таблица рейсов, заполнение, синхронное и async-чтение
import os
from datetime import date, datetime, timedelta

from sqlalchemy import Column, DateTime, Index, Integer, MetaData, String, Table
from sqlalchemy import create_engine, select
from common.flights import flights_for_day

SYNC_URL = os.getenv('DATABASE_URL_SYNC', 'postgresql+psycopg://postgres:postgres@localhost:5432/flights')
ASYNC_URL = os.getenv('DATABASE_URL_ASYNC', 'postgresql+asyncpg://postgres:postgres@localhost:5432/flights')

metadata = MetaData()
flights = Table(
    'flights', metadata,
    Column('id', Integer, primary_key=True),
    Column('carrier', String(16)),
    Column('flight_number', String(32)),
    Column('origin', String(8)),
    Column('destination', String(8)),
    Column('departure', DateTime),
    Column('arrival', DateTime),
    Column('price', Integer),
)
Index('idx_flights_date', flights.c.departure)


def day_query(day):
    start = datetime.fromisoformat(day)
    return select(flights).where(
        (flights.c.departure >= start) &
        (flights.c.departure < start + timedelta(days=1))
    )


def as_flights(rows):
    # Строки БД преобразуются к формату поставщиков 
    answer = []
    for row in rows:
        flight = dict(row)
        flight.pop('id', None)
        flight['departure'] = flight['departure'].isoformat()
        flight['arrival'] = flight['arrival'].isoformat()
        answer.append(flight)
    return answer


def read_sync(engine, day):
    with engine.connect() as conn:
        return as_flights(conn.execute(day_query(day)).mappings())


async def read_async(engine, day):
    async with engine.connect() as conn:
        result = await conn.execute(day_query(day))
        return as_flights(result.mappings())


def seed_database():
    # 120 суток по 1500 собственных рейсов, всегда одинаковых
    engine = create_engine(SYNC_URL)
    metadata.create_all(engine)
    start = date.fromisoformat(os.getenv('SEED_START_DATE', '2026-10-01'))

    with engine.begin() as conn:
        conn.execute(flights.delete())
        for i in range(120):
            day = (start + timedelta(days=i)).isoformat()
            generated = flights_for_day(day, 'OWN', 1500)
            for flight in generated:
                flight['departure'] = datetime.fromisoformat(flight['departure'])
                flight['arrival'] = datetime.fromisoformat(flight['arrival'])
            conn.execute(flights.insert(), generated)

    engine.dispose()
    print('Создано 180000 рейсов (120 дней × 1500)')


if __name__ == '__main__':
    seed_database()
