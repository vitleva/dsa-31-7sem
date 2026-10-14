# Доменная модель платной парковки

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Optional, Protocol

from exceptions import (
    DomainInvariantViolation,
    EntityNotFound,
    InvalidSessionTimeError,
    InvalidValueObject,
    SessionAlreadyClosedError,
    SpotAlreadyOccupiedError,
    TransitionNotAllowed,
)

_PLATE_HOMOGLYPHS = str.maketrans({
    "А": "A",
    "В": "B",
    "Е": "E",
    "К": "K",
    "М": "M",
    "Н": "H",
    "О": "O",
    "Р": "P",
    "С": "C",
    "Т": "T",
    "У": "Y",
    "Х": "X",
})
# «более 7 суток» — строго больше 7 дней
LONG_STAY = timedelta(days=7)
# «есть 15 минут на выезд»: ровно 15:00 ещё можно выехать
EXIT_GRACE = timedelta(minutes=15)


# Объекты-значения

@dataclass(frozen=True)
class SpotId:
    value: int

    def __post_init__(self) -> None:
        if self.value <= 0:
            raise InvalidValueObject("SpotId > 0")


@dataclass(frozen=True)
class SessionId:
    value: int

    def __post_init__(self) -> None:
        if self.value <= 0:
            raise InvalidValueObject("SessionId > 0")


@dataclass(frozen=True)
class Money:
    amount: float

    def __post_init__(self) -> None:
        if self.amount < 0:
            raise InvalidValueObject("Сумма ≥ 0")


@dataclass(frozen=True)
class LicensePlate:
    value: str

    def __post_init__(self) -> None:
        normalized = "".join(self.value.split()).upper().translate(_PLATE_HOMOGLYPHS)
        if not normalized:
            raise InvalidValueObject("Номер не пустой")
        object.__setattr__(self, "value", normalized)


@dataclass(frozen=True)
class Tariff:
    price_per_hour: float

    def __post_init__(self) -> None:
        if self.price_per_hour < 0:
            raise InvalidValueObject("Цена ≥ 0")

    def calculate(self, entry: datetime, exit_time: datetime) -> Money:
        """стоимость стоянки округляем вверх до полного часа"""
        if exit_time < entry:
            raise InvalidSessionTimeError("exit < entry")
        delta = exit_time - entry
        total_seconds = delta.days * 86400 + delta.seconds
        hours = (total_seconds + 3599) // 3600
        if hours < 1:
            hours = 1
        return Money(hours * self.price_per_hour)


@dataclass(frozen=True)
class Payment:
    amount: Money
    paid_at: datetime
    source: str

    def __post_init__(self) -> None:
        if self.source not in ("app", "terminal"):
            raise InvalidValueObject("Источник оплаты: app или terminal")


# Доменные события

@dataclass(frozen=True)
class SessionCreated:
    session_id: SessionId
    occurred_at: datetime
    plate: str


@dataclass(frozen=True)
class OperatorCalled:
    session_id: SessionId
    occurred_at: datetime


@dataclass(frozen=True)
class EntryRejected:
    session_id: SessionId
    occurred_at: datetime
    reason: str


@dataclass(frozen=True)
class EntryBarrierOpened:
    session_id: SessionId
    occurred_at: datetime
    spot_id: int


@dataclass(frozen=True)
class ExitRecognized:
    session_id: SessionId
    occurred_at: datetime
    plate: str


@dataclass(frozen=True)
class AppPaymentReceived:
    session_id: SessionId
    occurred_at: datetime
    amount: Money


@dataclass(frozen=True)
class PaymentAccepted:
    session_id: SessionId
    occurred_at: datetime
    amount: Money
    source: str


@dataclass(frozen=True)
class ExtraPaymentRequired:
    session_id: SessionId
    occurred_at: datetime
    amount: Money


@dataclass(frozen=True)
class ExtraPaymentAccepted:
    session_id: SessionId
    occurred_at: datetime
    amount: Money


@dataclass(frozen=True)
class ExitBarrierOpened:
    session_id: SessionId
    occurred_at: datetime


@dataclass(frozen=True)
class SessionClosed:
    session_id: SessionId
    occurred_at: datetime
    cost: Money


@dataclass(frozen=True)
class LongStayNotified:
    session_id: SessionId
    occurred_at: datetime


# Паттерн «Состояние»

class SessionState:
    """базовое состояние, любое событие по умолчанию запрещено"""

    name = ""
    allowed: frozenset[str] = frozenset()

    def создать_сессию(self, session: "ParkingSession") -> "SessionState":
        raise TransitionNotAllowed(self.name, "создать_сессию")

    def открыть_шлагбаум_въезда(
        self, session: "ParkingSession", spot_is_free: bool
    ) -> "SessionState":
        raise TransitionNotAllowed(self.name, "открыть_шлагбаум_въезда")

    def распознать_на_выезде(self, session: "ParkingSession") -> "SessionState":
        raise TransitionNotAllowed(self.name, "распознать_на_выезде")

    def оплатить(self, session: "ParkingSession", payment: Payment) -> "SessionState":
        raise TransitionNotAllowed(self.name, "оплатить")

    def истекли_15_мин(
        self, session: "ParkingSession", now: datetime
    ) -> "SessionState":
        raise TransitionNotAllowed(self.name, "истекли_15_мин")

    def доплатить(self, session: "ParkingSession", payment: Payment) -> "SessionState":
        raise TransitionNotAllowed(self.name, "доплатить")

    def закрыть_сессию(
        self, session: "ParkingSession", exit_time: datetime
    ) -> "SessionState":
        raise TransitionNotAllowed(self.name, "закрыть_сессию")

    def таймер_7_суток(
        self, session: "ParkingSession", now: datetime
    ) -> "SessionState":
        raise TransitionNotAllowed(self.name, "таймер_7_суток")

    def отклонить_въезд(self, session: "ParkingSession", reason: str) -> "SessionState":
        raise TransitionNotAllowed(self.name, "отклонить_въезд")


class Created(SessionState):
    name = "Создана"
    allowed = frozenset({"открыть_шлагбаум_въезда", "отклонить_въезд"})

    def открыть_шлагбаум_въезда(self, session: "ParkingSession", spot_is_free: bool):
        # Инвариант места передаётся из прикладного сервиса параметром
        if not spot_is_free:
            raise SpotAlreadyOccupiedError("Место занято")
        return Active()

    def отклонить_въезд(self, session: "ParkingSession", reason: str):
        if not reason or not reason.strip():
            raise InvalidValueObject("Нужна причина отказа")
        return Rejected()


class Active(SessionState):
    name = "Активна"
    allowed = frozenset({"распознать_на_выезде", "таймер_7_суток"})

    def распознать_на_выезде(self, session: "ParkingSession"):
        return AwaitingPayment()

    def таймер_7_суток(self, session: "ParkingSession", now: datetime):
        if now - session.entry_time <= LONG_STAY:
            raise DomainInvariantViolation("С момента въезда ещё не прошло более 7 суток")
        return HandedToOperator()


class AwaitingPayment(SessionState):
    name = "Ожидает оплаты"
    allowed = frozenset({"оплатить"})

    def оплатить(self, session: "ParkingSession", payment: Payment):
        return Paid()


class Paid(SessionState):
    name = "Оплачена"
    allowed = frozenset({"истекли_15_мин", "закрыть_сессию"})

    def истекли_15_мин(self, session: "ParkingSession", now: datetime):
        if session.payment is None:
            raise DomainInvariantViolation("Оплаты нет")
        if now - session.payment.paid_at <= EXIT_GRACE:
            raise DomainInvariantViolation("15 минут на выезд ещё не истекли")
        return NeedsExtraPayment()

    def закрыть_сессию(self, session: "ParkingSession", exit_time: datetime):
        # слияние параллельных ветвей: шлагбаум уже открыт, время выезда корректно
        if not session.exit_barrier_opened:
            raise DomainInvariantViolation(
                "Параллельная ветвь «Открыть шлагбаум на выезде» не завершена"
            )
        if exit_time < session.entry_time:
            raise InvalidSessionTimeError("exit < entry")
        return ClosedSuccessfully()


class NeedsExtraPayment(SessionState):
    name = "Требует доплаты"
    allowed = frozenset({"доплатить"})

    def доплатить(self, session: "ParkingSession", payment: Payment):
        due = session.extra_due
        if due is None:
            raise DomainInvariantViolation("Сумма доплаты не рассчитана")
        if payment.amount.amount < due.amount:
            raise DomainInvariantViolation("Сумма доплаты меньше требуемой")
        return Paid()


class ClosedSuccessfully(SessionState):
    name = "Закрыта успешно"


class HandedToOperator(SessionState):
    name = "Передана оператору"


class Rejected(SessionState):
    name = "Отклонена"


# Агрегаты


class ParkingSpot:
    def __init__(self, spot_id: SpotId):
        self._id = spot_id
        self._status = "free"
        self._current_session_id: Optional[SessionId] = None

    @property
    def id(self) -> SpotId:
        return self._id

    @property
    def status(self) -> str:
        return self._status

    @property
    def current_session_id(self) -> Optional[SessionId]:
        return self._current_session_id

    def is_free(self) -> bool:
        return self._status == "free"

    def occupy(self, session_id: SessionId) -> None:
        if self._status == "occupied":
            raise SpotAlreadyOccupiedError("Место занято")
        self._status = "occupied"
        self._current_session_id = session_id

    def free(self) -> None:
        if self._status != "occupied":
            raise DomainInvariantViolation("Уже свободно")
        self._status = "free"
        self._current_session_id = None


class ParkingSession:
    def __init__(
        self,
        session_id: SessionId,
        plate: LicensePlate,
        entry_time: datetime,
        tariff: Tariff,
    ):
        self._id = session_id
        self._plate = plate
        self._spot_id: Optional[SpotId] = None
        self._entry_time = entry_time
        self._exit_time: Optional[datetime] = None
        self._cost: Optional[Money] = None
        self._is_closed = False
        self._tariff = tariff
        self._payment: Optional[Payment] = None
        self._paid_total = Money(0)
        self._extra_due: Optional[Money] = None
        self._reject_reason: Optional[str] = None
        self._exit_barrier_opened = False
        self._state: SessionState = Created()
        self._events: list = [SessionCreated(session_id, entry_time, plate.value)]

    @property
    def id(self) -> SessionId:
        return self._id

    @property
    def plate(self) -> LicensePlate:
        return self._plate

    @property
    def spot_id(self) -> Optional[SpotId]:
        return self._spot_id

    @property
    def entry_time(self) -> datetime:
        return self._entry_time

    @property
    def exit_time(self) -> Optional[datetime]:
        return self._exit_time

    @property
    def cost(self) -> Optional[Money]:
        return self._cost

    @property
    def is_closed(self) -> bool:
        return self._is_closed

    @property
    def status(self) -> str:
        return self._state.name

    @property
    def allowed_events(self) -> frozenset[str]:
        return self._state.allowed

    @property
    def events(self) -> tuple:
        return tuple(self._events)

    @property
    def payment(self) -> Optional[Payment]:
        return self._payment

    @property
    def paid_total(self) -> Money:
        return self._paid_total

    @property
    def extra_due(self) -> Optional[Money]:
        return self._extra_due

    @property
    def reject_reason(self) -> Optional[str]:
        return self._reject_reason

    @property
    def exit_barrier_opened(self) -> bool:
        return self._exit_barrier_opened

    @property
    def tariff(self) -> Tariff:
        return self._tariff

    @property
    def outcome(self) -> Optional[str]:
        if self.status == "Закрыта успешно":
            return "Сессия завершена успешно"
        if self.status == "Передана оператору":
            return "Сессия передана оператору"
        if self.status == "Отклонена":
            return self._reject_reason
        return None

    def preview_cost(self, now: datetime) -> Money:
        return self._tariff.calculate(self._entry_time, now)

    def зафиксировать_вызов_оператора(self, now: datetime) -> None:
        """сообщение оператору, состояние сессии не меняется"""
        if self.status in ("Закрыта успешно", "Передана оператору", "Отклонена"):
            raise TransitionNotAllowed(self.status, "вызвать_оператора")
        self._events.append(OperatorCalled(self._id, now))

    def remember_app_payment(self, payment: Payment) -> None:
        """оплата в приложении, пока машина ещё на парковке"""
        if self.status != "Активна":
            raise TransitionNotAllowed(self.status, "оплатить")
        self._events.append(AppPaymentReceived(self._id, payment.paid_at, payment.amount))

    def открыть_шлагбаум_въезда(
        self, spot_id: SpotId, spot_is_free: bool, now: datetime
    ) -> None:
        new_state = self._state.открыть_шлагбаум_въезда(self, spot_is_free)
        self._spot_id = spot_id
        self._state = new_state
        self._events.append(EntryBarrierOpened(self._id, now, spot_id.value))

    def отклонить_въезд(self, reason: str, now: datetime) -> None:
        new_state = self._state.отклонить_въезд(self, reason)
        self._reject_reason = reason
        self._state = new_state
        self._events.append(EntryRejected(self._id, now, reason))

    def распознать_на_выезде(self, now: datetime) -> None:
        new_state = self._state.распознать_на_выезде(self)
        self._state = new_state
        self._events.append(ExitRecognized(self._id, now, self._plate.value))

    def оплатить(self, payment: Payment) -> None:
        new_state = self._state.оплатить(self, payment)
        paid_total = Money(self._paid_total.amount + payment.amount.amount)
        self._payment = payment
        self._paid_total = paid_total
        self._state = new_state
        self._events.append(
            PaymentAccepted(self._id, payment.paid_at, payment.amount, payment.source)
        )

    def истекли_15_мин(self, now: datetime) -> Money:
        """событие шлюза «прошло 15 минут», считает доплату по тарифу."""
        new_state = self._state.истекли_15_мин(self, now)
        total = self._tariff.calculate(self._entry_time, now)
        extra_amount = total.amount - self._paid_total.amount
        if extra_amount < 0:
            extra_amount = 0.0
        extra = Money(extra_amount)
        self._extra_due = extra
        self._state = new_state
        self._events.append(ExtraPaymentRequired(self._id, now, extra))
        return extra

    def доплатить(self, payment: Payment) -> None:
        new_state = self._state.доплатить(self, payment)
        paid_total = Money(self._paid_total.amount + payment.amount.amount)
        self._payment = payment
        self._paid_total = paid_total
        self._extra_due = Money(0)
        self._state = new_state
        self._events.append(ExtraPaymentAccepted(self._id, payment.paid_at, payment.amount))

    def mark_exit_barrier_opened(self, now: datetime) -> None:
        """ветвь параллельного шлюза, состояние жизненного цикла не меняет."""
        if self.status != "Оплачена":
            raise TransitionNotAllowed(self.status, "открыть_шлагбаум_выезда")
        if self._exit_barrier_opened:
            return
        self._exit_barrier_opened = True
        self._events.append(ExitBarrierOpened(self._id, now))

    def закрыть_сессию(self, exit_time: datetime) -> Money:
        if self._is_closed:
            raise SessionAlreadyClosedError("Уже закрыта")
        new_state = self._state.закрыть_сессию(self, exit_time)
        cost = self._tariff.calculate(self._entry_time, exit_time)
        self._exit_time = exit_time
        self._cost = cost
        self._is_closed = True
        self._state = new_state
        self._events.append(SessionClosed(self._id, exit_time, cost))
        return cost

    def on_long_stay(self, now: datetime) -> bool:
        """событие таймера, True — сессия передана оператору"""
        if "таймер_7_суток" not in self.allowed_events:
            return False
        if now - self._entry_time <= LONG_STAY:
            return False
        self._state = self._state.таймер_7_суток(self, now)
        self._events.append(LongStayNotified(self._id, now))
        return True


# Репозитории


class SpotRepository(Protocol):
    def save(self, spot: ParkingSpot) -> None: ...

    def get(self, spot_id: SpotId) -> ParkingSpot: ...

    def all(self) -> list[ParkingSpot]: ...


class SessionRepository(Protocol):
    def save(self, session: ParkingSession) -> None: ...

    def get(self, session_id: SessionId) -> ParkingSession: ...

    def all(self) -> list[ParkingSession]: ...


class InMemorySpotRepo:
    def __init__(self) -> None:
        self._data: dict[SpotId, ParkingSpot] = {}

    def save(self, spot: ParkingSpot) -> None:
        self._data[spot.id] = spot

    def get(self, spot_id: SpotId) -> ParkingSpot:
        try:
            return self._data[spot_id]
        except KeyError:
            raise EntityNotFound(f"Место {spot_id.value} не найдено")

    def all(self) -> list[ParkingSpot]:
        return list(self._data.values())


class InMemorySessionRepo:
    def __init__(self) -> None:
        self._data: dict[SessionId, ParkingSession] = {}

    def save(self, session: ParkingSession) -> None:
        self._data[session.id] = session

    def get(self, session_id: SessionId) -> ParkingSession:
        try:
            return self._data[session_id]
        except KeyError:
            raise EntityNotFound(f"Сессия {session_id.value} не найдена")

    def all(self) -> list[ParkingSession]:
        return list(self._data.values())
