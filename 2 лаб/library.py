from dataclasses import dataclass
from datetime import date, timedelta
from typing import Optional, Protocol

import pytest

from exceptions import DomainInvariantViolation, InsufficientCopiesError, InvalidValueObject


#  Value Objects 

@dataclass(frozen=True)
class BookId:
    value: int

    def __post_init__(self):
        if self.value <= 0:
            raise InvalidValueObject("BookId > 0")


@dataclass(frozen=True)
class CopyId:
    value: int

    def __post_init__(self):
        if self.value <= 0:
            raise InvalidValueObject("CopyId > 0")


@dataclass(frozen=True)
class LoanId:
    value: int

    def __post_init__(self):
        if self.value <= 0:
            raise InvalidValueObject("LoanId > 0")


@dataclass(frozen=True)
class ReaderId:
    value: int

    def __post_init__(self):
        if self.value <= 0:
            raise InvalidValueObject("ReaderId > 0")


@dataclass(frozen=True)
class LoanPeriod:
    days: int

    def __post_init__(self):
        if self.days <= 0:
            raise InvalidValueObject("Срок выдачи > 0")


@dataclass(frozen=True)
class Money:
    amount: float

    def __post_init__(self):
        if self.amount < 0:
            raise InvalidValueObject("Сумма ≥ 0")


#  Агрегаты 

class Book:
    """Агрегаткорень Book"""

    _next_copy_id = 1

    def __init__(self, book_id: BookId, title: str, total_copies: int):
        if total_copies < 1:
            raise InvalidValueObject("Тираж ≥ 1")
        self._id = book_id
        self._title = title
        self._total_copies = total_copies
        # список: (copy_id, is_available)
        self._copies = []
        for _ in range(total_copies):
            self._copies.append((CopyId(Book._next_copy_id), True))
            Book._next_copy_id += 1

    @property
    def id(self):
        return self._id

    @property
    def available_copies(self):
        return sum(1 for _, free in self._copies if free)

    def take_copy(self) -> CopyId:
        if self.available_copies == 0:
            raise InsufficientCopiesError("Нет свободных экземпляров")
        for i, (cid, free) in enumerate(self._copies):
            if free:
                self._copies[i] = (cid, False)
                return cid
        raise InsufficientCopiesError("Нет свободных экземпляров")

    def return_copy(self, copy_id: CopyId):
        for i, (cid, free) in enumerate(self._copies):
            if cid == copy_id:
                self._copies[i] = (cid, True)
                return
        raise DomainInvariantViolation("Экземпляр не найден")


class Loan:
    """Агрегаткорень Loan. Связь с Book — только через copy_id"""

    FINE_PER_DAY = 10.0

    def __init__(self, loan_id: LoanId, copy_id: CopyId, reader_id: ReaderId,
                 issue_date: date, period: LoanPeriod):
        self._id = loan_id
        self._copy_id = copy_id
        self._reader_id = reader_id
        self._due_date = issue_date + timedelta(days=period.days)
        self._return_date: Optional[date] = None
        self._fine: Optional[Money] = None

    @property
    def id(self):
        return self._id

    @property
    def copy_id(self):
        return self._copy_id

    @property
    def fine(self):
        return self._fine

    def return_book(self, return_date: date) -> Money:
        if self._return_date is not None:
            raise DomainInvariantViolation("Уже возвращена")
        self._return_date = return_date
        if return_date > self._due_date:
            days_late = (return_date - self._due_date).days
            self._fine = Money(days_late * self.FINE_PER_DAY)
        else:
            self._fine = Money(0.0)
        return self._fine


#  Репозитории (Protocol + inmemory) 

class BookRepository(Protocol):
    def save(self, book: Book) -> None: ...


class LoanRepository(Protocol):
    def save(self, loan: Loan) -> None: ...


class InMemoryBookRepo:
    def __init__(self):
        self._data = {}

    def save(self, book: Book):
        self._data[book.id] = book


class InMemoryLoanRepo:
    def __init__(self):
        self._data = {}

    def save(self, loan: Loan):
        self._data[loan.id] = loan


#  Доменный сервис (затрагивает 2 агрегата) 

class LendingService:
    def __init__(self, books: BookRepository, loans: LoanRepository):
        self._books = books
        self._loans = loans

    def lend(self, book: Book, reader_id: ReaderId, period: LoanPeriod, issue_date: date) -> Loan:
        copy_id = book.take_copy()
        loan = Loan(LoanId(1), copy_id, reader_id, issue_date, period)
        self._books.save(book)
        self._loans.save(loan)
        return loan

    def return_loan(self, loan: Loan, book: Book, return_date: date):
        loan.return_book(return_date)
        book.return_copy(loan.copy_id)
        self._loans.save(loan)
        self._books.save(book)


#  Тест 

def test_library():
    books = InMemoryBookRepo()
    loans = InMemoryLoanRepo()
    svc = LendingService(books, loans)

    book = Book(BookId(1), "DDD", total_copies=1)
    books.save(book)

    loan = svc.lend(book, ReaderId(1), LoanPeriod(7), date(2026, 1, 1))
    assert book.available_copies == 0

    with pytest.raises(InsufficientCopiesError):
        svc.lend(book, ReaderId(2), LoanPeriod(7), date(2026, 1, 2))

    svc.return_loan(loan, book, date(2026, 1, 15))  # опоздание 7 дней
    assert book.available_copies == 1
    assert loan.fine.amount == 70.0