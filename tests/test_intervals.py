"""Boundaries under each combination of `end_open` and `interval`.

One timeline is recorded four times, once per combination of how an open
interval marks its end (``NULL`` or a sentinel) and whether the interval is
half-open or closed. The versions abut on a shared instant, and every test
runs a query and asserts which versions come back: these two settings
change results silently rather than raising, so a test that reads the
configuration instead of executing it would pass with the comparison
inverted.
"""

from __future__ import annotations

import datetime as dt
from collections.abc import Iterable, Iterator
from typing import Any

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Mapped, mapped_column

from stele.runtime import Base, Binding, HistoryMixin, SCD2Config

D = dt.datetime

#: The first version ends and the second begins here. A closed interval
#: contains it twice over, a half-open one once.
BOUNDARY = D(2025, 1, 1)

FIRST_START = D(2024, 1, 1)
SECOND_END = D(2026, 1, 1)

#: After every start and long before the sentinel, so it means "the live
#: version" without depending on the wall clock.
LATE = D(2026, 6, 1)

SENTINEL = D(9999, 12, 31)


class BoundColumns:
    """The column shape all four history tables share."""

    EntityId: Mapped[int] = mapped_column(primary_key=True)
    StartDate: Mapped[dt.datetime] = mapped_column(primary_key=True)
    EndDate: Mapped[dt.datetime | None] = mapped_column()
    Label: Mapped[str] = mapped_column()


class BoundEntity(Base):
    """The live table the history tables below are versions of."""

    __tablename__ = "bound_entity"

    EntityId: Mapped[int] = mapped_column(primary_key=True)
    Label: Mapped[str]


class BoundNullHalfOpen(Base, BoundColumns, HistoryMixin):
    __tablename__ = "bound_null_half_open"

    __history_of__ = BoundEntity
    __scd2__ = SCD2Config(
        start_attr="StartDate",
        end_attr="EndDate",
        end_open="null",
        interval="half_open",
        business_key=("EntityId",),
    )


class BoundNullClosed(Base, BoundColumns, HistoryMixin):
    __tablename__ = "bound_null_closed"

    __history_of__ = BoundEntity
    __scd2__ = SCD2Config(
        start_attr="StartDate",
        end_attr="EndDate",
        end_open="null",
        interval="closed",
        business_key=("EntityId",),
    )


class BoundSentinelHalfOpen(Base, BoundColumns, HistoryMixin):
    __tablename__ = "bound_sentinel_half_open"

    __history_of__ = BoundEntity
    __scd2__ = SCD2Config(
        start_attr="StartDate",
        end_attr="EndDate",
        end_open="sentinel",
        end_sentinel=SENTINEL,
        interval="half_open",
        business_key=("EntityId",),
    )


class BoundSentinelClosed(Base, BoundColumns, HistoryMixin):
    __tablename__ = "bound_sentinel_closed"

    __history_of__ = BoundEntity
    __scd2__ = SCD2Config(
        start_attr="StartDate",
        end_attr="EndDate",
        end_open="sentinel",
        end_sentinel=SENTINEL,
        interval="closed",
        business_key=("EntityId",),
    )


#: Each class with the value its catalog writes in an open interval's end.
CONFIGURATIONS: list[tuple[Any, dt.datetime | None]] = [
    (BoundNullHalfOpen, None),
    (BoundNullClosed, None),
    (BoundSentinelHalfOpen, SENTINEL),
    (BoundSentinelClosed, SENTINEL),
]

IDS = [
    "null/half_open",
    "null/closed",
    "sentinel/half_open",
    "sentinel/closed",
]


def rows(cls: Any, open_end: dt.datetime | None) -> list[Any]:
    """Three abutting versions of one entity, plus an end nobody wrote.

    Entity 2 is the row a sentinel catalog is not supposed to contain: its
    end is ``NULL`` where the sentinel was expected.
    """
    return [
        cls(EntityId=1, Label="one", StartDate=FIRST_START, EndDate=BOUNDARY),
        cls(EntityId=1, Label="two", StartDate=BOUNDARY, EndDate=SECOND_END),
        cls(EntityId=1, Label="three", StartDate=SECOND_END, EndDate=open_end),
        cls(EntityId=2, Label="unended", StartDate=FIRST_START, EndDate=None),
    ]


@pytest.fixture
def binding() -> Iterator[Binding]:
    engine = create_engine("sqlite://")
    Base.metadata.create_all(
        engine,
        tables=[
            Base.metadata.tables[cls.__tablename__]
            for cls, _ in CONFIGURATIONS
        ],
    )
    b = Binding(engine=engine)
    with b.session() as s:
        for cls, open_end in CONFIGURATIONS:
            s.add_all(rows(cls, open_end))
    yield b
    engine.dispose()


def labels(found: Iterable[Any]) -> list[str]:
    return [row.Label for row in found]


# --- where the boundary instant falls --------------------------------------


@pytest.mark.parametrize(
    ("cls", "expected"),
    [
        (BoundNullHalfOpen, ["two"]),
        (BoundNullClosed, ["one", "two"]),
        (BoundSentinelHalfOpen, ["two"]),
        (BoundSentinelClosed, ["one", "two"]),
    ],
    ids=IDS,
)
def test_the_instant_two_versions_share(
    binding: Binding, cls: Any, expected: list[str]
) -> None:
    """A closed interval contains its end, so both versions hold it."""
    stmt = cls.as_of(BOUNDARY).where(cls.EntityId == 1).order_by(cls.StartDate)
    with binding.session() as s:
        assert labels(s.scalars(stmt)) == expected


@pytest.mark.parametrize(
    ("cls", "expected"),
    [
        (BoundNullHalfOpen, ["two"]),
        (BoundNullClosed, ["one", "two"]),
        (BoundSentinelHalfOpen, ["two"]),
        (BoundSentinelClosed, ["one", "two"]),
    ],
    ids=IDS,
)
def test_a_window_opening_where_a_version_ended(
    binding: Binding, cls: Any, expected: list[str]
) -> None:
    """`overlaps` reads the interval convention the same way `valid_at` does.

    The window is ``[BOUNDARY, SECOND_END)``. The first version ends where
    it opens, which overlaps only if the interval includes its end; the
    third version starts where it closes, which never overlaps.
    """
    stmt = (
        select(cls)
        .where(cls.overlaps(BOUNDARY, SECOND_END), cls.EntityId == 1)
        .order_by(cls.StartDate)
    )
    with binding.session() as s:
        assert labels(s.scalars(stmt)) == expected


# --- an end that was never written -----------------------------------------


@pytest.mark.parametrize("cls", [c for c, _ in CONFIGURATIONS], ids=IDS)
def test_an_end_left_null_is_open_in_either_mode(
    binding: Binding, cls: Any
) -> None:
    """A ``NULL`` end has no end, whichever marker the catalog uses.

    Reading it any other way puts the row in ``current()`` and in no
    instant at all, which is a row that exists and is valid nowhere.
    """
    with binding.session() as s:
        live = sorted(labels(s.scalars(cls.current())))
        at_late = sorted(labels(s.scalars(cls.as_of(LATE))))
    assert live == at_late == ["three", "unended"]


@pytest.mark.parametrize("cls", [c for c, _ in CONFIGURATIONS], ids=IDS)
def test_an_end_left_null_overlaps_a_window_after_its_start(
    binding: Binding, cls: Any
) -> None:
    stmt = select(cls).where(cls.overlaps(LATE, SENTINEL), cls.EntityId == 2)
    with binding.session() as s:
        assert labels(s.scalars(stmt)) == ["unended"]
