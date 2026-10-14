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