"""The SCD2 selects name what they return, and `Binding` keeps it.

The `assert_type` calls are the substance: no-ops at run time, checked by
the mypy step. The round trip at the end runs the same calls so the
annotations are not the only thing standing behind them.
"""

from __future__ import annotations

import datetime as dt
from typing import Any, assert_type

from sqlalchemy import Select, create_engine
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

from stele.runtime import Binding, HistoryMixin, SCD2Config
from stele.runtime.history import as_of_all


class Base(DeclarativeBase):
    pass


class Widget(Base):
    __tablename__ = "widget"

    WidgetId: Mapped[int] = mapped_column(primary_key=True)
    WidgetName: Mapped[str]


class WidgetHistory(Base, HistoryMixin):
    __tablename__ = "widget_history"

    WidgetId: Mapped[int] = mapped_column(primary_key=True)
    WidgetName: Mapped[str]
    StartDate: Mapped[dt.datetime] = mapped_column(primary_key=True)
    EndDate: Mapped[dt.datetime | None]

    __history_of__ = Widget
    __scd2__ = SCD2Config(
        start_attr="StartDate",
        end_attr="EndDate",
        business_key=("WidgetId",),
    )


class Gadget(Base):
    __tablename__ = "gadget"

    GadgetId: Mapped[int] = mapped_column(primary_key=True)


class GadgetHistory(Base, HistoryMixin):
    __tablename__ = "gadget_history"

    GadgetId: Mapped[int] = mapped_column(primary_key=True)
    StartDate: Mapped[dt.datetime] = mapped_column(primary_key=True)
    EndDate: Mapped[dt.datetime | None]

    __history_of__ = Gadget
    __scd2__ = SCD2Config(
        start_attr="StartDate",
        end_attr="EndDate",
        business_key=("GadgetId",),
    )


AT = dt.datetime(2026, 3, 1)


def test_point_in_time_selects_name_their_element_type() -> None:
    assert_type(WidgetHistory.as_of(AT), Select[tuple[WidgetHistory]])
    assert_type(
        WidgetHistory.changes_between(AT, AT), Select[tuple[WidgetHistory]]
    )
    assert_type(WidgetHistory.versions_of((1,)), Select[tuple[WidgetHistory]])
    assert_type(WidgetHistory.timeline((1,)), Select[tuple[WidgetHistory]])


def test_current_stays_open_about_returning_either_class() -> None:
    """Which class `current` yields is a property of the model, not the call.

    With the live row absent from history it selects the primary table, so
    naming `WidgetHistory` here would be wrong for half the configurations.
    """
    assert_type(WidgetHistory.current(), Select[tuple[Any]])


def test_as_of_all_takes_a_mixed_list_and_says_nothing_about_elements() -> (
    None
):
    """Several models under string keys is a dict of several element types.

    A type variable over the argument would name the element type for a
    list of one class and reject the mixed list this exists to serve, so
    the mixed call is the behaviour worth pinning.
    """
    assert_type(
        as_of_all([WidgetHistory, GadgetHistory], AT),
        dict[str, Select[tuple[Any]]],
    )


def test_the_element_type_survives_the_binding() -> None:
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    binding = Binding(engine=engine)
    with binding.session() as s:
        s.add(
            WidgetHistory(
                WidgetId=1,
                WidgetName="v1",
                StartDate=dt.datetime(2026, 1, 1),
                EndDate=None,
            )
        )

    found = binding.scalars(WidgetHistory.as_of(AT))

    assert_type(found, list[WidgetHistory])
    assert [w.WidgetName for w in found] == ["v1"]
