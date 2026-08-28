"""SCD2 query helpers.

Every knob here corresponds to a modelling decision that produces *wrong
answers rather than errors* if guessed. They are read from the generated
``__scd2__`` descriptor rather than hardcoded:

  end_open      whether the live row's EndDate is NULL or a sentinel
  interval      [start, end) or [start, end]
  current_in_history
                whether the live row is duplicated into the history table
  naive_utc     normalise datetimes so as_of() means the same thing on
                Databricks (tz-aware TIMESTAMP) and SQL Server (naive
                datetime2)
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass
from typing import Any, ClassVar, Literal, Self, cast

from sqlalchemy import ColumnElement, Select, and_, or_, select
from sqlalchemy.orm import InstrumentedAttribute

# What every point-in-time helper accepts for an instant.
TimestampLike = dt.datetime | dt.date | str


@dataclass(frozen=True)
class SCD2Config:
    start_attr: str
    end_attr: str
    end_open: Literal["null", "sentinel"] = "null"
    end_sentinel: dt.datetime | None = None
    interval: Literal["half_open", "closed"] = "half_open"
    current_in_history: bool = True
    naive_utc: bool = True
    business_key: tuple[str, ...] = ()


def normalize(ts: TimestampLike, cfg: SCD2Config) -> dt.datetime:
    """Coerce an instant into the representation the columns actually use."""
    if isinstance(ts, str):
        ts = dt.datetime.fromisoformat(ts)
    elif isinstance(ts, dt.date) and not isinstance(ts, dt.datetime):
        ts = dt.datetime(ts.year, ts.month, ts.day)

    if cfg.naive_utc:
        if ts.tzinfo is not None:
            ts = ts.astimezone(dt.UTC).replace(tzinfo=None)
        return ts
    if ts.tzinfo is None:
        return ts.replace(tzinfo=dt.UTC)
    return ts


def utcnow(cfg: SCD2Config) -> dt.datetime:
    now = dt.datetime.now(dt.UTC)
    return now.replace(tzinfo=None) if cfg.naive_utc else now


class HistoryMixin:
    """Mixed into every generated ``*History`` class."""

    __scd2__: ClassVar[SCD2Config]
    __history_of__: ClassVar[type]

    # -- column accessors ------------------------------------------------

    @classmethod
    def _start(cls) -> InstrumentedAttribute[Any]:
        return cast(
            "InstrumentedAttribute[Any]",
            getattr(cls, cls.__scd2__.start_attr),
        )

    @classmethod
    def _end(cls) -> InstrumentedAttribute[Any]:
        return cast(
            "InstrumentedAttribute[Any]",
            getattr(cls, cls.__scd2__.end_attr),
        )

    # -- predicates ------------------------------------------------------

    @classmethod
    def _is_open(cls) -> ColumnElement[bool]:
        cfg = cls.__scd2__
        end = cls._end()
        if cfg.end_open == "null":
            return end.is_(None)
        return or_(end.is_(None), end >= cfg.end_sentinel)

    @classmethod
    def valid_at(cls, ts: TimestampLike) -> ColumnElement[bool]:
        """Predicate: this row's validity interval contains `ts`."""
        cfg = cls.__scd2__
        at = normalize(ts, cfg)
        start, end = cls._start(), cls._end()

        after_start = start <= at
        before_end = end > at if cfg.interval == "half_open" else end >= at

        if cfg.end_open == "null":
            before_end = or_(end.is_(None), before_end)
        return and_(after_start, before_end)

    @classmethod
    def overlaps(
        cls, start_ts: TimestampLike, end_ts: TimestampLike
    ) -> ColumnElement[bool]:
        """Predicate: interval overlaps [start_ts, end_ts)."""
        cfg = cls.__scd2__
        a, b = normalize(start_ts, cfg), normalize(end_ts, cfg)
        start, end = cls._start(), cls._end()
        upper = start < b
        lower = end > a
        if cfg.end_open == "null":
            lower = or_(end.is_(None), lower)
        return and_(upper, lower)

    # -- selects ---------------------------------------------------------

    @classmethod
    def as_of(cls, ts: TimestampLike) -> Select[tuple[Self]]:
        """Every row as it stood at `ts`."""
        return select(cls).where(cls.valid_at(ts))

    @classmethod
    def current(cls) -> Select[tuple[Any]]:
        """The currently-valid version of every row.

        If the live row is not duplicated into the history table, this queries
        the primary table instead, because the history table alone would be
        missing the newest version of every entity.

        That is also why this one select cannot name its element type the way
        its neighbours do: which class the rows belong to is a property of the
        model, not of the call. Bind the result yourself where it matters.
        """
        if not cls.__scd2__.current_in_history:
            return select(cls.__history_of__)
        return select(cls).where(cls._is_open())

    @classmethod
    def changes_between(
        cls, start_ts: TimestampLike, end_ts: TimestampLike
    ) -> Select[tuple[Self]]:
        """Versions that came into effect within [start_ts, end_ts)."""
        cfg = cls.__scd2__
        a, b = normalize(start_ts, cfg), normalize(end_ts, cfg)
        start = cls._start()
        return select(cls).where(and_(start >= a, start < b)).order_by(start)

    @classmethod
    def versions_of(cls, entity: Any) -> Select[tuple[Self]]:
        """Full version history for one entity instance or key tuple.

        ``entity`` stays untyped because the business key is read off it by
        name: an instance of either class, a tuple, a list, or a dict all
        work, and narrowing the parameter would reject callers that work.
        """
        cfg = cls.__scd2__
        if not cfg.business_key:
            raise ValueError(
                f"{cls.__name__} has no business_key configured; "
                "set the primary table's primary_key in the overlay"
            )
        if isinstance(entity, (tuple, list)):
            values = tuple(entity)
        elif isinstance(entity, dict):
            values = tuple(entity[k] for k in cfg.business_key)
        else:
            values = tuple(getattr(entity, k) for k in cfg.business_key)

        conds = [
            getattr(cls, k) == v
            for k, v in zip(cfg.business_key, values, strict=True)
        ]
        return select(cls).where(and_(*conds)).order_by(cls._start())

    @classmethod
    def timeline(cls, entity: Any) -> Select[tuple[Self]]:
        """Alias for versions_of, reading better at call sites."""
        return cls.versions_of(entity)


def as_of_all(
    models: list[type[HistoryMixin]], ts: TimestampLike
) -> dict[str, Select[tuple[Any]]]:
    """Build point-in-time snapshot selects for several history models.

    The element type stays open here, unlike the selects this is built
    from. The dict holds a different one under every key, and a type
    variable over the argument would name the element type only for a list
    of one class while rejecting the mixed list this exists to serve.
    """
    return {m.__name__: m.as_of(ts) for m in models}
