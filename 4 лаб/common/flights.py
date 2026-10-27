#Генерация рейсов и поиск маршрутов
import random
from datetime import date, datetime, timedelta

# Одни и те же 30 аэропортов используются во всех версиях.
AIRPORTS = [
    'SVO', 'DME', 'VKO', 'LED', 'KZN', 'OVB', 'SVX', 'AER', 'KRR', 'UFA',
    'ROV', 'KGD', 'MRV', 'VOG', 'PEE', 'CEK', 'OMS', 'KJA', 'IKT', 'UUD',
    'VVO', 'KHV', 'YKS', 'PEK', 'IST', 'DXB', 'TAS', 'ALA', 'EVN', 'GYD',
]


def flights_for_day(day, carrier, count):
    # Одна дата и один перевозчик всегда дают одинаковые рейсы
    date.fromisoformat(day)  # Ошибка при неправильной дате
    rnd = random.Random(f'{carrier}-{day}')
    start = datetime.fromisoformat(day)
    result = []

    for number in range(1, count + 1):
        origin, destination = rnd.sample(AIRPORTS, 2)
        departure = start + timedelta(minutes=rnd.randrange(1440))
        arrival = departure + timedelta(minutes=rnd.randint(60, 360))
        result.append({
            'carrier': carrier,
            'flight_number': f'{carrier}{number:04d}',
            'origin': origin,
            'destination': destination,
            'departure': departure.isoformat(),
            'arrival': arrival.isoformat(),
            'price': rnd.randint(25, 600) * 10,
        })
    return result


def build_routes(flights, origin, destination, day):
    # До двух пересадок, ожидание 1–6 часов; вернуть 20 самых дешёвых
    departures = {}
    for flight in flights:
        # Все рейсы, в том числе после пересадок, вылетают в дату поиска.
        if flight['departure'][:10] == day:
            departures.setdefault(flight['origin'], []).append(flight)

    routes = []

    def find(airport, path, visited):
        if len(path) == 3:  # Три рейса = максимум две пересадки
            return

        for flight in departures.get(airport, []):
            next_airport = flight['destination']
            if next_airport in visited:  # Аэропорты не повторяются
                continue

            if path:
                previous_arrival = datetime.fromisoformat(path[-1]['arrival'])
                next_departure = datetime.fromisoformat(flight['departure'])
                hours = (next_departure - previous_arrival).total_seconds() / 3600
                if not 1 <= hours <= 6:
                    continue

            new_path = path + [flight]
            if next_airport == destination:
                routes.append({
                    'flights': new_path,
                    'total_price': sum(f['price'] for f in new_path),
                    'transfers': len(new_path) - 1,
                })
            else:
                find(next_airport, new_path, visited | {next_airport})

    find(origin, [], {origin})
    routes.sort(key=lambda item: item['total_price'])
    return routes[:20]
