# прикладной сервис процесса парковки

from __future__ import annotations

from datetime import datetime
from typing import Optional, Protocol

from domain import (
    EXIT_GRACE,
    InMemorySessionRepo,
    InMemorySpotRepo,
    LicensePlate,
    Money,
    ParkingSession,
    ParkingSpot,
    Payment,
    SessionId,
    SessionRepository,
    SpotId,
    SpotRepository,
    Tariff,
)
from exceptions import (
    ClockMovedBack,
    DomainInvariantViolation,
    EntityNotFound,
    GatewayUnresolved,
    OperatorInputRequired,
    TransitionNotAllowed,
)


# Порты и заглушки

class Clock(Protocol):
    def now(self) -> datetime: ...


class FixedClock:
    """часы, время которых задаётся снаружи, для проверки таймеров"""

    def __init__(self, moment: datetime):
        self._moment = moment

    def now(self) -> datetime:
        return self._moment

    def move_to(self, moment: datetime) -> None:
        if moment < self._moment:
            raise ClockMovedBack("Перевод часов назад запрещён")
        self._moment = moment


class Camera(Protocol):
    def recognize(self, raw_reading: Optional[str]) -> Optional[str]: ...


class InMemoryCamera:
    """пустая строка и None означают, что номер не распознан."""

    def recognize(self, raw_reading: Optional[str]) -> Optional[str]:
        if raw_reading is None:
            return None
        text = str(raw_reading).strip()
        if not text:
            return None
        return text


class Barrier(Protocol):
    def open(self, direction: str, plate: str) -> None: ...


class InMemoryBarrier:
    def __init__(self) -> None:
        self.opened: list[tuple[str, str]] = []

    def open(self, direction: str, plate: str) -> None:
        self.opened.append((direction, plate))


class PaymentTerminal(Protocol):
    def charge(
        self, session_id: SessionId, amount: Money, now: datetime, source: str
    ) -> Payment: ...

    def has_payment(self, session_id: SessionId) -> bool: ...

    def latest(self, session_id: SessionId) -> Optional[Payment]: ...


class InMemoryPaymentTerminal:
    """заглушка платёжной системы, хранит оплаты в памяти"""

    def __init__(self) -> None:
        self._by_session: dict[int, list[Payment]] = {}

    def charge(
        self, session_id: SessionId, amount: Money, now: datetime, source: str
    ) -> Payment:
        payment = Payment(amount, now, source)
        self._by_session.setdefault(session_id.value, []).append(payment)
        return payment

    def has_payment(self, session_id: SessionId) -> bool:
        return bool(self._by_session.get(session_id.value))

    def latest(self, session_id: SessionId) -> Optional[Payment]:
        items = self._by_session.get(session_id.value, [])
        return items[-1] if items else None


class BlockedPlateList(Protocol):
    def is_blocked(self, plate: LicensePlate) -> bool: ...


class InMemoryBlockedList:
    def __init__(self, plates: Optional[list[str]] = None):
        self._plates: set[str] = set()
        for plate in plates or []:
            self.block(plate)

    def block(self, plate: str) -> None:
        self._plates.add(LicensePlate(plate).value)

    def is_blocked(self, plate: LicensePlate) -> bool:
        return plate.value in self._plates


# Предикаты исключающих шлюзов


def exclusive_gateway(name: str, branches: list[tuple[bool, str]]) -> str:
    """ровно одна ветвь, иначе GatewayUnresolved (потока по умолчанию нет)"""
    matched = [label for condition, label in branches if condition]
    if len(matched) != 1:
        raise GatewayUnresolved(f"Шлюз «{name}» не разрешён: {matched}")
    return matched[0]


def plate_recognized(value: Optional[str]) -> bool:
    return value is not None and value.strip() != ""


def plate_not_recognized(value: Optional[str]) -> bool:
    return value is None or value.strip() == ""


def has_free_spots(spots: list[ParkingSpot]) -> bool:
    return any(spot.is_free() for spot in spots)


def no_free_spots(spots: list[ParkingSpot]) -> bool:
    return not any(spot.is_free() for spot in spots)


def is_plate_blocked(plate: LicensePlate, blocked: BlockedPlateList) -> bool:
    return blocked.is_blocked(plate)


def is_plate_allowed(plate: LicensePlate, blocked: BlockedPlateList) -> bool:
    return not blocked.is_blocked(plate)


def is_parking_paid(has_payment: bool) -> bool:
    return has_payment


def is_parking_unpaid(has_payment: bool) -> bool:
    return not has_payment


def grace_period_exceeded(session: ParkingSession, now: datetime) -> bool:
    if session.payment is None:
        return False
    return now - session.payment.paid_at > EXIT_GRACE


def within_grace_period(session: ParkingSession, now: datetime) -> bool:
    if session.payment is None:
        return False
    return now - session.payment.paid_at <= EXIT_GRACE


class ParkingProcess:
    """процесс «Заезд на парковку» от распознавания номера до выезда"""

    def __init__(
        self,
        spots: SpotRepository,
        sessions: SessionRepository,
        tariff: Tariff,
        clock: Clock,
        camera: Camera,
        barrier: Barrier,
        terminal: PaymentTerminal,
        blocked: BlockedPlateList,
    ):
        self.spots = spots
        self.sessions = sessions
        self.tariff = tariff
        self.clock = clock
        self.camera = camera
        self.barrier = barrier
        self.terminal = terminal
        self.blocked = blocked
        self.operator_calls: list[tuple[datetime, Optional[str]]] = []
        self._next_session_id = 1

    def recognize_plate(self, raw_reading: Optional[str]) -> Optional[str]:
        """задача «Распознать номер» (и на выезде - тот же порт камеры)"""
        return self.camera.recognize(raw_reading)

    def call_operator(self, raw_reading: Optional[str]) -> None:
        """задача «Вызвать оператора»"""
        self.operator_calls.append((self.clock.now(), raw_reading))

    def enter_plate_manually(self, plate: str) -> LicensePlate:
        """задача человека «Ввести номер в систему». принимает результат"""
        return LicensePlate(plate)

    def create_parking_session(self, plate: LicensePlate) -> ParkingSession:
        """задача «Создать парковочную сессию». начальное состояние - «Создана»"""
        session_id = SessionId(self._next_session_id)
        self._next_session_id += 1
        session = ParkingSession(session_id, plate, self.clock.now(), self.tariff)
        self.sessions.save(session)
        return session

    def reject_entry(self, session_id: SessionId, reason: str) -> ParkingSession:
        """завершающие события «Въезд запрещён: …» - состояние «Отклонена»"""
        session = self._require_session(session_id)
        session.отклонить_въезд(reason, self.clock.now())
        self.sessions.save(session)
        return session

    def open_entry_barrier(
        self, session_id: SessionId, spot_id: Optional[SpotId] = None
    ) -> ParkingSession:
        """задача «Открыть шлагбаум на въезде». Создана - Активна"""
        session = self._require_session(session_id)
        spot = self._require_spot(spot_id) if spot_id is not None else self._first_free_spot()
        now = self.clock.now()
        session.открыть_шлагбаум_въезда(spot.id, spot.is_free(), now)
        spot.occupy(session.id)
        self.barrier.open("entry", session.plate.value)
        self.sessions.save(session)
        self.spots.save(spot)
        return session

    def start_entry(
        self, raw_reading: Optional[str], manual_plate: Optional[str] = None
    ) -> ParkingSession:
        """въезд целиком: пути P1–P4
        manual_plate - результат задачи оператора, если камера номер не прочитала
        """
        plate, operator_used = self._resolve_plate(raw_reading, manual_plate)
        session = self.create_parking_session(plate)
        if operator_used:
            session.зафиксировать_вызов_оператора(self.clock.now())
            self.sessions.save(session)

        spots = self.spots.all()
        free_gate = exclusive_gateway(
            "Есть свободные места?",
            [(has_free_spots(spots), "yes"), (no_free_spots(spots), "no")],
        )
        if free_gate == "no":
            return self.reject_entry(session.id, "Въезд запрещён: нет мест")

        block_gate = exclusive_gateway(
            "Номер в списке заблокированных?",
            [
                (is_plate_blocked(plate, self.blocked), "yes"),
                (is_plate_allowed(plate, self.blocked), "no"),
            ],
        )
        if block_gate == "yes":
            return self.reject_entry(session.id, "Въезд запрещён: номер заблокирован")

        return self.open_entry_barrier(session.id)

    def pay_in_app(self, session_id: SessionId) -> Payment:
        """Водитель оплачивает стоянку в приложении, сессия остаётся «Активна»"""
        session = self._require_session(session_id)
        if session.status != "Активна":
            raise TransitionNotAllowed(session.status, "оплатить")
        if self.terminal.has_payment(session.id):
            raise DomainInvariantViolation("Оплата уже зарегистрирована")
        now = self.clock.now()
        amount = session.preview_cost(now)
        payment = self.terminal.charge(session.id, amount, now, "app")
        session.remember_app_payment(payment)
        self.sessions.save(session)
        return payment

    def recognize_plate_on_exit(
        self, raw_reading: Optional[str], manual_plate: Optional[str] = None
    ) -> ParkingSession:
        """задача «Распознать номер на выезде». Активна - Ожидает оплаты."""
        plate, operator_used = self._resolve_plate(raw_reading, manual_plate)
        session = self._find_active(plate)
        now = self.clock.now()
        if operator_used:
            session.зафиксировать_вызов_оператора(now)
        session.распознать_на_выезде(now)
        self.sessions.save(session)
        return session

    def confirm_app_payment(self, session_id: SessionId) -> ParkingSession:
        """шлюз «Стоянка оплачена?» = Да. Событие «оплатить»."""
        session = self._require_session(session_id)
        if "оплатить" not in session.allowed_events:
            raise TransitionNotAllowed(session.status, "оплатить")
        payment = self.terminal.latest(session.id)
        if payment is None:
            raise DomainInvariantViolation("Оплаты нет")
        session.оплатить(payment)
        self.sessions.save(session)
        return session

    def accept_terminal_payment(self, session_id: SessionId) -> Payment:
        """задача «Принять оплату через терминал». Событие «оплатить»."""
        session = self._require_session(session_id)
        if "оплатить" not in session.allowed_events:
            raise TransitionNotAllowed(session.status, "оплатить")
        now = self.clock.now()
        amount = session.preview_cost(now)
        payment = self.terminal.charge(session.id, amount, now, "terminal")
        session.оплатить(payment)
        self.sessions.save(session)
        return payment

    def request_extra_payment(self, session_id: SessionId) -> Money:
        """шлюз «С момента оплаты прошло 15 минут?» = Да.
        Событие истекли_15_мин: Оплачена - Требует доплаты
        """
        session = self._require_session(session_id)
        extra = session.истекли_15_мин(self.clock.now())
        self.sessions.save(session)
        return extra

    def accept_extra_payment(self, session_id: SessionId) -> Payment:
        """событие «доплатить»: Требует доплаты - Оплачена"""
        session = self._require_session(session_id)
        if "доплатить" not in session.allowed_events:
            raise TransitionNotAllowed(session.status, "доплатить")
        if session.extra_due is None:
            raise DomainInvariantViolation("Доплата не запрошена")
        now = self.clock.now()
        payment = self.terminal.charge(session.id, session.extra_due, now, "terminal")
        session.доплатить(payment)
        self.sessions.save(session)
        return payment

    def open_exit_barrier(self, session_id: SessionId) -> None:
        """задача «Открыть шлагбаум на выезде»"""
        session = self._require_session(session_id)
        self.barrier.open("exit", session.plate.value)
        session.mark_exit_barrier_opened(self.clock.now())
        self.sessions.save(session)

    def close_parking_session(self, session_id: SessionId) -> Money:
        """задача «Закрыть парковочную сессию» и освобождение места"""
        session = self._require_session(session_id)
        if session.spot_id is None:
            raise DomainInvariantViolation("У сессии нет парковочного места")
        spot = self._require_spot(session.spot_id)
        cost = session.закрыть_сессию(self.clock.now())
        spot.free()
        self.sessions.save(session)
        self.spots.save(spot)
        return cost

    def finish_exit(self, session_id: SessionId) -> ParkingSession:
        """Параллельные ветви выполняются последовательно.
        К закрытию сессии переходим только после открытия шлагбаума.
        Если 15 минут уже истекли, выезд не завершается
        """
        session = self._require_session(session_id)
        now = self.clock.now()
        if session.status != "Оплачена":
            raise TransitionNotAllowed(session.status, "закрыть_сессию")
        gate = exclusive_gateway(
            "С момента оплаты прошло 15 минут?",
            [
                (grace_period_exceeded(session, now), "yes"),
                (within_grace_period(session, now), "no"),
            ],
        )
        if gate == "yes":
            raise DomainInvariantViolation("Срок 15 минут истёк, нужна доплата")
        self.open_exit_barrier(session_id)
        self.close_parking_session(session_id)
        return self._require_session(session_id)

    def continue_exit(self, session_id: SessionId) -> ParkingSession:
        """Шлюзы выезда после распознавания номера.
        Если нужна доплата, метод останавливается в состоянии «Требует доплаты».
        После accept_extra_payment его вызывают ещё раз
        """
        session = self._require_session(session_id)
        if session.status == "Ожидает оплаты":
            has = self.terminal.has_payment(session.id)
            gate = exclusive_gateway(
                "Стоянка оплачена?",
                [(is_parking_paid(has), "yes"), (is_parking_unpaid(has), "no")],
            )
            if gate == "yes":
                self.confirm_app_payment(session.id)
            else:
                self.accept_terminal_payment(session.id)
            session = self._require_session(session_id)

        if session.status == "Оплачена":
            now = self.clock.now()
            gate = exclusive_gateway(
                "С момента оплаты прошло 15 минут?",
                [
                    (grace_period_exceeded(session, now), "yes"),
                    (within_grace_period(session, now), "no"),
                ],
            )
            if gate == "yes":
                self.request_extra_payment(session.id)
                return self._require_session(session_id)
            return self.finish_exit(session.id)

        if session.status == "Требует доплаты":
            raise DomainInvariantViolation(
                "Нужна доплата: вызовите accept_extra_payment, затем снова continue_exit"
            )
        raise TransitionNotAllowed(session.status, "закрыть_сессию")

    def on_long_stay(self, session_id: Optional[SessionId] = None) -> list[ParkingSession]:
        """таймер «7 суток» и задача «Уведомить оператора о длительной стоянке»"""
        now = self.clock.now()
        if session_id is not None:
            targets = [self._require_session(session_id)]
        else:
            targets = self.sessions.all()
        notified = []
        for session in targets:
            if session.on_long_stay(now):
                notified.append(session)
            self.sessions.save(session)
        return notified

    def _resolve_plate(
        self, raw_reading: Optional[str], manual_plate: Optional[str]
    ) -> tuple[LicensePlate, bool]:
        recognized = self.recognize_plate(raw_reading)
        gate = exclusive_gateway(
            "Номер распознан?",
            [
                (plate_recognized(recognized), "yes"),
                (plate_not_recognized(recognized), "no"),
            ],
        )
        if gate == "yes":
            return LicensePlate(recognized or ""), False
        if manual_plate is None or not str(manual_plate).strip():
            self.call_operator(raw_reading)
            raise OperatorInputRequired(
                "Номер не распознан. Нужен результат задачи «Ввести номер в систему»."
            )
        self.call_operator(raw_reading)
        return self.enter_plate_manually(manual_plate), True

    def _first_free_spot(self) -> ParkingSpot:
        for spot in self.spots.all():
            if spot.is_free():
                return spot
        raise GatewayUnresolved("Есть свободные места?")

    def _require_session(self, session_id: SessionId) -> ParkingSession:
        return self.sessions.get(session_id)

    def _require_spot(self, spot_id: SpotId) -> ParkingSpot:
        return self.spots.get(spot_id)

    def _find_active(self, plate: LicensePlate) -> ParkingSession:
        found = [
            session
            for session in self.sessions.all()
            if session.plate == plate and session.status == "Активна"
        ]
        if not found:
            raise EntityNotFound(f"Активная сессия для номера {plate.value} не найдена")
        if len(found) > 1:
            raise DomainInvariantViolation(
                f"Несколько активных сессий для номера {plate.value}"
            )
        return found[0]


def make_parking_process(
    spot_count: int = 2,
    blocked_plates: Optional[list[str]] = None,
    now: Optional[datetime] = None,
    price_per_hour: float = 100.0,
) -> ParkingProcess:
    """Сборка сервиса с заглушками, места создаются свободными"""
    moment = now or datetime(2026, 5, 1, 9, 0)
    spots = InMemorySpotRepo()
    for number in range(1, spot_count + 1):
        spots.save(ParkingSpot(SpotId(number)))
    return ParkingProcess(
        spots=spots,
        sessions=InMemorySessionRepo(),
        tariff=Tariff(price_per_hour),
        clock=FixedClock(moment),
        camera=InMemoryCamera(),
        barrier=InMemoryBarrier(),
        terminal=InMemoryPaymentTerminal(),
        blocked=InMemoryBlockedList(blocked_plates or []),
    )
