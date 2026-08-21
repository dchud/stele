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
from typing import Any, ClassVar, Literal, cast

from sqlalchemy import Select, and_, or_, select
from sqlalchemy.orm import InstrumentedAttribute


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


def normalize(ts: dt.datetime | dt.date | str, cfg: SCD2Config) -> dt.datetime:
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
    def _is_open(cls):
        cfg = cls.__scd2__
        end = cls._end()
        if cfg.end_open == "null":
            return end.is_(None)
        return or_(end.is_(None), end >= cfg.end_sentinel)

    @classmethod
    def valid_at(cls, ts: dt.datetime | dt.date | str):
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
    def overlaps(cls, start_ts, end_ts):
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
    def as_of(cls, ts: dt.datetime | dt.date | str) -> Select:
        """Every row as it stood at `ts`."""
        return select(cls).where(cls.valid_at(ts))

    @classmethod
    def current(cls) -> Select:
        """The currently-valid version of every row.

        If the live row is not duplicated into the history table, this queries
        the primary table instead, because the history table alone would be
        missing the newest version of every entity.
        """
        if not cls.__scd2__.current_in_history:
            return select(cls.__history_of__)
        return select(cls).where(cls._is_open())

    @classmethod
    def changes_between(cls, start_ts, end_ts) -> Select:
        """Versions that came into effect within [start_ts, end_ts)."""
        cfg = cls.__scd2__
        a, b = normalize(start_ts, cfg), normalize(end_ts, cfg)
        start = cls._start()
        return select(cls).where(and_(start >= a, start < b)).order_by(start)

    @classmethod
    def versions_of(cls, entity: Any) -> Select:
        """Full version history for one entity instance or key tuple."""
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
    def timeline(cls, entity: Any):
        """Alias for versions_of, reading better at call sites."""
        return cls.versions_of(entity)


def as_of_all(models: list[type[HistoryMixin]], ts) -> dict[str, Select]:
    """Build point-in-time snapshot selects for several history models."""
    return {m.__name__: m.as_of(ts) for m in models}
