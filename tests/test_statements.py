"""What the validation and profiling statements compile to.

`infer --validate` and `profile` are the only part of stele that builds SQL
instead of models, so they are the only part that could be Databricks-shaped
without anything noticing. Compiling each statement for both dialects is what
notices: one statement has to come out with backticks and LIMIT on one side
and brackets and TOP on the other, and a schema token has to resolve to a
real schema name on both.

Compiling needs no connection: a dialect renders a statement on its own,
which is what makes both halves testable where only one warehouse exists.
Running them is a separate question, and SQLite - a third dialect, and
neither of the two - is what answers whether the numbers arrive where the
caller reads them.
"""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any

import pytest
from sqlalchemy import Engine, Executable, create_engine, text
from sqlalchemy.dialects import mssql
from sqlalchemy.pool import StaticPool
from sqlalchemy.sql import ClauseElement

from stele.infer import (
    foreign_key_statement,
    null_fraction_statement,
    orphan_statement,
    primary_key_statement,
)
from stele.profile import profile_statement
from stele.spec import ColumnSpec, ModelSpec, TableSpec
from stele.tables import columns_of, core_table, schema_translation

try:
    from databricks.sqlalchemy import DatabricksDialect

    _DATABRICKS: Any = DatabricksDialect()
except ImportError:  # pragma: no cover - the extra is optional
    _DATABRICKS = None

needs_databricks = pytest.mark.skipif(
    _DATABRICKS is None, reason="databricks-sqlalchemy is not installed"
)


def _spec() -> ModelSpec:
    widget = TableSpec(
        name="Widget",
        schema="dbo",
        columns=[
            ColumnSpec(name="WidgetId", source_type="bigint", ordinal=1),
            ColumnSpec(name="Name", source_type="string", ordinal=2),
            ColumnSpec(name="OwnerId", source_type="bigint", ordinal=3),
            ColumnSpec(name="Region", source_type="string", ordinal=4),
        ],
        primary_key=["WidgetId"],
    )
    owner = TableSpec(
        name="Owner",
        schema="dbo",
        columns=[
            ColumnSpec(name="OwnerId", source_type="bigint", ordinal=1),
            ColumnSpec(name="Region", source_type="string", ordinal=2),
        ],
        primary_key=["OwnerId"],
    )
    return ModelSpec(catalog="cat", schemas=["dbo"], tables=[widget, owner])


def _tables() -> tuple[TableSpec, TableSpec]:
    spec = _spec()
    widget, owner = spec.table("dbo.Widget"), spec.table("dbo.Owner")
    assert widget is not None and owner is not None
    return widget, owner


def _sql(stmt: ClauseElement, dialect: Any) -> str:
    """One statement as the dialect would send it, tokens resolved.

    `literal_binds` puts the row limits into the text, because a limit
    rendered as a parameter says nothing about which of LIMIT and TOP the
    dialect chose.
    """
    return str(
        stmt.compile(
            dialect=dialect,
            schema_translate_map=schema_translation(_spec()),
            render_schema_translate=True,
            compile_kwargs={"literal_binds": True},
        )
    )


def _mssql(stmt: ClauseElement) -> str:
    return _sql(stmt, mssql.dialect())


def _databricks(stmt: ClauseElement) -> str:
    return _sql(stmt, _DATABRICKS)


# --- naming ----------------------------------------------------------------


@needs_databricks
def test_a_schema_token_resolves_to_a_real_schema() -> None:
    """The token is what the statement carries; the map is what runs."""
    widget, _ = _tables()
    stmt = null_fraction_statement(widget, ["OwnerId"])

    for sql in (_databricks(stmt), _mssql(stmt)):
        assert "stele__" not in sql
        assert "dbo." in sql


@needs_databricks
def test_the_catalog_is_no_part_of_the_name() -> None:
    """A three-part name is Databricks-shaped and cannot address SQL Server.

    The catalog belongs to the connection, which is where the generated
    model layer already keeps it.
    """
    widget, _ = _tables()
    stmt = null_fraction_statement(widget, ["OwnerId"])

    assert "cat" not in _databricks(stmt)
    assert "cat" not in _mssql(stmt)


@needs_databricks
def test_each_dialect_quotes_identifiers_its_own_way() -> None:
    widget, _ = _tables()
    stmt = null_fraction_statement(widget, ["OwnerId"])

    assert "`Widget`" in _databricks(stmt)
    assert "[Widget]" in _mssql(stmt)


# --- profiling -------------------------------------------------------------


@needs_databricks
def test_profiling_measures_length_with_each_dialects_function() -> None:
    """LENGTH on Databricks, LEN on SQL Server, out of one statement."""
    widget, _ = _tables()
    stmt = profile_statement(widget, [widget.columns[1]])

    assert "length(" in _databricks(stmt)
    assert "LEN(" in _mssql(stmt)


@needs_databricks
def test_a_profiling_sample_limits_rows_the_way_each_dialect_does() -> None:
    widget, _ = _tables()
    stmt = profile_statement(widget, [widget.columns[1]], sample=1000)

    assert "LIMIT 1000" in _databricks(stmt)
    assert "TOP 1000" in _mssql(stmt)


def test_a_profile_pass_labels_its_results_positionally() -> None:
    """The caller reads the answers back by position, not by column name."""
    widget, _ = _tables()
    stmt = profile_statement(
        widget, [widget.columns[1], widget.columns[3]], include_distinct=True
    )

    assert [c.key for c in stmt.selected_columns] == [
        "_total",
        "_len_0",
        "_null_0",
        "_dist_0",
        "_len_1",
        "_null_1",
        "_dist_1",
    ]


def test_distinct_counts_are_left_out_unless_asked_for() -> None:
    """Counting distinct values is a second pass over the data."""
    widget, _ = _tables()
    stmt = profile_statement(widget, [widget.columns[1]])

    assert [c.key for c in stmt.selected_columns] == [
        "_total",
        "_len_0",
        "_null_0",
    ]


def test_a_column_is_asked_only_what_its_type_can_answer() -> None:
    """An integer has a range and no width; a string has the reverse."""
    widget, _ = _tables()
    stmt = profile_statement(widget, [widget.columns[0], widget.columns[1]])

    assert [c.key for c in stmt.selected_columns] == [
        "_total",
        "_min_0",
        "_max_0",
        "_len_1",
        "_null_1",
    ]


# --- primary keys ----------------------------------------------------------


@needs_databricks
def test_a_key_check_reads_rows_nulls_and_duplicate_groups() -> None:
    widget, _ = _tables()
    stmt = primary_key_statement(widget, ["WidgetId"])

    for sql in (_databricks(stmt), _mssql(stmt)):
        assert "total_rows" in sql
        assert "null_rows" in sql
        assert "duplicate_groups" in sql
        assert "GROUP BY" in sql
        assert "HAVING count(*) > 1" in sql


@needs_databricks
def test_a_composite_key_groups_by_every_column() -> None:
    widget, _ = _tables()
    stmt = primary_key_statement(widget, ["WidgetId", "Region"])

    assert "GROUP BY dbo.`Widget`.`WidgetId`, dbo.`Widget`.`Region`" in (
        _databricks(stmt)
    )
    assert "GROUP BY dbo.[Widget].[WidgetId], dbo.[Widget].[Region]" in (
        _mssql(stmt)
    )


# --- foreign keys ----------------------------------------------------------


@needs_databricks
def test_a_containment_check_names_both_key_sets() -> None:
    widget, owner = _tables()
    stmt = foreign_key_statement(widget, owner, ["OwnerId"], ["OwnerId"])

    for sql in (_databricks(stmt), _mssql(stmt)):
        assert "WITH c AS" in sql
        assert "p AS" in sql
        assert "distinct_values" in sql
        assert "matched_values" in sql


@needs_databricks
def test_a_sampled_containment_check_limits_the_child_side() -> None:
    """Only the child is sampled; a missing parent row is a false orphan."""
    widget, owner = _tables()
    stmt = foreign_key_statement(
        widget, owner, ["OwnerId"], ["OwnerId"], sample=500
    )

    databricks = _databricks(stmt)
    assert databricks.count("LIMIT 500") == 1
    assert "FROM dbo.`Widget`" in databricks

    sqlserver = _mssql(stmt)
    assert sqlserver.count("TOP 500") == 1
    assert "FROM dbo.[Widget]" in sqlserver


@needs_databricks
def test_a_composite_reference_joins_on_every_column() -> None:
    widget, owner = _tables()
    stmt = foreign_key_statement(
        widget, owner, ["OwnerId", "Region"], ["OwnerId", "Region"]
    )

    assert "ON c.`OwnerId` = p.`OwnerId` AND c.`Region` = p.`Region`" in (
        _databricks(stmt)
    )
    assert "ON c.[OwnerId] = p.[OwnerId] AND c.[Region] = p.[Region]" in (
        _mssql(stmt)
    )


@needs_databricks
def test_orphan_examples_are_capped_by_each_dialects_row_limit() -> None:
    widget, owner = _tables()
    stmt = orphan_statement(widget, owner, ["OwnerId"], ["OwnerId"])

    databricks = _databricks(stmt)
    assert "NOT (EXISTS" in databricks
    assert "LIMIT 5" in databricks

    sqlserver = _mssql(stmt)
    assert "NOT (EXISTS" in sqlserver
    assert "TOP 5" in sqlserver


@needs_databricks
def test_a_null_fraction_counts_rows_where_any_key_column_is_null() -> None:
    widget, _ = _tables()
    stmt = null_fraction_statement(widget, ["OwnerId", "Region"])

    for sql in (_databricks(stmt), _mssql(stmt)):
        assert "IS NULL OR" in sql
        assert "AS total" in sql
        assert "AS nulls" in sql


@needs_databricks
def test_a_self_reference_reads_one_table_through_two_key_sets() -> None:
    """A table referencing itself asks for the same Core table twice."""
    widget, _ = _tables()
    stmt = foreign_key_statement(widget, widget, ["OwnerId"], ["WidgetId"])

    sql = _databricks(stmt)
    assert sql.count("FROM dbo.`Widget`") == 2
    assert "WITH c AS" in sql and "p AS" in sql


# --- resolving spec names against a Core table -----------------------------


def test_a_column_named_in_another_case_still_resolves() -> None:
    """An overlay is typed by hand; the catalog's case is not binding."""
    widget, _ = _tables()
    table = core_table(widget)

    (col,) = columns_of(table, ["ownerid"])

    assert col.name == "OwnerId"


def test_a_column_the_table_does_not_have_is_named_in_the_error() -> None:
    widget, _ = _tables()
    table = core_table(widget)

    with pytest.raises(KeyError, match="Custodian"):
        columns_of(table, ["Custodian"])


def test_asking_twice_for_a_table_returns_the_one_already_built() -> None:
    """Both sides of a self-reference name the same table."""
    widget, _ = _tables()
    from sqlalchemy import MetaData

    md = MetaData()

    assert core_table(widget, md) is core_table(widget, md)


# --- schema translation ----------------------------------------------------


def test_schema_translation_is_identity_by_default() -> None:
    """Introspection recorded the source's own names, so a token resolves
    back to the name it was built from."""
    assert schema_translation(_spec()) == {"stele__dbo": "dbo"}


def test_schema_translation_takes_the_replicas_names() -> None:
    assert schema_translation(_spec(), {"dbo": "WidgetReplica"}) == {
        "stele__dbo": "WidgetReplica"
    }


# --- and they run ----------------------------------------------------------
#
# SQLite is neither backend, which is the point: a statement built for one
# warehouse would not run here at all. What these check is that the numbers
# come back where the caller reads them, which the compiled text cannot say.


@pytest.fixture
def loaded() -> Iterator[Engine]:
    """Widgets and owners, with one orphan and one null reference."""
    engine = create_engine("sqlite://", poolclass=StaticPool)
    with engine.begin() as conn:
        conn.execute(
            text(
                "CREATE TABLE Owner (OwnerId INTEGER, Region TEXT)",
            )
        )
        conn.execute(
            text(
                "CREATE TABLE Widget (WidgetId INTEGER, Name TEXT, "
                "OwnerId INTEGER, Region TEXT)"
            )
        )
        conn.execute(
            text("INSERT INTO Owner VALUES (1, 'north'), (2, 'south')")
        )
        conn.execute(
            text(
                "INSERT INTO Widget VALUES "
                "(10, 'alpha', 1, 'north'), "
                "(11, 'beta', 1, 'north'), "
                "(12, NULL, 2, 'south'), "
                "(13, 'gamma-long-name', 99, 'west'), "
                "(14, 'delta', NULL, 'east')"
            )
        )
    yield engine.execution_options(schema_translate_map={"stele__dbo": "main"})
    engine.dispose()


def _read(engine: Engine, stmt: Executable) -> dict[str, Any]:
    with engine.connect() as conn:
        row = conn.execute(stmt).mappings().first()
    assert row is not None
    return dict(row)


def test_a_profile_pass_reads_lengths_and_nulls_off_the_data(
    loaded: Engine,
) -> None:
    widget, _ = _tables()
    stmt = profile_statement(
        widget, [widget.columns[1]], include_distinct=True
    )

    row = _read(loaded, stmt)

    assert row["_total"] == 5
    assert row["_len_0"] == len("gamma-long-name")
    assert row["_null_0"] == 1
    assert row["_dist_0"] == 4


def test_a_sampled_profile_pass_reads_fewer_rows(loaded: Engine) -> None:
    widget, _ = _tables()
    stmt = profile_statement(widget, [widget.columns[1]], sample=2)

    assert _read(loaded, stmt)["_total"] == 2


def test_a_unique_key_comes_back_with_nothing_against_it(
    loaded: Engine,
) -> None:
    widget, _ = _tables()

    row = _read(loaded, primary_key_statement(widget, ["WidgetId"]))

    assert row == {
        "total_rows": 5,
        "null_rows": 0,
        "duplicate_groups": 0,
    }


def test_a_repeated_column_comes_back_with_its_duplicate_groups(
    loaded: Engine,
) -> None:
    """One region is shared by two widgets, which is one group, not two."""
    widget, _ = _tables()

    row = _read(loaded, primary_key_statement(widget, ["Region"]))

    assert row["duplicate_groups"] == 1


def test_containment_counts_the_distinct_child_keys_the_parent_has(
    loaded: Engine,
) -> None:
    """Nulls are out of the count; the orphan is in it and unmatched."""
    widget, owner = _tables()

    row = _read(
        loaded, foreign_key_statement(widget, owner, ["OwnerId"], ["OwnerId"])
    )

    assert row == {"distinct_values": 3, "matched_values": 2}


def test_the_orphan_query_names_the_value_the_parent_lacks(
    loaded: Engine,
) -> None:
    widget, owner = _tables()
    stmt = orphan_statement(widget, owner, ["OwnerId"], ["OwnerId"])

    with loaded.connect() as conn:
        rows = [dict(r) for r in conn.execute(stmt).mappings()]

    assert rows == [{"OwnerId": 99}]


def test_the_null_fraction_counts_rows_with_no_reference(
    loaded: Engine,
) -> None:
    widget, _ = _tables()

    row = _read(loaded, null_fraction_statement(widget, ["OwnerId"]))

    assert row == {"total": 5, "nulls": 1}


def test_a_profile_pass_reads_an_integer_range_off_the_data(
    loaded: Engine,
) -> None:
    """The range `infer --discover` prunes with, from the same pass."""
    widget, _ = _tables()
    stmt = profile_statement(widget, [widget.columns[2]])

    row = _read(loaded, stmt)

    assert (row["_min_0"], row["_max_0"]) == (1, 99)
