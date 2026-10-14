import os
from datetime import datetime, timedelta
from dataclasses import asdict, is_dataclass

from flask import Flask, jsonify, request

from domain import (
    InMemorySessionRepo, InMemorySpotRepo, LicensePlate, ParkingSpot,
    ParkingSession, Payment, SessionId, SpotId, Tariff,
)
from exceptions import (
    ClockMovedBack, DomainError, DomainInvariantViolation, EntityNotFound,
    GatewayUnresolved, InvalidValueObject, OperatorInputRequired,
    TransitionNotAllowed,
)
from process import (
    InMemoryBarrier, InMemoryBlockedList, InMemoryCamera,
    InMemoryPaymentTerminal, ParkingProcess,
)


class SystemOffsetClock:
    """Системное время со сдвигом; сдвиг нужен для проверки таймеров"""
    def __init__(self):
        self._offset = timedelta(0)
        self._last = datetime.now()

    def now(self):
        value = datetime.now() + self._offset
        if value < self._last:
            raise ClockMovedBack("Перевод часов назад запрещён")
        self._last = value
        return value

    def move_to(self, moment):
        current = self.now()
        if moment < current:
            raise ClockMovedBack("Перевод часов назад запрещён")
        self._offset = moment - datetime.now()
        self._last = moment
        return moment


def json_value(value):
    if is_dataclass(value):
        return {key: json_value(item) for key, item in asdict(value).items()}
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, (list, tuple, set, frozenset)):
        return [json_value(item) for item in value]
    if isinstance(value, dict):
        return {str(key): json_value(item) for key, item in value.items()}
    if hasattr(value, "value") and value.__class__.__name__ in {"SessionId", "SpotId", "Money", "LicensePlate"}:
        return value.value
    if hasattr(value, "amount"):
        return value.amount
    if hasattr(value, "__dict__"):
        return {k.lstrip("_"): json_value(v) for k, v in vars(value).items()}
    return value


def session_json(session):
    return {
        "id": session.id.value,
        "plate": session.plate.value,
        "spot_id": session.spot_id.value if session.spot_id else None,
        "status": session.status,
        "allowed_events": sorted(session.allowed_events),
        "events": [json_value(event) for event in session.events],
        "entry_time": session.entry_time.isoformat(),
        "exit_time": session.exit_time.isoformat() if session.exit_time else None,
        "cost": session.cost.amount if session.cost else None,
        "paid_total": session.paid_total.amount,
        "extra_due": session.extra_due.amount if session.extra_due else None,
        "reject_reason": session.reject_reason,
        "outcome": session.outcome,
    }


def create_app():
    app = Flask(__name__)
    port = int(os.getenv("PORT", "5000"))

    # Все часы, репозитории и сервис создаются один раз при запуске
    clock = SystemOffsetClock()
    spots = InMemorySpotRepo()
    for number in range(1, int(os.getenv("SPOT_COUNT", "2")) + 1):
        spots.save(ParkingSpot(SpotId(number)))
    sessions = InMemorySessionRepo()
    tariff = Tariff(float(os.getenv("PRICE_PER_HOUR", "100")))
    barrier = InMemoryBarrier()
    terminal = InMemoryPaymentTerminal()
    blocked = InMemoryBlockedList(
        [x.strip() for x in os.getenv("BLOCKED_PLATES", "").split(",") if x.strip()]
    )
    process = ParkingProcess(
        spots=spots, sessions=sessions, tariff=tariff, clock=clock,
        camera=InMemoryCamera(), barrier=barrier, terminal=terminal,
        blocked=blocked,
    )


    @app.errorhandler(Exception)
    def handle_error(error):
        if isinstance(error, TransitionNotAllowed):
            return jsonify(error="transition_not_allowed", message=str(error)), 409
        if isinstance(error, (DomainInvariantViolation, InvalidValueObject, ValueError, TypeError)):
            return jsonify(error="validation_error", message=str(error)), 422
        if isinstance(error, EntityNotFound):
            return jsonify(error="not_found", message=str(error)), 404
        if isinstance(error, ClockMovedBack):
            return jsonify(error="clock_moved_back", message=str(error)), 422
        if isinstance(error, GatewayUnresolved):
            return jsonify(error="gateway_unresolved", message=str(error)), 500
        if isinstance(error, OperatorInputRequired):
            return jsonify(error="operator_input_required", message=str(error)), 422
        if isinstance(error, DomainError):
            return jsonify(error="domain_error", message=str(error)), 422
        if isinstance(error, Exception):
            return jsonify(error="internal_error", message="Внутренняя ошибка сервера"), 500

    @app.get("/health")
    def health():
        return jsonify(status="ok"), 200

    @app.post("/sessions")
    def start_entry():
        data = request.get_json(silent=True) or {}
        session = process.start_entry(
            data.get("plate"),
            manual_plate=data.get("manual_plate"),
        )
        return jsonify(session_json(session)), 201

    @app.get("/sessions/<int:session_id>")
    def get_session(session_id):
        session = sessions.get(SessionId(session_id))
        return jsonify(session_json(session)), 200

    @app.post("/sessions/<int:session_id>/reject")
    def reject_entry(session_id):
        data = request.get_json(silent=True) or {}
        reason = data.get("reason")
        if reason not in ("Въезд запрещён: нет мест", "Въезд запрещён: номер заблокирован"):
            raise InvalidValueObject("Укажите допустимую причину отказа во въезде")
        return jsonify(session_json(process.reject_entry(SessionId(session_id), reason))), 200

    @app.post("/sessions/<int:session_id>/pay/app")
    def pay_in_app(session_id):
        payment = process.pay_in_app(SessionId(session_id))
        return jsonify(json_value(payment)), 200

    @app.post("/exit/recognize")
    def recognize_exit():
        data = request.get_json(silent=True) or {}
        session = process.recognize_plate_on_exit(
            data.get("plate"), manual_plate=data.get("manual_plate")
        )
        session = process.continue_exit(session.id)
        return jsonify(session_json(session)), 200

    @app.post("/sessions/<int:session_id>/pay/terminal")
    def pay_terminal(session_id):
        payment = process.accept_terminal_payment(SessionId(session_id))
        return jsonify(json_value(payment)), 200

    @app.post("/sessions/<int:session_id>/extra-payment/request")
    def request_extra_payment(session_id):
        amount = process.request_extra_payment(SessionId(session_id))
        return jsonify({"extra_due": amount.amount}), 200

    @app.post("/sessions/<int:session_id>/extra-payment")
    def accept_extra_payment(session_id):
        payment = process.accept_extra_payment(SessionId(session_id))
        return jsonify(json_value(payment)), 200

    @app.post("/sessions/<int:session_id>/exit/finish")
    def finish_exit(session_id):
        session = process.finish_exit(SessionId(session_id))
        return jsonify(session_json(session)), 200

    @app.post("/internal/tick")
    def tick():
        data = request.get_json(silent=True) or {}
        raw_now = data.get("now")
        if raw_now is not None:
            if not isinstance(raw_now, str):
                raise InvalidValueObject("Поле now должно быть строкой ISO 8601")
            try:
                target = datetime.fromisoformat(raw_now)
            except ValueError as exc:
                raise InvalidValueObject("Поле now должно быть временем ISO 8601") from exc
            now = clock.move_to(target)
        else:
            now = clock.now()

        # Обрабатываем все истёкшие таймеры 7 суток и 15 минут.
        long_stay = process.on_long_stay()
        extra_payment = []
        for session in sessions.all():
            if session.status == "Оплачена" and session.payment is not None:
                if now - session.payment.paid_at >= timedelta(minutes=15):
                    try:
                        process.request_extra_payment(session.id)
                        extra_payment.append(session.id.value)
                    except TransitionNotAllowed:
                        pass
        return jsonify({
            "now": now.isoformat(),
            "handed_to_operator": [s.id.value for s in long_stay],
            "requires_extra_payment": extra_payment,
        }), 200

    return app


app = create_app()
app.json.ensure_ascii = False

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.getenv("PORT", "5000")))
