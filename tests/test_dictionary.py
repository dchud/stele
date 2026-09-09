"""The tbls document: what survives the format, and what it says.

Two checks, because neither covers the other. The schema in `data/` says
what the format permits, and tbls does not enforce it - it renders a
document carrying a property the schema forbids, or missing one it
requires, without complaint. Rendering says the other half: that a valid
document produces the page the mapping was designed around.
"""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path
from typing import Any, cast

import pytest
from jsonschema import Draft7Validator

from stele.dictionary import build, to_json, write
from stele.spec import (
    ColumnSpec,
    ForeignKeySpec,
    ModelSpec,
    TableSpec,
)

SCHEMA = json.loads(
    (Path(__file__).parent / "data" / "tbls.schema.json").read_text()
)


def _spec() -> ModelSpec:
    """Two schemas, an inferred key, a verified one, and a history pair."""
    order = TableSpec(
        name="Order",
        schema="dbo",
        comment="One row per customer order.",
        observed_row_count=1_204_331,
        primary_key=["OrderId"],
        primary_key_origin="inferred",
        primary_key_verified=True,
        history_table="dbo.Order_history",
        columns=[
            ColumnSpec(
                name="OrderId",
                source_type="BIGINT",
                nullable=False,
                ordinal=0,
                comment="Surrogate key.",
                observed_null_fraction=0.0,
                observed_distinct=1_204_331,
                observed_min_value=1,
                observed_max_value=1_204_331,
            ),
            ColumnSpec(
                name="CustomerId",
                source_type="BIGINT",
                ordinal=1,
                observed_null_fraction=0.021,
                observed_distinct=84_220,
            ),
            ColumnSpec(
                name="Custodian",
                source_type="STRING",
                ordinal=2,
                observed_max_length=18,
                observed_null_fraction=0.114,
            ),
        ],
        foreign_keys=[
            ForeignKeySpec(
                columns=["CustomerId"],
                referred_table="dbo.Customer",
                referred_columns=["CustomerId"],
                origin="inferred",
                containment=0.997,
                evidence="column name matches the parent's key",
            ),
            ForeignKeySpec(
                columns=["Custodian"],
                referred_table="ref.Owner",
                referred_columns=["OwnerId"],
                origin="manual",
                enabled=False,
            ),
        ],
    )
    history = TableSpec(
        name="Order_history",
        schema="dbo",
        history_of="dbo.Order",
        observed_row_count=8_912_004,
        columns=[ColumnSpec(name="OrderId", source_type="BIGINT")],
    )
    customer = TableSpec(
        name="Customer",
        schema="dbo",
        observed_row_count=84_512,
        primary_key=["CustomerId"],
        primary_key_origin="inferred",
        columns=[
            ColumnSpec(name="CustomerId", source_type="BIGINT", nullable=False)
        ],
    )
    owner = TableSpec(
        name="Owner",
        schema="ref",
        table_type="VIEW",
        primary_key=["OwnerId"],
        primary_key_origin="catalog",
        primary_key_verified=True,
        columns=[ColumnSpec(name="OwnerId", source_type="BIGINT")],
    )
    dropped = TableSpec(
        name="Staging",
        schema="dbo",
        enabled=False,
        columns=[ColumnSpec(name="Junk", source_type="STRING")],
    )
    return ModelSpec(
        catalog="acme_prod",
        schemas=["dbo", "ref"],
        tables=[order, history, customer, owner, dropped],
    )


def _doc(**kw: Any) -> dict[str, Any]:
    return cast(dict[str, Any], json.loads(to_json(build(_spec(), **kw))))


def _table(doc: dict[str, Any], name: str) -> dict[str, Any]:
    return next(t for t in doc["tables"] if t["name"] == name)


# --------------------------------------------------------------------------
# The contract
# --------------------------------------------------------------------------


@pytest.mark.parametrize("include_history", [False, True])
def test_document_matches_the_published_schema(include_history: bool) -> None:
    Draft7Validator(SCHEMA).validate(_doc(include_history=include_history))


def test_serialisation_renames_def_and_drops_unset_fields() -> None:
    doc = _doc()
    relation = doc["relations"][0]
    assert "def" in relation
    assert "def_" not in relation
    # `default` is never set, so it is absent rather than null.
    assert "default" not in _table(doc, "dbo.Order")["columns"][0]


def test_disabled_tables_and_references_are_left_out() -> None:
    doc = _doc()
    assert not [t for t in doc["tables"] if t["name"] == "dbo.Staging"]
    assert not [
        c
        for c in _table(doc, "dbo.Order")["constraints"]
        if c["type"] == "FOREIGN KEY" and "Custodian" in c["columns"]
    ]


# --------------------------------------------------------------------------
# Provenance
# --------------------------------------------------------------------------


def test_primary_key_carries_its_origin_and_whether_data_agreed() -> None:
    doc = _doc()
    pk = next(
        c
        for c in _table(doc, "dbo.Order")["constraints"]
        if c["type"] == "PRIMARY KEY"
    )
    assert pk["columns"] == ["OrderId"]
    assert "inferred by stele" in pk["def"]
    assert "verified against the data" in pk["def"]

    unverified = next(
        c
        for c in _table(doc, "dbo.Customer")["constraints"]
        if c["type"] == "PRIMARY KEY"
    )
    assert "not verified against the data" in unverified["def"]


def test_a_reference_is_both_a_relation_and_a_constraint() -> None:
    """The relation draws the diagram edge; the constraint is the text."""
    doc = _doc()
    relation = next(r for r in doc["relations"] if r["table"] == "dbo.Order")
    assert relation["parent_table"] == "dbo.Customer"
    assert relation["virtual"] is True
    assert "column name matches" in relation["def"]
    assert "containment 0.997" in relation["def"]

    fk = next(
        c
        for c in _table(doc, "dbo.Order")["constraints"]
        if c["type"] == "FOREIGN KEY"
    )
    assert fk["referenced_table"] == "dbo.Customer"
    assert "REFERENCES dbo.Customer (CustomerId)" in fk["def"]
    assert "column name matches" in fk["def"]
    assert "containment 0.997" in fk["def"]


def test_a_catalog_declared_reference_is_not_virtual() -> None:
    spec = _spec()
    order = spec.table("dbo.Order")
    assert order is not None
    order.foreign_keys[0].origin = "catalog"
    doc = json.loads(to_json(build(spec)))
    assert doc["relations"][0]["virtual"] is False


def test_labels_mark_what_the_catalog_did_not_declare() -> None:
    doc = _doc()
    order = {label["name"] for label in _table(doc, "dbo.Order")["labels"]}
    assert order == {"inferred", "history"}
    customer = {
        label["name"] for label in _table(doc, "dbo.Customer")["labels"]
    }
    assert customer == {"inferred", "unverified"}
    assert "labels" not in _table(doc, "ref.Owner")


# --------------------------------------------------------------------------
# Observed behaviour
# --------------------------------------------------------------------------


def test_statistics_reach_the_column_and_the_row_count_the_table() -> None:
    doc = _doc()
    order = _table(doc, "dbo.Order")
    assert "1,204,331 rows." in order["comment"]
    assert "One row per customer order." in order["comment"]

    by_name = {c["name"]: c for c in order["columns"]}
    assert by_name["OrderId"]["extra_def"] == (
        "no nulls; 1,204,331 distinct; range [1, 1,204,331]"
    )
    assert by_name["Custodian"]["extra_def"] == (
        "11.4% null; longest observed 18"
    )
    # The row count is on the table, not repeated down every column.
    assert "rows" not in by_name["CustomerId"]["extra_def"]


def test_a_column_nobody_described_still_shows_its_numbers() -> None:
    doc = _doc()
    custodian = next(
        c
        for c in _table(doc, "dbo.Order")["columns"]
        if c["name"] == "Custodian"
    )
    assert "comment" not in custodian
    assert custodian["extra_def"]


def test_columns_keep_their_catalog_order() -> None:
    doc = _doc()
    names = [c["name"] for c in _table(doc, "dbo.Order")["columns"]]
    assert names == ["OrderId", "CustomerId", "Custodian"]


# --------------------------------------------------------------------------
# History
# --------------------------------------------------------------------------


def test_history_is_described_on_its_primary_and_left_out() -> None:
    doc = _doc()
    assert not [t for t in doc["tables"] if t["name"] == "dbo.Order_history"]
    comment = _table(doc, "dbo.Order")["comment"]
    assert "SCD2 history in dbo.Order_history" in comment
    assert "StartDate/EndDate" in comment


def test_including_history_adds_the_table_and_keeps_the_description() -> None:
    doc = _doc(include_history=True)
    history = _table(doc, "dbo.Order_history")
    assert "SCD2 history for dbo.Order." in history["comment"]
    assert {label["name"] for label in history["labels"]} == {"history"}
    assert (
        "SCD2 history in dbo.Order_history"
        in (_table(doc, "dbo.Order")["comment"])
    )


# --------------------------------------------------------------------------
# Viewpoints
# --------------------------------------------------------------------------


def test_viewpoints_group_by_schema_and_collect_what_stele_guessed() -> None:
    doc = _doc()
    by_name = {v["name"]: v for v in doc["viewpoints"]}
    # Catalog order, matching the document's own table list.
    assert by_name["dbo"]["tables"] == ["dbo.Order", "dbo.Customer"]
    assert by_name["ref"]["tables"] == ["ref.Owner"]
    assert by_name["Inferred shape"]["labels"] == ["inferred", "unverified"]


def test_one_schema_gets_no_schema_viewpoint() -> None:
    spec = _spec()
    spec.tables = [t for t in spec.tables if t.schema == "dbo"]
    names = {v["name"] for v in json.loads(to_json(build(spec)))["viewpoints"]}
    assert names == {"Inferred shape"}


def test_a_catalog_that_declares_everything_gets_no_uncertainty_view() -> None:
    spec = ModelSpec(
        catalog="declared",
        tables=[
            TableSpec(
                name="Owner",
                schema="ref",
                primary_key=["OwnerId"],
                primary_key_origin="catalog",
                primary_key_verified=True,
                columns=[ColumnSpec(name="OwnerId", source_type="BIGINT")],
            )
        ],
    )
    assert "viewpoints" not in json.loads(to_json(build(spec)))


# --------------------------------------------------------------------------
# The renderer itself, where it is installed
# --------------------------------------------------------------------------


requires_tbls = pytest.mark.skipif(
    shutil.which("tbls") is None, reason="tbls renders the document"
)


def _render(spec_out: Path, rendered: Path) -> None:
    subprocess.run(
        ["tbls", "doc", f"json://{spec_out.resolve()}", str(rendered)],
        check=True,
        capture_output=True,
    )


@requires_tbls
def test_tbls_puts_the_content_where_the_mapping_expects(
    tmp_path: Path,
) -> None:
    """Each of these landed somewhere else before the output was read."""
    out = tmp_path / "dictionary.json"
    write(_spec(), out)
    _render(out, tmp_path / "rendered")
    page = (tmp_path / "rendered" / "dbo.Order.md").read_text()

    # Uncollapsed at the top, rather than behind the disclosure `def` gets.
    assert "1,204,331 rows." in page
    # Readable as text, rather than only as a label inside the diagram.
    assert "column name matches the parent's key" in page
    assert "containment 0.997" in page
    # A grid column of its own.
    assert "11.4% null; longest observed 18" in page
    # Provenance a reader sees before trusting the shape.
    assert "inferred by stele; verified against the data" in page


@requires_tbls
@pytest.mark.parametrize("include_history", [False, True])
def test_tbls_renders_a_page_per_table_and_viewpoint(
    tmp_path: Path, include_history: bool
) -> None:
    out = tmp_path / "dictionary.json"
    write(_spec(), out, include_history=include_history)
    rendered = tmp_path / "rendered"
    _render(out, rendered)

    assert (rendered / "README.md").exists()
    assert (rendered / "dbo.Order.md").exists()
    assert (rendered / "dbo.Order_history.md").exists() is include_history
    # dbo, ref, and the one collecting what stele guessed at.
    viewpoints = sorted(rendered.glob("viewpoint-*.md"))
    assert len(viewpoints) == 3
