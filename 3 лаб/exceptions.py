class DomainError(Exception):
    pass


class InvalidValueObject(DomainError):
    pass


class DomainInvariantViolation(DomainError):
    pass


class InsufficientCopiesError(DomainInvariantViolation):
    pass


class SpotAlreadyOccupiedError(DomainInvariantViolation):
    pass


class SessionAlreadyClosedError(DomainInvariantViolation):
    pass


class InvalidSessionTimeError(DomainInvariantViolation):
    pass


class TransitionNotAllowed(DomainError):
    """Недопустимый переход"""

    def __init__(self, state: str, event: str):
        self.state = state
        self.event = event
        super().__init__(f"Событие {event} недопустимо в состоянии «{state}»")


class GatewayUnresolved(DomainError):
    """Ни одно условие исключающего шлюза не выполнено и потока по умолчанию нет"""


class EntityNotFound(DomainError):
    pass


class ClockMovedBack(DomainError):
    pass


class OperatorInputRequired(DomainError):
    """Номер не распознан: нужна задача человека «Ввести номер в систему»."""
