"""Smoke tests covering the spec -> generate -> map path and the SCD2 helpers."""

from __future__ import annotations

import datetime as dt
import importlib
import shutil
import subprocess
import sys
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest
from sqlalchemy import create_engine, event, select
from sqlalchemy.orm import Session, configure_mappers
from sqlalchemy.pool import StaticPool

from stele.generate import (
    Generator,
    generate,
    pascal,
    plural,
    safe_attr,
    snake,
)
from stele.infer import (
    FKProposal,
    apply_to_spec,
    infer,
    to_foreign_key_specs,
    validate_declared,
)
from stele.introspect import pair_history_tables, quote_ident
from stele.runtime import Binding
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


@pytest.mark.parametrize(
    "src",
    [
        "class",
        "except",
        "lambda",
        "global",
        "del",
        "try",
        "raise",
        "yield",
        "async",
        "None",
    ],
)
def test_safe_attr_suffixes_a_keyword(src: str) -> None:
    assert safe_attr(src) == src + "_"


@pytest.mark.parametrize(
    "src", ["metadata", "registry", "to_dict", "id", "type"]
)
def test_safe_attr_suffixes_a_name_the_base_class_claims(src: str) -> None:
    assert safe_attr(src) == src + "_"


@pytest.mark.parametrize(
    "src,want",
    [
        ("Unit Price", "Unit_Price"),
        ("my-col", "my_col"),
        ("unit%", "unit_"),
        ("2fast", "_2fast"),
        ("", "_"),
        ("%%%", "___"),
        ("café", "café"),
    ],
)
def test_safe_attr_rewrites_what_is_not_an_identifier(
    src: str, want: str
) -> None:
    assert safe_attr(src) == want
    assert want.isidentifier()


@pytest.mark.parametrize("src", ["WidgetId", "widget_id", "_private", "x2"])
def test_safe_attr_leaves_a_usable_name_alone(src: str) -> None:
    assert safe_attr(src) == src


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


@pytest.mark.parametrize(
    "override,expr,mssql",
    [
        ("nvarchar(50)", "NVARCHAR(50)", ["NVARCHAR"]),
        ("NVARCHAR(50)", "NVARCHAR(50)", ["NVARCHAR"]),
        ("datetime2(6)", "DATETIME2(6)", ["DATETIME2"]),
    ],
)
def test_a_type_override_is_imported_by_its_real_name(
    override: str, expr: str, mssql: list[str]
) -> None:
    """An overlay is hand-written, so the case it uses is whatever was typed."""
    rt = resolve(_col("x", "string", type_override=override))
    assert rt.expression == expr
    assert sorted(rt.mssql_imports) == mssql


def test_a_spec_from_a_newer_stele_is_refused() -> None:
    """Keys this version does not know would be dropped without a word."""
    with pytest.raises(ValueError, match="spec_version"):
        spec_from_dict({"spec_version": 99, "catalog": "c", "schemas": []})


@pytest.mark.parametrize(
    "observed,fragment",
    [
        (None, "run `stele profile`"),
        (0, "every sampled value was empty"),
        (9000, "exceeds 4000"),
    ],
)
def test_a_string_without_a_width_says_why(
    observed: int | None, fragment: str
) -> None:
    """Telling someone to run the profile they just ran is the one useless answer."""
    rt = resolve(_col("x", "string", observed_max_length=observed))
    assert "NVARCHAR(None)" in rt.expression
    assert rt.note is not None and fragment in rt.note


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


def _two_candidate_spec() -> ModelSpec:
    """A table an affix match and a generic key name both claim."""
    return ModelSpec(
        catalog="c",
        schemas=["dbo"],
        history=HistoryConfig(),
        tables=[
            TableSpec(
                name="Gauge",
                schema="dbo",
                columns=[
                    _col("GaugeId", "bigint", False, 1),
                    _col("Id", "bigint", False, 2),
                    _col("GaugeName", "string", True, 3),
                ],
            )
        ],
    )


#: `validate_primary_key` only ever hands the engine to `_one`, which the
#: tests below replace, so nothing here touches a driver.
_ENGINE: Any = object()


def _fake_one(rejected: set[str]) -> Any:
    """Stand in for the validation query: name a column to fail it."""

    def one(engine: Any, sql: str) -> dict[str, int]:
        bad = any(f"`{c}`" in sql or f".{c}" in sql for c in rejected)
        return {
            "total_rows": 100,
            "null_rows": 2 if bad else 0,
            "duplicate_groups": 3 if bad else 0,
        }

    return one


def _fake_fk_queries(orphans: list[str], matched: int = 8) -> Any:
    """Stand in for the three shapes an FK check reads back.

    `validate_foreign_key` runs a containment query, a null-fraction query,
    and, only where values went unmatched, a query for examples of them.
    """

    def one(engine: Any, sql: str) -> dict[str, Any]:
        if "distinct_values" in sql:
            return {"distinct_values": 10, "matched_values": matched}
        return {"total": 100, "nulls": 4}

    def rows(engine: Any, sql: str) -> list[dict[str, Any]]:
        return [{"v": o} for o in orphans]

    return one, rows


def _fk_spec() -> ModelSpec:
    s = _demo_spec()
    widget = s.table("dbo.Widget")
    owner = s.table("dbo.Owner")
    assert widget is not None and owner is not None
    owner.primary_key = ["OwnerId"]
    widget.primary_key = ["WidgetId"]
    widget.foreign_keys.append(
        ForeignKeySpec(
            columns=["OwnerId"],
            referred_table="dbo.Owner",
            referred_columns=["OwnerId"],
            origin="manual",
        )
    )
    return s


def test_unmatched_values_are_named(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A ratio says something failed; the values say what to do about it."""
    one, rows = _fake_fk_queries(["9931", "9932"])
    monkeypatch.setattr("stele.infer._one", one)
    monkeypatch.setattr("stele.infer._rows", rows)

    (declared,) = validate_declared(_fk_spec(), _ENGINE)

    assert declared.orphan_examples == ["9931", "9932"]
    assert "9931" in declared.reason


def test_a_clean_reference_asks_for_no_examples(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The example query is the one round trip a clean check does not make."""
    one, _ = _fake_fk_queries([], matched=10)
    monkeypatch.setattr("stele.infer._one", one)

    def forbidden(engine: Any, sql: str) -> list[dict[str, Any]]:
        raise AssertionError("asked for examples with nothing unmatched")

    monkeypatch.setattr("stele.infer._rows", forbidden)

    (declared,) = validate_declared(_fk_spec(), _ENGINE)

    assert declared.containment == 1.0
    assert declared.orphan_examples == []


def test_a_declared_reference_is_checked_against_the_data(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Name matching cannot propose every reference, so it cannot check them."""
    one, rows = _fake_fk_queries(["77"])
    monkeypatch.setattr("stele.infer._one", one)
    monkeypatch.setattr("stele.infer._rows", rows)
    spec = _fk_spec()
    widget = spec.table("dbo.Widget")
    assert widget is not None
    # A reference no name matching would find: the column names disagree.
    widget.columns.append(_col("Custodian", "bigint", True, 9))
    widget.foreign_keys.append(
        ForeignKeySpec(
            columns=["Custodian"],
            referred_table="dbo.Owner",
            referred_columns=["OwnerId"],
            origin="manual",
        )
    )

    checked = validate_declared(spec, _ENGINE)

    assert [c.columns for c in checked] == [["OwnerId"], ["Custodian"]]
    assert all(c.containment == 0.8 for c in checked)


def test_a_rejected_primary_key_falls_back_to_the_next_candidate(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Losing the top candidate must not cost the table its key."""
    monkeypatch.setattr("stele.infer._one", _fake_one({"GaugeId"}))
    spec = _two_candidate_spec()
    result = infer(spec, engine=_ENGINE, validate=True)

    (pk,) = result.primary_keys
    assert pk.columns == ["Id"]
    assert pk.verified
    apply_to_spec(spec, result)
    assert spec.table("dbo.Gauge").primary_key == ["Id"]  # type: ignore[union-attr]


def test_the_best_candidate_is_kept_when_it_validates(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The fallback runs only on rejection; a clean top candidate wins."""
    monkeypatch.setattr("stele.infer._one", _fake_one(set()))
    result = infer(_two_candidate_spec(), engine=_ENGINE, validate=True)

    (pk,) = result.primary_keys
    assert pk.columns == ["GaugeId"]
    assert pk.verified


def test_every_candidate_rejected_reports_the_best_one(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The operator gets the strongest candidate and the counts that sank it."""
    monkeypatch.setattr("stele.infer._one", _fake_one({"GaugeId", "Id"}))
    spec = _two_candidate_spec()
    result = infer(spec, engine=_ENGINE, validate=True)

    (pk,) = result.primary_keys
    assert pk.columns == ["GaugeId"]
    assert not pk.verified
    assert "REJECTED" in pk.reason
    assert spec.table("dbo.Gauge").primary_key == []  # type: ignore[union-attr]


def test_infer_leaves_the_callers_spec_alone(spec: ModelSpec) -> None:
    """Proposing is not applying, and the count depends on the difference."""
    result = infer(spec, engine=None, validate=False)
    assert [t.primary_key for t in spec.tables] == [[], [], []]

    # Two keys and the reference between them; with infer applying the keys
    # itself, the two would not have been counted.
    assert apply_to_spec(spec, result) == 3
    assert spec.table("dbo.Widget").primary_key == ["WidgetId"]  # type: ignore[union-attr]
    # A second pass has nothing left to do.
    assert apply_to_spec(spec, result) == 0


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
    apply_to_spec(spec, infer(spec, engine=None, validate=False))
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


def _composite_spec() -> ModelSpec:
    """A parent keyed on two columns, and a child referencing the pair.

    Distinct table names because every generated package in this file shares
    one declarative registry.
    """
    return ModelSpec(
        catalog="c",
        schemas=["dbo"],
        history=HistoryConfig(),
        tables=[
            TableSpec(
                name="District",
                schema="dbo",
                columns=[
                    _col("RegionId", "bigint", False, 1),
                    _col("DistrictId", "bigint", False, 2),
                    _col("DistrictName", "string", True, 3),
                ],
                primary_key=["RegionId", "DistrictId"],
            ),
            TableSpec(
                name="Posting",
                schema="dbo",
                columns=[
                    _col("PostingId", "bigint", False, 1),
                    _col("RegionId", "bigint", True, 2),
                    _col("DistrictId", "bigint", True, 3),
                ],
                primary_key=["PostingId"],
                foreign_keys=[
                    ForeignKeySpec(
                        columns=["RegionId", "DistrictId"],
                        referred_table="dbo.District",
                        referred_columns=["RegionId", "DistrictId"],
                        origin="manual",
                    )
                ],
            ),
        ],
    )


def test_a_composite_reference_is_one_constraint(tmp_path: Path) -> None:
    """Per column it would claim each column references the parent alone."""
    generate(_composite_spec(), tmp_path / "comp")
    text = (tmp_path / "comp" / "posting.py").read_text(encoding="utf-8")

    assert "ForeignKeyConstraint(" in text
    assert '["RegionId", "DistrictId"]' in text
    assert "ForeignKey(f" not in text


def test_a_composite_reference_maps_and_emits_one_clause(
    tmp_path: Path,
) -> None:
    """Referencing one column of a composite key is invalid on the replica."""
    from stele.runtime import replica_ddl

    generate(_composite_spec(), tmp_path / "compref")
    sys.path.insert(0, str(tmp_path))
    mod = importlib.import_module("compref")
    configure_mappers()

    assert (
        mod.Posting.__mapper__.relationships["district"].mapper.class_
        is mod.District
    )

    sql = replica_ddl(
        mod.metadata, dialect_name="mssql", schemas={"dbo": "dbo"}
    )
    # The registry is shared across generated packages in this file, so look
    # only at the constraint this one contributes.
    clauses = [
        ln
        for ln in sql.splitlines()
        if "FOREIGN KEY" in ln and "District" in ln
    ]
    assert len(clauses) == 1
    assert "FOREIGN KEY([RegionId], [DistrictId])" in clauses[0]


def test_composite_keyed_tables_are_reported(spec: ModelSpec) -> None:
    """Their absence from the proposals is otherwise silent."""
    from stele.infer import composite_key_tables

    assert composite_key_tables(spec) == []

    composite = _composite_spec()
    assert composite_key_tables(composite) == [
        ("dbo.District", ["RegionId", "DistrictId"])
    ]


def _named_spec(
    name: str, child_cols: list[ColumnSpec], fks: list[ForeignKeySpec]
) -> ModelSpec:
    """A parent and a child, with table names unique to one test.

    Generated classes share one declarative registry across this file, so a
    name reused between tests configures the wrong mapper.
    """
    return ModelSpec(
        catalog="c",
        schemas=["dbo"],
        history=HistoryConfig(),
        tables=[
            TableSpec(
                name=f"{name}Party",
                schema="dbo",
                primary_key=[f"{name}PartyId"],
                columns=[_col(f"{name}PartyId", "bigint", False, 1)],
            ),
            TableSpec(
                name=f"{name}Deal",
                schema="dbo",
                primary_key=[f"{name}DealId"],
                columns=child_cols,
                foreign_keys=fks,
            ),
        ],
    )


def test_two_references_to_one_parent_configure(tmp_path: Path) -> None:
    """Buyer and seller on one table is an ordinary shape, not an edge case."""
    spec = _named_spec(
        "Two",
        [
            _col("TwoDealId", "bigint", False, 1),
            _col("BuyerId", "bigint", True, 2),
            _col("SellerId", "bigint", True, 3),
        ],
        [
            ForeignKeySpec(
                columns=[c],
                referred_table="dbo.TwoParty",
                referred_columns=["TwoPartyId"],
                origin="manual",
            )
            for c in ("BuyerId", "SellerId")
        ],
    )
    generate(spec, tmp_path / "twofk")
    sys.path.insert(0, str(tmp_path))
    mod = importlib.import_module("twofk")
    configure_mappers()

    rels = mod.TwoDeal.__mapper__.relationships
    by_column = {
        next(iter(r.local_columns)).name: name for name, r in rels.items()
    }
    assert by_column == {
        "BuyerId": "buyer_two_party",
        "SellerId": "seller_two_party",
    }

    # Each side of each pair points at the other, which is what makes the
    # collection usable from the parent.
    for name, rel in rels.items():
        assert rel.back_populates in mod.TwoParty.__mapper__.relationships
        assert (
            mod.TwoParty.__mapper__.relationships[
                rel.back_populates
            ].back_populates
            == name
        )


@pytest.mark.parametrize(
    "column,forward,back",
    [
        ("BillingCustomerId", "billing_customer", "billing_customer_orders"),
        (
            "ShippingCustomerId",
            "shipping_customer",
            "shipping_customer_orders",
        ),
        ("BuyerId", "buyer_customer", "buyer_orders"),
    ],
)
def test_two_references_are_named_from_their_columns(
    column: str, forward: str, back: str
) -> None:
    """The column is what distinguishes them, so the column names them."""
    spec = ModelSpec(
        catalog="c",
        schemas=["dbo"],
        history=HistoryConfig(),
        tables=[
            TableSpec(
                name="Customer",
                schema="dbo",
                primary_key=["CustomerId"],
                columns=[_col("CustomerId", "bigint", False, 1)],
            ),
            TableSpec(
                name="Order",
                schema="dbo",
                primary_key=["OrderId"],
                columns=[_col("OrderId", "bigint", False, 1)],
                foreign_keys=[
                    ForeignKeySpec(
                        columns=[c],
                        referred_table="dbo.Customer",
                        referred_columns=["CustomerId"],
                    )
                    for c in (column, "OtherId")
                ],
            ),
        ],
    )
    gen = Generator(spec)
    order = spec.table("dbo.Order")
    customer = spec.table("dbo.Customer")
    assert order is not None and customer is not None
    assert gen._relationship_names(order, order.foreign_keys[0], customer) == (
        forward,
        back,
    )


def test_a_relationship_never_displaces_a_column(tmp_path: Path) -> None:
    """The column is the data; the relationship is a convenience."""
    spec = _named_spec(
        "Clash",
        [
            _col("ClashDealId", "bigint", False, 1),
            _col("ClashPartyId", "bigint", True, 2),
            _col("clash_party", "string", True, 3),
        ],
        [
            ForeignKeySpec(
                columns=["ClashPartyId"],
                referred_table="dbo.ClashParty",
                referred_columns=["ClashPartyId"],
                origin="manual",
            )
        ],
    )
    gen = Generator(spec)
    gen.build_modules()
    generate(spec, tmp_path / "clash")
    sys.path.insert(0, str(tmp_path))
    mod = importlib.import_module("clash")
    configure_mappers()

    assert "clash_party" in {c.name for c in mod.ClashDeal.__table__.columns}
    assert any("clash_party" in w for w in gen.report.warnings), (
        gen.report.warnings
    )


def test_a_column_python_cannot_name_still_generates(
    tmp_path: Path,
) -> None:
    """Databricks accepts these names; the emitted module has to import."""
    spec = ModelSpec(
        catalog="c",
        schemas=["dbo"],
        history=HistoryConfig(),
        tables=[
            TableSpec(
                name="Awkward",
                schema="dbo",
                primary_key=["AwkwardId"],
                columns=[
                    _col("AwkwardId", "bigint", False, 1),
                    _col("class", "string", True, 2),
                    _col("Unit Price", "decimal(18,4)", True, 3),
                    _col("2fast", "string", True, 4),
                ],
            )
        ],
    )
    report = generate(spec, tmp_path / "awkward")
    sys.path.insert(0, str(tmp_path))
    mod = importlib.import_module("awkward")
    configure_mappers()

    assert mod.Awkward.class_.property.columns[0].name == "class"
    assert mod.Awkward.Unit_Price.property.columns[0].name == "Unit Price"
    assert mod.Awkward._2fast.property.columns[0].name == "2fast"

    assert sorted(report.renamed_attrs) == [
        "dbo.Awkward.2fast -> _2fast",
        "dbo.Awkward.Unit Price -> Unit_Price",
        "dbo.Awkward.class -> class_",
    ]


def test_two_columns_reaching_one_attribute_are_reported(
    tmp_path: Path,
) -> None:
    """Only one of them can be mapped, so say which pair collided."""
    spec = ModelSpec(
        catalog="c",
        schemas=["dbo"],
        history=HistoryConfig(),
        tables=[
            TableSpec(
                name="Collide",
                schema="dbo",
                primary_key=["CollideId"],
                columns=[
                    _col("CollideId", "bigint", False, 1),
                    _col("unit price", "string", True, 2),
                    _col("unit-price", "string", True, 3),
                ],
            )
        ],
    )
    report = generate(spec, tmp_path / "collide")
    assert any(
        "unit price" in w and "unit-price" in w and "unit_price" in w
        for w in report.warnings
    ), report.warnings


def _keyed_demo_spec() -> ModelSpec:
    """`_demo_spec` with the keys inference would have proposed."""
    s = _demo_spec()
    for key, cols in (
        ("dbo.Widget", ["WidgetId"]),
        ("dbo.Owner", ["OwnerId"]),
    ):
        tbl = s.table(key)
        assert tbl is not None
        tbl.primary_key = cols
    widget = s.table("dbo.Widget")
    assert widget is not None
    widget.foreign_keys.append(
        ForeignKeySpec(
            columns=["OwnerId"],
            referred_table="dbo.Owner",
            referred_columns=["OwnerId"],
            origin="manual",
        )
    )
    pair_history_tables(s)
    return s


def test_a_module_imports_only_what_it_uses(tmp_path: Path) -> None:
    """The bar is code someone can point a linter at without excuses."""
    bare = ModelSpec(
        catalog="c",
        schemas=["dbo"],
        history=HistoryConfig(),
        tables=[
            TableSpec(
                name="Standalone",
                schema="dbo",
                primary_key=["StandaloneId"],
                columns=[_col("StandaloneId", "bigint", False, 1)],
            )
        ],
    )
    generate(bare, tmp_path / "bare")
    text = (tmp_path / "bare" / "standalone.py").read_text(encoding="utf-8")
    for absent in (
        "HistoryMixin",
        "SCD2Config",
        "TYPE_CHECKING",
        "relationship",
        "ForeignKey",
    ):
        assert absent not in text, absent

    generate(_keyed_demo_spec(), tmp_path / "rich")
    widget = (tmp_path / "rich" / "widget.py").read_text(encoding="utf-8")
    assert "HistoryMixin, SCD2Config" in widget
    assert "relationship" in widget
    assert "ForeignKey(" in widget


def test_generated_code_passes_a_linter(tmp_path: Path) -> None:
    """Unused imports and import order, over a package with every shape."""
    ruff = shutil.which("ruff")
    if ruff is None:
        pytest.skip("ruff is not on PATH")
    generate(_paired_spec(), tmp_path / "linted")
    proc = subprocess.run(
        [ruff, "check", "--isolated", "--select", "F,I", str(tmp_path)],
        capture_output=True,
        text=True,
    )
    assert proc.returncode == 0, proc.stdout


def test_two_classes_are_separated_by_two_blank_lines(
    tmp_path: Path,
) -> None:
    generate(_keyed_demo_spec(), tmp_path / "spaced")
    text = (tmp_path / "spaced" / "widget.py").read_text(encoding="utf-8")
    assert "\n\n\nclass WidgetHistory(" in text
    assert "\n\n\n\nclass" not in text


def test_a_single_column_business_key_is_a_tuple(tmp_path: Path) -> None:
    """`("WidgetId")` is a string; the trailing comma is what makes it a key."""
    generate(_keyed_demo_spec(), tmp_path / "bkey")
    text = (tmp_path / "bkey" / "widget.py").read_text(encoding="utf-8")
    assert 'business_key=("WidgetId",),' in text


def test_a_stale_module_is_removed_on_regeneration(tmp_path: Path) -> None:
    """A table that goes away upstream must not leave a module behind."""
    out = tmp_path / "pruned"
    generate(_keyed_demo_spec(), out)
    assert (out / "owner.py").exists()

    smaller = _keyed_demo_spec()
    smaller.tables = [t for t in smaller.tables if t.key != "dbo.Owner"]
    report = generate(smaller, out)

    assert report.removed_modules == ["owner.py"]
    assert not (out / "owner.py").exists()
    assert (out / "widget.py").exists()


def test_a_file_stele_did_not_write_is_left_alone(tmp_path: Path) -> None:
    """--out pointed somewhere unintended must not empty it."""
    out = tmp_path / "mixed"
    generate(_keyed_demo_spec(), out)
    (out / "notes.py").write_text("hand written\n", encoding="utf-8")

    report = generate(_keyed_demo_spec(), out)

    assert report.removed_modules == []
    assert (out / "notes.py").exists()


def test_nothing_is_pruned_from_a_directory_stele_does_not_own(
    tmp_path: Path,
) -> None:
    out = tmp_path / "foreign"
    out.mkdir()
    (out / "__init__.py").write_text("", encoding="utf-8")
    (out / "thing.py").write_text("x = 1\n", encoding="utf-8")

    report = generate(_keyed_demo_spec(), out)

    assert report.removed_modules == []
    assert (out / "thing.py").exists()


def test_history_whose_primary_is_missing_is_reported(
    tmp_path: Path,
) -> None:
    spec = _demo_spec()
    spec.tables = [t for t in spec.tables if t.key != "dbo.Widget"]
    report = generate(spec, tmp_path / "orphan")
    assert report.unpaired_history == ["dbo.Widget_history"]


def test_history_whose_primary_is_disabled_is_reported(
    tmp_path: Path,
) -> None:
    """It is reached only through its primary, so it renders nowhere."""
    spec = _demo_spec()
    widget = spec.table("dbo.Widget")
    assert widget is not None
    widget.enabled = False
    report = generate(spec, tmp_path / "disabled")

    assert report.unpaired_history == ["dbo.Widget_history"]
    assert "widget" not in report.modules


def test_snake_case_generates_a_package_that_configures(
    tmp_path: Path,
) -> None:
    """The flag only changes anything on the catalogs it has to work for."""
    cols = [
        _col("GadgetId", "bigint", False, 1),
        _col("GadgetName", "string", True, 2),
    ]
    hist = [
        *(ColumnSpec(**c.__dict__) for c in cols),
        _col("StartDate", "timestamp_ntz", False, 3),
        _col("EndDate", "timestamp_ntz", True, 4),
    ]
    spec = ModelSpec(
        catalog="c",
        schemas=["dbo"],
        history=HistoryConfig(),
        tables=[
            TableSpec(
                name="Gadget",
                schema="dbo",
                primary_key=["GadgetId"],
                columns=cols,
            ),
            TableSpec(name="Gadget_history", schema="dbo", columns=hist),
        ],
    )
    pair_history_tables(spec)
    generate(spec, tmp_path / "snake", preserve_names=False)
    sys.path.insert(0, str(tmp_path))
    mod = importlib.import_module("snake")
    configure_mappers()

    assert mod.GadgetHistory.__scd2__.start_attr == "start_date"
    assert mod.GadgetHistory.__scd2__.business_key == ("gadget_id",)
    # the predicate the descriptor feeds still builds
    assert mod.GadgetHistory.as_of("2026-01-01") is not None


def test_schema_token_not_literal(models: ModuleType) -> None:
    assert models.Widget.__table__.schema == "stele__dbo"


def test_two_bindings_resolve_their_own_schema(models: ModuleType) -> None:
    """One statement through two bindings that share a compiled cache.

    `Binding` calls `engine.execution_options(...)`, which returns an
    `OptionEngine` holding `self._compiled_cache = proxied._compiled_cache`.
    Two bindings built from one engine therefore compile through one cache,
    which is the shape SQLAlchemy fixed in 2.0.18: a statement cached under
    one `schema_translate_map` was reused under another whose key set
    differed, and the second map was ignored.

    The token survives because substitution happens after compilation, and
    because the SQLAlchemy floor is above that fix. Neither is stele's to
    guarantee, and one class hierarchy addressing two backends is the whole
    design, so it is asserted rather than assumed.
    """
    engine = create_engine("sqlite://", poolclass=StaticPool)

    @event.listens_for(engine, "connect")
    def _attach(dbapi_connection: Any, _: Any) -> None:
        dbapi_connection.execute("ATTACH DATABASE ':memory:' AS alpha")
        dbapi_connection.execute("ATTACH DATABASE ':memory:' AS beta")

    alpha = Binding(engine=engine, schemas={"dbo": "alpha"})
    beta = Binding(engine=engine, schemas={"dbo": "beta"})
    models.metadata.create_all(alpha.engine)
    models.metadata.create_all(beta.engine)

    with alpha.session() as s:
        s.add(models.Owner(OwnerId=1, OwnerName="in alpha"))
    with beta.session() as s:
        s.add(models.Owner(OwnerId=1, OwnerName="in beta"))

    # Alternating drives the shared cache in both directions: a statement
    # cached for one binding is next asked for by the other.
    stmt = select(models.Owner.OwnerName)
    assert alpha.scalars(stmt) == ["in alpha"]
    assert beta.scalars(stmt) == ["in beta"]
    assert alpha.scalars(stmt) == ["in alpha"]
    assert beta.scalars(stmt) == ["in beta"]


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


def test_compile_renders_the_bindings_schema(models: ModuleType) -> None:
    """The token is resolved by the connection at execution; this asks for it
    without one, so a statement can be handed to something that is not
    SQLAlchemy."""
    b = Binding(engine=create_engine("sqlite://"), schemas={"dbo": "main"})

    sql = str(b.compile(select(models.Widget.WidgetName)))

    assert "stele__dbo" not in sql
    assert "main" in sql


def test_two_bindings_compile_the_same_statement_differently(
    models: ModuleType,
) -> None:
    """One statement, two backends: the whole point of the schema token."""
    stmt = select(models.Widget.WidgetName)
    alpha = Binding(engine=create_engine("sqlite://"), schemas={"dbo": "a"})
    beta = Binding(engine=create_engine("sqlite://"), schemas={"dbo": "b"})

    assert 'a."Widget"' in str(alpha.compile(stmt))
    assert 'b."Widget"' in str(beta.compile(stmt))


def test_compile_keeps_parameters_out_of_the_sql_by_default(
    models: ModuleType,
) -> None:
    b = Binding(engine=create_engine("sqlite://"), schemas={"dbo": "main"})
    stmt = select(models.Widget.WidgetName).where(models.Widget.WidgetId == 7)

    compiled = b.compile(stmt)

    assert "7" not in str(compiled)
    assert 7 in compiled.params.values()


def test_literal_binds_puts_the_values_in_the_text(
    models: ModuleType,
) -> None:
    """For a consumer that takes a complete statement and nothing else."""
    b = Binding(engine=create_engine("sqlite://"), schemas={"dbo": "main"})
    stmt = select(models.Widget.WidgetName).where(models.Widget.WidgetId == 7)

    assert "7" in str(b.compile(stmt, literal_binds=True))


def test_a_binding_with_no_schemas_compiles(models: ModuleType) -> None:
    """Asking to render an empty map trips a bare assertion in the compiler."""
    b = Binding(engine=create_engine("sqlite://"))

    assert "stele__dbo" in str(b.compile(select(models.Widget.WidgetName)))


def test_a_token_the_map_does_not_name_renders_as_itself(
    models: ModuleType,
) -> None:
    """Which is what execution does with it too."""
    b = Binding(engine=create_engine("sqlite://"), schemas={"other": "x"})

    assert "stele__dbo" in str(b.compile(select(models.Widget.WidgetName)))


def test_a_history_select_compiles_with_its_predicate(
    models: ModuleType,
) -> None:
    """The case that earns the method: the instant travels into the SQL, so
    whatever receives it filters correctly without reimplementing the
    interval."""
    b = Binding(engine=create_engine("sqlite://"), schemas={"dbo": "main"})

    sql = str(
        b.compile(models.WidgetHistory.as_of("2026-01-01"), literal_binds=True)
    )

    assert "stele__dbo" not in sql
    assert "StartDate" in sql and "EndDate" in sql
    assert "2026-01-01" in sql


def test_replica_ddl_resolves_schema_tokens(models: ModuleType) -> None:
    from stele.runtime import replica_ddl

    sql = replica_ddl(
        models.metadata, dialect_name="mssql", schemas={"dbo": "dbo"}
    )
    assert "stele__dbo" not in sql
    assert "NVARCHAR" in sql and "dbo.[Widget]" in sql


def _keyed_spec() -> ModelSpec:
    """Keys of every shape: one integer column, text, and composite."""
    return ModelSpec(
        catalog="c",
        schemas=["dbo"],
        history=HistoryConfig(),
        tables=[
            TableSpec(
                name="Pylon",
                schema="dbo",
                columns=[
                    _col("PylonId", "bigint", False, 1),
                    _col("PylonCount", "int", True, 2),
                ],
                primary_key=["PylonId"],
            ),
            TableSpec(
                name="Cairn",
                schema="dbo",
                columns=[
                    _col(
                        "CairnCode",
                        "string",
                        False,
                        1,
                        observed_max_length=8,
                    ),
                    _col(
                        "CairnName",
                        "string",
                        True,
                        2,
                        observed_max_length=30,
                    ),
                ],
                primary_key=["CairnCode"],
            ),
            TableSpec(
                name="Dolmen",
                schema="dbo",
                columns=[
                    _col("SiteId", "bigint", False, 1),
                    _col("SlotId", "bigint", False, 2),
                ],
                primary_key=["SiteId", "SlotId"],
            ),
        ],
    )


def _create_table(sql: str, table: str) -> str:
    """The registry is shared across generated packages in this file, so
    every assertion has to name the statement it is about."""
    for stmt in sql.split(";"):
        if f"[{table}] (" in stmt:
            return stmt
    raise AssertionError(f"no CREATE TABLE for {table}")


def test_replica_keys_are_never_identity(tmp_path: Path) -> None:
    """The source owns the keys, so the replica must accept them verbatim.

    A bulk load without KEEPIDENTITY into an IDENTITY column renumbers every
    row and silently breaks the foreign keys that point at it.
    """
    from stele.runtime import replica_ddl

    generate(_keyed_spec(), tmp_path / "keyed")
    sys.path.insert(0, str(tmp_path))
    mod = importlib.import_module("keyed")
    configure_mappers()

    sql = replica_ddl(
        mod.metadata, dialect_name="mssql", schemas={"dbo": "dbo"}
    )

    pylon = _create_table(sql, "Pylon")
    assert "IDENTITY" not in pylon
    assert "[PylonId] BIGINT NOT NULL" in pylon
    assert "PRIMARY KEY ([PylonId])" in pylon
    assert "[PylonCount] INTEGER NULL" in pylon

    cairn = _create_table(sql, "Cairn")
    assert "IDENTITY" not in cairn
    assert "PRIMARY KEY ([CairnCode])" in cairn

    dolmen = _create_table(sql, "Dolmen")
    assert "IDENTITY" not in dolmen
    assert "PRIMARY KEY ([SiteId], [SlotId])" in dolmen
