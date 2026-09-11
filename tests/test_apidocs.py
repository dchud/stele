"""Reference pages for the generated package, and what they link to."""

from __future__ import annotations

from pathlib import Path

from stele.apidocs import MARKER, write
from stele.spec import ColumnSpec, ForeignKeySpec, ModelSpec, TableSpec


def _col(name: str, type_: str = "STRING", **kw: object) -> ColumnSpec:
    return ColumnSpec(name=name, source_type=type_, **kw)  # type: ignore[arg-type]


def _spec() -> ModelSpec:
    order = TableSpec(
        name="Order",
        schema="dbo",
        comment="One row per customer order.",
        primary_key=["OrderId"],
        primary_key_origin="inferred",
        history_table="dbo.Order_history",
        columns=[
            _col("OrderId", "BIGINT", nullable=False),
            _col("CustomerId", "BIGINT"),
        ],
        foreign_keys=[
            ForeignKeySpec(
                columns=["CustomerId"],
                referred_table="dbo.Customer",
                referred_columns=["CustomerId"],
                origin="inferred",
            )
        ],
    )
    history = TableSpec(
        name="Order_history",
        schema="dbo",
        history_of="dbo.Order",
        columns=[
            _col("OrderId", "BIGINT", nullable=False),
            _col("StartDate", "TIMESTAMP"),
            _col("EndDate", "TIMESTAMP"),
        ],
    )
    customer = TableSpec(
        name="Customer",
        schema="dbo",
        primary_key=["CustomerId"],
        primary_key_origin="inferred",
        columns=[_col("CustomerId", "BIGINT", nullable=False)],
    )
    return ModelSpec(
        catalog="acme", schemas=["dbo"], tables=[order, history, customer]
    )


def _write(tmp_path: Path, **kw: object) -> Path:
    out = tmp_path / "api"
    write(_spec(), out, package="acme_models", **kw)  # type: ignore[arg-type]
    return out


def test_a_page_per_module_and_an_index_over_them(tmp_path: Path) -> None:
    out = _write(tmp_path)
    names = sorted(p.name for p in out.glob("*.md"))
    assert "index.md" in names
    assert "order.md" in names
    assert "customer.md" in names


def test_a_class_says_what_it_is_called_and_how_to_import_it(
    tmp_path: Path,
) -> None:
    page = (_write(tmp_path) / "order.md").read_text()
    assert "## Order" in page
    assert "from acme_models import Order" in page
    assert "One row per customer order." in page


def test_attributes_are_listed_against_their_columns(tmp_path: Path) -> None:
    page = (_write(tmp_path) / "order.md").read_text()
    assert "`OrderId`" in page
    assert "PK" in page


def test_a_class_links_to_the_table_it_maps(tmp_path: Path) -> None:
    """tbls names its page for the schema-qualified table."""
    page = (_write(tmp_path) / "order.md").read_text()
    assert "../database/dbo.Order.md" in page


def test_without_a_dictionary_there_is_no_link_to_one(
    tmp_path: Path,
) -> None:
    """A link written to a page that is not there fails every build."""
    page = (_write(tmp_path, dictionary=None) / "order.md").read_text()
    assert "database" not in page


def test_a_relationship_links_to_its_target_class(tmp_path: Path) -> None:
    page = (_write(tmp_path) / "order.md").read_text()
    # Customer lives on its own page; the history class shares this one.
    assert "customer.md#customer" in page
    assert "order.md#orderhistory" in page


def test_a_history_class_shows_the_read_helpers(tmp_path: Path) -> None:
    page = (_write(tmp_path) / "order.md").read_text()
    assert "Order.as_of(when)" in page
    assert "Order.versions_of(key)" in page


def test_the_index_points_at_both_documents(tmp_path: Path) -> None:
    index = (_write(tmp_path) / "index.md").read_text()
    assert "acme_models" in index
    assert "../database/README.md" in index
    assert "order" in index


# --- what a second run does ------------------------------------------------


def test_a_page_for_a_class_that_went_away_is_removed(
    tmp_path: Path,
) -> None:
    out = _write(tmp_path)
    stale = out / "widget.md"
    stale.write_text(f"<!-- {MARKER} -->\n\n# widget\n")

    write(_spec(), out, package="acme_models")

    assert not stale.exists()


def test_a_page_nobody_here_wrote_is_left_alone(tmp_path: Path) -> None:
    """Pointing --docs at the wrong directory should cost nothing."""
    out = _write(tmp_path)
    mine = out / "notes.md"
    mine.write_text("# My own notes\n")

    write(_spec(), out, package="acme_models")

    assert mine.read_text() == "# My own notes\n"
