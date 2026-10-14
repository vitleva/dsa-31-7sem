"""Предметная область 4: Платная парковка"""

from dataclasses import dataclass
from datetime import datetime
from typing import Optional, Protocol

import pytest

from exceptions import (
    DomainInvariantViolation,
    InvalidSessionTimeError,
    InvalidValueObject,
    SessionAlreadyClosedError,
    SpotAlreadyOccupiedError,
)


# Value Objects

@dataclass(frozen=True)
class SpotId:
    value: int

    def __post_init__(self):
        if self.value <= 0:
            raise InvalidValueObject("SpotId > 0")


@dataclass(frozen=True)
class SessionId:
    value: int

    def __post_init__(self):
        if self.value <= 0:
            raise InvalidValueObject("SessionId > 0")


@dataclass(frozen=True)
class Money:
    amount: float

    def __post_init__(self):
        if self.amount < 0:
            raise InvalidValueObject("Сумма ≥ 0")


@dataclass(frozen=True)
class Tariff:
    price_per_hour: float

    def __post_init__(self):
        if self.price_per_hour < 0:
            raise InvalidValueObject("Цена ≥ 0")

    def calculate(self, entry: datetime, exit: datetime) -> Money:
        # округление вверх до полного часа
        seconds = (exit - entry).total_seconds()
        hours = max(1, int((seconds + 3599) // 3600))
        return Money(hours * self.price_per_hour)


# Агрегаты

class ParkingSpot:
    """Агрегат-корень ParkingSpot"""

    def __init__(self, spot_id: SpotId):
        self._id = spot_id
        self._status = "free"  # free / occupied
        self._current_session_id = None

    @property
    def id(self):
        return self._id

    @property
    def status(self):
        return self._status

    def occupy(self, session_id: SessionId):
        if self._status == "occupied":
            raise SpotAlreadyOccupiedError("Место занято")
        self._status = "occupied"
        self._current_session_id = session_id

    def free(self):
        if self._status != "occupied":
            raise DomainInvariantViolation("Уже свободно")
        self._status = "free"
        self._current_session_id = None


class ParkingSession:
    """Агрегат-корень ParkingSession. Связь со Spot — только через spot_id"""

    def __init__(self, session_id: SessionId, spot_id: SpotId, entry_time: datetime, tariff: Tariff):
        self._id = session_id
        self._spot_id = spot_id
        self._entry_time = entry_time
        self._exit_time: Optional[datetime] = None
        self._cost: Optional[Money] = None
        self._is_closed = False
        self._tariff = tariff

    @property
    def id(self):
        return self._id

    @property
    def spot_id(self):
        return self._spot_id

    @property
    def is_closed(self):
        return self._is_closed

    @property
    def cost(self):
        return self._cost

    def close(self, exit_time: datetime) -> Money:
        if self._is_closed:
            raise SessionAlreadyClosedError("Уже закрыта")
        if exit_time < self._entry_time:
            raise InvalidSessionTimeError("exit < entry")
        self._exit_time = exit_time
        self._cost = self._tariff.calculate(self._entry_time, exit_time)
        self._is_closed = True
        return self._cost


# Репозитории

class SpotRepository(Protocol):
    def save(self, spot: ParkingSpot) -> None: ...


class SessionRepository(Protocol):
    def save(self, session: ParkingSession) -> None: ...


class InMemorySpotRepo:
    def __init__(self):
        self._data = {}

    def save(self, spot: ParkingSpot):
        self._data[spot.id] = spot


class InMemorySessionRepo:
    def __init__(self):
        self._data = {}

    def save(self, session: ParkingSession):
        self._data[session.id] = session


# Доменный сервис

class ParkingService:
    _next_session_id = 1

    def __init__(self, spots: SpotRepository, sessions: SessionRepository):
        self._spots = spots
        self._sessions = sessions

    def enter(self, spot: ParkingSpot, entry: datetime, tariff: Tariff) -> ParkingSession:
        session_id = SessionId(ParkingService._next_session_id)
        ParkingService._next_session_id += 1
        spot.occupy(session_id)
        session = ParkingSession(session_id, spot.id, entry, tariff)
        self._spots.save(spot)
        self._sessions.save(session)
        return session

    def exit(self, session: ParkingSession, spot: ParkingSpot, exit_time: datetime):
        session.close(exit_time)
        spot.free()
        self._sessions.save(session)
        self._spots.save(spot)


# Тест

def test_parking():
    spots = InMemorySpotRepo()
    sessions = InMemorySessionRepo()
    svc = ParkingService(spots, sessions)

    spot = ParkingSpot(SpotId(1))
    spots.save(spot)
    tariff = Tariff(100.0)
    entry = datetime(2026, 5, 1, 9, 0)

    session = svc.enter(spot, entry, tariff)
    assert spot.status == "occupied"

    with pytest.raises(SpotAlreadyOccupiedError):
        svc.enter(spot, entry, tariff)

    exit_time = datetime(2026, 5, 1, 10, 30)  # 1.5 ч - 2 часа
    svc.exit(session, spot, exit_time)
    assert spot.status == "free"
    assert session.cost.amount == 200.0
    assert session.is_closed

    with pytest.raises(SessionAlreadyClosedError):
        session.close(datetime(2026, 5, 1, 12, 0))