"""Smoke tests covering the spec -> generate -> map path and the SCD2 helpers."""

from __future__ import annotations

import datetime as dt
import importlib
import sys
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, configure_mappers

from stele.generate import generate, pascal, plural, snake
from stele.infer import apply_to_spec, infer
from stele.introspect import pair_history_tables, quote_ident
from stele.spec import (
    ColumnSpec,
    HistoryConfig,
    ModelSpec,
    TableSpec,
    spec_from_dict,
)
from stele.types import bucket_length, resolve


def _col(
    name: str,
    type_: str,
    nullable: bool = True,
    ordinal: int = 0,
    **kw: Any,
) -> ColumnSpec:
    return ColumnSpec(
        name=name, source_type=type_, nullable=nullable, ordinal=ordinal, **kw
    )


def _demo_spec() -> ModelSpec:
    cols = [
        _col("WidgetId", "bigint", False, 1),
        _col("WidgetName", "string", True, 2),
        _col("OwnerId", "bigint", True, 3),
    ]
    hist = [
        *(ColumnSpec(**c.__dict__) for c in cols),
        _col("StartDate", "timestamp_ntz", False, 4),
        _col("EndDate", "timestamp_ntz", True, 5),
    ]
    owner = [
        _col("OwnerId", "bigint", False, 1),
        _col("OwnerName", "string", True, 2),
    ]
    s = ModelSpec(
        catalog="c",
        schemas=["dbo"],
        history=HistoryConfig(),
        tables=[
            TableSpec(name="Widget", schema="dbo", columns=cols),
            TableSpec(name="Widget_history", schema="dbo", columns=hist),
            TableSpec(name="Owner", schema="dbo", columns=owner),
        ],
    )
    pair_history_tables(s)
    return s


@pytest.fixture
def spec() -> ModelSpec:
    return _demo_spec()


# -- naming ---------------------------------------------------------------


@pytest.mark.parametrize(
    "src,want",
    [
        ("Customer", "Customer"),
        ("customer_order", "CustomerOrder"),
        ("ETL_log", "ETLLog"),
    ],
)
def test_pascal(src: str, want: str) -> None:
    assert pascal(src) == want


@pytest.mark.parametrize(
    "src,want",
    [
        ("CustomerOrder", "customer_order"),
        ("ETLLog", "etl_log"),
        ("Widget", "widget"),
    ],
)
def test_snake(src: str, want: str) -> None:
    assert snake(src) == want


@pytest.mark.parametrize(
    "src,want",
    [
        ("customer", "customers"),
        ("box", "boxes"),
        ("company", "companies"),
        ("day", "days"),
    ],
)
def test_plural(src: str, want: str) -> None:
    assert plural(src) == want


# -- types ----------------------------------------------------------------


def test_decimal_keeps_precision() -> None:
    rt = resolve(_col("x", "decimal(18,4)"))
    assert "Numeric(precision=18, scale=4" in rt.expression
    assert rt.python_type == "decimal.Decimal"


def test_string_without_length_is_max() -> None:
    rt = resolve(_col("x", "string"))
    assert "NVARCHAR(None)" in rt.expression
    assert rt.note is not None
    assert "no length known" in rt.note


def test_profiled_string_gets_bucketed_length() -> None:
    rt = resolve(_col("x", "string", observed_max_length=43))
    assert "String(50)" in rt.expression and "NVARCHAR(50)" in rt.expression


def test_type_override_wins() -> None:
    rt = resolve(_col("x", "string", type_override="NVARCHAR(12)"))
    assert rt.expression == "NVARCHAR(12)"


def test_complex_type_flagged_lossy() -> None:
    rt = resolve(_col("x", "array<string>"))
    assert rt.lossy and "JSON" in rt.expression


@pytest.mark.parametrize(
    "n,want", [(1, 10), (43, 50), (255, 255), (9000, None), (None, None)]
)
def test_bucket_length(n: int | None, want: int | None) -> None:
    assert bucket_length(n) == want


# -- introspection --------------------------------------------------------


def test_history_pairing(spec: ModelSpec) -> None:
    widget = spec.table("dbo.Widget")
    hist = spec.table("dbo.Widget_history")
    assert widget is not None and hist is not None
    assert widget.history_table == "dbo.Widget_history"
    assert hist.history_of == "dbo.Widget"


def test_quote_ident_rejects_backtick() -> None:
    with pytest.raises(ValueError):
        quote_ident("bad`name")


def test_spec_roundtrip(spec: ModelSpec) -> None:
    import dataclasses

    again = spec_from_dict(dataclasses.asdict(spec))
    assert [t.key for t in again.tables] == [t.key for t in spec.tables]


# -- inference ------------------------------------------------------------


def test_infer_proposes_keys_and_relationships(spec: ModelSpec) -> None:
    result = infer(spec, engine=None, validate=False)
    pks = {p.table: p.columns for p in result.primary_keys}
    assert pks["dbo.Widget"] == ["WidgetId"]
    assert pks["dbo.Owner"] == ["OwnerId"]
    fks = [(f.table, f.columns, f.referred_table) for f in result.foreign_keys]
    assert ("dbo.Widget", ["OwnerId"], "dbo.Owner") in fks


def test_key_names_are_recognised_either_side_of_the_table_name() -> None:
    """One catalog can hold both naming conventions, one schema each."""
    suffixed = [
        TableSpec(
            name="Widget",
            schema="dbo",
            columns=[
                _col("WidgetId", "bigint", False, 1),
                _col("WidgetName", "string", True, 2),
            ],
        ),
        TableSpec(
            name="Order",
            schema="dbo",
            columns=[
                _col("OrderId", "bigint", False, 1),
                _col("WidgetId", "bigint", True, 2),
            ],
        ),
    ]
    prefixed = [
        TableSpec(
            name="Gadget",
            schema="ops",
            columns=[
                _col("IdGadget", "bigint", False, 1),
                _col("GadgetName", "string", True, 2),
            ],
        ),
        TableSpec(
            name="Shipment",
            schema="ops",
            columns=[
                _col("IdShipment", "bigint", False, 1),
                _col("IdGadget", "bigint", True, 2),
            ],
        ),
    ]
    s = ModelSpec(
        catalog="c",
        schemas=["dbo", "ops"],
        history=HistoryConfig(),
        tables=[*suffixed, *prefixed],
    )

    result = infer(s, engine=None, validate=False)

    pks = {p.table: p.columns for p in result.primary_keys}
    assert pks["dbo.Widget"] == ["WidgetId"]
    assert pks["ops.Gadget"] == ["IdGadget"]
    assert pks["ops.Shipment"] == ["IdShipment"]

    # A key the proposer misses is a key no relationship can point at, so
    # the foreign keys are the part worth asserting.
    fks = [(f.table, f.columns, f.referred_table) for f in result.foreign_keys]
    assert ("dbo.Order", ["WidgetId"], "dbo.Widget") in fks
    assert ("ops.Shipment", ["IdGadget"], "ops.Gadget") in fks


def test_history_business_key_follows_primary(spec: ModelSpec) -> None:
    infer(spec, engine=None, validate=False)
    pair_history_tables(spec)
    hist = spec.table("dbo.Widget_history")
    assert hist is not None
    assert hist.primary_key == ["WidgetId", "StartDate"]


# -- generation + runtime -------------------------------------------------


@pytest.fixture(scope="module")
def models(tmp_path_factory: pytest.TempPathFactory) -> ModuleType:
    """Module-scoped: generated classes register on the shared declarative
    registry, so importing the same package twice would collide."""
    s = _demo_spec()
    apply_to_spec(s, infer(s, engine=None, validate=False))
    pair_history_tables(s)
    out = tmp_path_factory.mktemp("gen")
    generate(s, out / "m")
    sys.path.insert(0, str(out))
    mod = importlib.import_module("m")
    configure_mappers()
    return mod


def test_generated_package_maps(models: ModuleType) -> None:
    assert models.Widget.__tablename__ == "Widget"
    assert models.WidgetHistory.__history_of__ is models.Widget
    assert models.WidgetHistory.__scd2__.business_key == ("WidgetId",)


def test_schema_token_not_literal(models: ModuleType) -> None:
    assert models.Widget.__table__.schema == "stele__dbo"


def test_scd2_queries(models: ModuleType) -> None:
    engine = create_engine("sqlite://").execution_options(
        schema_translate_map={models.SCHEMA_DBO: None}
    )
    models.metadata.create_all(engine)
    W, H = models.Widget, models.WidgetHistory
    with Session(engine) as s:
        s.add(models.Owner(OwnerId=1, OwnerName="o"))
        s.add(W(WidgetId=1, WidgetName="now", OwnerId=1))
        s.add(
            H(
                WidgetId=1,
                WidgetName="v1",
                OwnerId=1,
                StartDate=dt.datetime(2026, 1, 1),
                EndDate=dt.datetime(2026, 5, 1),
            )
        )
        s.add(
            H(
                WidgetId=1,
                WidgetName="v2",
                OwnerId=1,
                StartDate=dt.datetime(2026, 5, 1),
                EndDate=None,
            )
        )
        s.commit()

    with Session(engine) as s:
        assert (
            s.scalars(H.as_of(dt.datetime(2026, 3, 1))).one().WidgetName
            == "v1"
        )
        assert (
            s.scalars(H.as_of(dt.datetime(2026, 6, 1))).one().WidgetName
            == "v2"
        )
        assert s.scalars(H.current()).one().WidgetName == "v2"
        # half-open: the boundary instant belongs to the later version
        assert (
            s.scalars(H.as_of(dt.datetime(2026, 5, 1))).one().WidgetName
            == "v2"
        )
        widget = s.get(W, 1)
        assert widget is not None
        assert len(s.scalars(H.versions_of(widget)).all()) == 2
        assert [h.WidgetName for h in widget.history] == ["v1", "v2"]
        assert widget.owner.OwnerName == "o"


def test_closed_interval_changes_boundary(
    spec: ModelSpec, tmp_path: Path
) -> None:
    spec.history.interval = "closed"
    apply_to_spec(spec, infer(spec, engine=None, validate=False))
    pair_history_tables(spec)
    generate(spec, tmp_path / "m2")
    text = (tmp_path / "m2" / "widget.py").read_text()
    assert 'interval="closed"' in text


def test_replica_ddl_resolves_schema_tokens(models: ModuleType) -> None:
    from stele.runtime import replica_ddl

    sql = replica_ddl(
        models.metadata, dialect_name="mssql", schemas={"dbo": "dbo"}
    )
    assert "stele__dbo" not in sql
    assert "NVARCHAR" in sql and "dbo.[Widget]" in sql
