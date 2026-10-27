"""Короткие тесты обязательных правил построения маршрутов."""
from common.flights import AIRPORTS, build_routes, flights_for_day

DAY = '2026-10-15'


def flight(number, origin, destination, depart, arrive, price=100, day=DAY):
    return {
        'carrier': 'TEST', 'flight_number': number,
        'origin': origin, 'destination': destination,
        'departure': f'{day}T{depart}',
        'arrival': f'{day}T{arrive}',
        'price': price,
    }


def test_generator():
    one = flights_for_day(DAY, 'P1', 500)
    assert one == flights_for_day(DAY, 'P1', 500)
    assert len(one) == 500
    assert len(AIRPORTS) == 30


def test_direct_and_connection():
    flights = [
        flight('A', 'SVO', 'LED', '08:00:00', '10:00:00', 100),
        flight('B', 'LED', 'KZN', '11:00:00', '12:00:00', 150),
        flight('C', 'SVO', 'KZN', '09:00:00', '11:00:00', 400),
    ]
    routes = build_routes(flights, 'SVO', 'KZN', DAY)
    assert [r['total_price'] for r in routes] == [250, 400]
    assert [r['transfers'] for r in routes] == [1, 0]


def test_connection_waiting_limits():
    flights = [
        flight('A', 'SVO', 'LED', '08:00:00', '10:00:00'),
        flight('B', 'LED', 'KZN', '10:30:00', '12:00:00'),  # меньше часа
        flight('C', 'LED', 'KZN', '17:00:00', '18:00:00'),  # больше шести
        flight('D', 'LED', 'KZN', '11:00:00', '12:00:00'),  # допустимо
    ]
    routes = build_routes(flights, 'SVO', 'KZN', DAY)
    assert len(routes) == 1
    assert [f['flight_number'] for f in routes[0]['flights']] == ['A', 'D']


def test_no_repeated_airports():
    flights = [
        flight('A', 'SVO', 'LED', '08:00:00', '10:00:00'),
        flight('B', 'LED', 'SVO', '11:00:00', '12:00:00'),
        flight('C', 'SVO', 'KZN', '13:00:00', '14:00:00'),
    ]
    routes = build_routes(flights, 'SVO', 'KZN', DAY)
    assert len(routes) == 1
    assert routes[0]['transfers'] == 0


def test_all_departures_on_search_date():
    flights = [
        flight('A', 'SVO', 'LED', '08:00:00', '10:00:00'),
        flight('B', 'LED', 'KZN', '11:00:00', '12:00:00', day='2026-10-16'),
    ]
    assert build_routes(flights, 'SVO', 'KZN', DAY) == []


def test_maximum_two_connections():
    flights = [
        flight('A', 'SVO', 'LED', '06:00:00', '07:00:00'),
        flight('B', 'LED', 'KZN', '08:00:00', '09:00:00'),
        flight('C', 'KZN', 'OVB', '10:00:00', '11:00:00'),
        flight('D', 'OVB', 'AER', '12:00:00', '13:00:00'),
    ]
    assert build_routes(flights, 'SVO', 'AER', DAY) == []
    assert build_routes(flights, 'SVO', 'OVB', DAY)[0]['transfers'] == 2


def test_cheapest_twenty():
    flights = [flight(str(i), 'SVO', 'LED', '08:00:00', '10:00:00', i) for i in range(30, 0, -1)]
    routes = build_routes(flights, 'SVO', 'LED', DAY)
    assert len(routes) == 20
    assert [route['total_price'] for route in routes] == list(range(1, 21))
