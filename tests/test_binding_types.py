"""`Binding.scalars` and `Binding.rows` carry the element type through.

The `assert_type` calls are the substance here: they are no-ops at run time
and are checked by mypy, which is what `./check.sh` runs them for. The round
trip below exists so the narrower signatures are known to still execute.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import assert_type

from sqlalchemy import Row, create_engine, select
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

from stele.runtime import Binding


class Base(DeclarativeBase):
    pass


class Widget(Base):
    __tablename__ = "widget"

    WidgetId: Mapped[int] = mapped_column(primary_key=True)
    WidgetName: Mapped[str]


def _binding() -> Binding:
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    return Binding(engine=engine)


def test_scalars_returns_the_selected_entity() -> None:
    binding = _binding()
    with binding.session() as s:
        s.add(Widget(WidgetId=1, WidgetName="v1"))

    found = binding.scalars(select(Widget))

    assert_type(found, list[Widget])
    assert [w.WidgetName for w in found] == ["v1"]


def test_scalars_of_a_single_column_returns_that_column() -> None:
    binding = _binding()
    with binding.session() as s:
        s.add(Widget(WidgetId=1, WidgetName="v1"))

    names = binding.scalars(select(Widget.WidgetName))

    assert_type(names, list[str])
    assert names == ["v1"]


def test_rows_keeps_the_width_of_the_select() -> None:
    binding = _binding()
    with binding.session() as s:
        s.add(Widget(WidgetId=1, WidgetName="v1"))

    pairs = binding.rows(select(Widget.WidgetId, Widget.WidgetName))

    assert_type(pairs, Sequence[Row[tuple[int, str]]])
    assert [tuple(r) for r in pairs] == [(1, "v1")]
