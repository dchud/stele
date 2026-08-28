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
from stele.infer import (
    FKProposal,
    apply_to_spec,
    infer,
    to_foreign_key_specs,
)
from stele.introspect import pair_history_tables, quote_ident
from stele.spec import (
    ColumnSpec,
    ForeignKeySpec,
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


def _paired_spec() -> ModelSpec:
    """A ledger whose parent versions, and a branch that does not."""
    ledger = [
        _col("LedgerId", "bigint", False, 1),
        _col("AccountId", "bigint", True, 2),
        _col("BranchId", "bigint", True, 3),
    ]
    account = [
        _col("AccountId", "bigint", False, 1),
        _col("AccountName", "string", True, 2),
        _col("ParentAccountId", "bigint", True, 3),
    ]

    def versioned(cols: list[ColumnSpec]) -> list[ColumnSpec]:
        return [
            *(ColumnSpec(**c.__dict__) for c in cols),
            _col("StartDate", "timestamp_ntz", False, 8),
            _col("EndDate", "timestamp_ntz", True, 9),
        ]

    s = ModelSpec(
        catalog="c",
        schemas=["dbo"],
        history=HistoryConfig(),
        tables=[
            TableSpec(name="Ledger", schema="dbo", columns=ledger),
            TableSpec(
                name="Ledger_history", schema="dbo", columns=versioned(ledger)
            ),
            TableSpec(name="Account", schema="dbo", columns=account),
            TableSpec(
                name="Account_history",
                schema="dbo",
                columns=versioned(account),
            ),
            TableSpec(
                name="Branch",
                schema="dbo",
                columns=[
                    _col("BranchId", "bigint", False, 1),
                    _col("BranchName", "string", True, 2),
                ],
            ),
        ],
    )
    pair_history_tables(s)
    apply_to_spec(s, infer(s, engine=None, validate=False))
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


def test_prefix_key_survives_a_plural_the_singulariser_mishandles() -> None:
    """`Boxes` reduces to `Boxe`, so no affix form of it spells `IdBox`.

    The stem fallback is what catches this, and it has to look at both
    ends of the column name for the same reason the affix rule does.
    """
    s = ModelSpec(
        catalog="c",
        schemas=["ops"],
        history=HistoryConfig(),
        tables=[
            TableSpec(
                name="Boxes",
                schema="ops",
                columns=[
                    _col("IdBox", "bigint", False, 1),
                    _col("BoxLabel", "string", True, 2),
                ],
            ),
            TableSpec(
                name="Shipment",
                schema="ops",
                columns=[
                    _col("IdShipment", "bigint", False, 1),
                    _col("IdBox", "bigint", True, 2),
                ],
            ),
        ],
    )

    result = infer(s, engine=None, validate=False)

    pks = {p.table: p.columns for p in result.primary_keys}
    assert pks["ops.Boxes"] == ["IdBox"]
    fks = [(f.table, f.columns, f.referred_table) for f in result.foreign_keys]
    assert ("ops.Shipment", ["IdBox"], "ops.Boxes") in fks


def _two_customers(child_schema: str, child_name: str) -> ModelSpec:
    """Two schemas each holding a `Customer`, plus a child pointing at one."""

    def customer(schema: str) -> TableSpec:
        return TableSpec(
            name="Customer",
            schema=schema,
            columns=[
                _col("CustomerId", "bigint", False, 1),
                _col("CustomerName", "string", True, 2),
            ],
        )

    return ModelSpec(
        catalog="c",
        schemas=["dbo", "ops", "sales"],
        history=HistoryConfig(),
        tables=[
            customer("dbo"),
            customer("ops"),
            TableSpec(
                name=child_name,
                schema=child_schema,
                columns=[
                    _col(f"{child_name}Id", "bigint", False, 1),
                    _col("CustomerId", "bigint", True, 2),
                ],
            ),
        ],
    )


def test_a_parent_in_the_childs_own_schema_wins() -> None:
    result = infer(_two_customers("dbo", "Order"), engine=None, validate=False)

    fks = [f for f in result.foreign_keys if f.table == "dbo.Order"]
    assert [f.referred_table for f in fks] == ["dbo.Customer"]
    assert fks[0].score == 0.8
    # The rejected twin is named, so the choice is reviewable rather than
    # silent.
    assert "ops.Customer" in fks[0].reason


def test_a_name_claimed_by_several_schemas_is_left_to_the_operator() -> None:
    result = infer(
        _two_customers("sales", "Invoice"), engine=None, validate=False
    )

    fks = sorted(
        (f for f in result.foreign_keys if f.table == "sales.Invoice"),
        key=lambda f: f.referred_table,
    )
    assert [f.referred_table for f in fks] == ["dbo.Customer", "ops.Customer"]
    assert [f.score for f in fks] == [0.4, 0.4]
    assert "ops.Customer" in fks[0].reason
    assert "dbo.Customer" in fks[1].reason
    # Below the default threshold, so neither is applied without a human.
    assert to_foreign_key_specs(result.foreign_keys, 0.6) == {}


def test_colliding_proposals_are_dropped_rather_than_picked_between() -> None:
    props = [
        FKProposal(
            table="sales.Invoice",
            columns=["CustomerId"],
            referred_table=parent,
            referred_columns=["CustomerId"],
            score=0.8,
            reason="name matches",
        )
        for parent in ("dbo.Customer", "ops.Customer")
    ]

    assert to_foreign_key_specs(props, 0.5) == {}


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


def test_history_reaches_a_parent_that_does_not_version(
    models: ModuleType,
) -> None:
    """Owner has no history table, so its current row is its only state."""
    rel = models.WidgetHistory.__mapper__.relationships["owner"]
    assert rel.mapper.class_ is models.Owner
    assert rel.uselist is False
    assert rel.viewonly is True


def test_history_reaches_a_parent_that_versions(
    tmp_path: Path,
) -> None:
    """A versioned parent is reached through its own history class.

    Which version comes back is the session's business, not the join's, so
    the relationship carries no interval predicate at all.
    """
    from stele.generate import Generator

    spec = _paired_spec()
    gen = Generator(spec)
    modules = gen.build_modules()
    hist = next(
        c
        for m in modules
        for c in m.classes
        if c.class_name == "LedgerHistory"
    )
    rels = {r.attr: r for r in hist.relationships}

    assert rels["account"].target_class == "AccountHistory"
    assert rels["account"].uselist is False
    assert rels["account"].viewonly is True
    join = rels["account"].primaryjoin or ""
    assert "foreign(AccountHistory.AccountId)" in join
    assert "StartDate" not in join

    assert rels["branch"].target_class == "Branch"


def test_a_self_reference_configures(tmp_path: Path) -> None:
    """Both ends of a self-join sit on one table; remote_side names the parent."""
    spec = _paired_spec()
    account = spec.table("dbo.Account")
    assert account is not None
    account.foreign_keys.append(
        ForeignKeySpec(
            columns=["ParentAccountId"],
            referred_table="dbo.Account",
            referred_columns=["AccountId"],
            origin="manual",
            relationship_name="parent",
            backref_name="children",
        )
    )
    pair_history_tables(spec)
    generate(spec, tmp_path / "selfref")
    sys.path.insert(0, str(tmp_path))
    mod = importlib.import_module("selfref")
    configure_mappers()

    assert mod.Account.__mapper__.relationships["parent"].mapper.class_ is (
        mod.Account
    )
    # the history side needs no remote_side; foreign() already says which
    # end is dependent
    assert (
        mod.AccountHistory.__mapper__.relationships["parent"].mapper.class_
        is mod.AccountHistory
    )


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
