"""What profiling reads, and what it warns about afterwards.

`profile_spec` issues Databricks SQL against three-part names, which SQLite
cannot resolve, so the query side is exercised through a recording engine
rather than a database. What that leaves untested is whether the SQL is valid
Databricks — which no test without a warehouse could tell us anyway.
`profile_warnings` is a pure function over a spec and is tested directly.
"""

from __future__ import annotations

from typing import Any

from stele.profile import BATCH, profile_spec, profile_warnings
from stele.spec import ColumnSpec, ModelSpec, TableSpec


def _col(name: str, type_: str = "string", **kw: Any) -> ColumnSpec:
    return ColumnSpec(name=name, source_type=type_, **kw)


def _spec(*tables: TableSpec) -> ModelSpec:
    return ModelSpec(catalog="cat", schemas=["dbo"], tables=list(tables))


class _Recorder:
    """Stands in for an Engine, remembering the SQL and answering canned rows."""

    def __init__(self, row: dict[str, Any] | None) -> None:
        self.row = row
        self.statements: list[str] = []

    # -- the sliver of the Engine/Connection surface profiling touches ----
    def connect(self) -> _Recorder:
        return self

    def __enter__(self) -> _Recorder:
        return self

    def __exit__(self, *exc: object) -> None:
        return None

    def execute(self, clause: Any) -> _Recorder:
        self.statements.append(str(clause))
        return self

    def mappings(self) -> _Recorder:
        return self

    def first(self) -> dict[str, Any] | None:
        return self.row


def _row(n: int, total: int = 100, **over: Any) -> dict[str, Any]:
    out: dict[str, Any] = {"_total": total}
    for i in range(n):
        out[f"_len_{i}"] = 12
        out[f"_null_{i}"] = 0
    out.update(over)
    return out


# --- what it reads ---------------------------------------------------------


def test_only_character_columns_are_profiled() -> None:
    tbl = TableSpec(
        name="Beacon",
        schema="dbo",
        columns=[_col("BeaconId", "bigint"), _col("BeaconName")],
    )
    engine = _Recorder(_row(1))

    counts = profile_spec(_spec(tbl), engine)  # type: ignore[arg-type]

    assert counts == {"dbo.Beacon": 100}
    assert tbl.column("BeaconName").observed_max_length == 12  # type: ignore[union-attr]
    assert tbl.column("BeaconId").observed_max_length is None  # type: ignore[union-attr]
    assert "BeaconId" not in engine.statements[0]


def test_a_table_with_no_character_columns_is_not_queried() -> None:
    tbl = TableSpec(
        name="Beacon", schema="dbo", columns=[_col("BeaconId", "bigint")]
    )
    engine = _Recorder(_row(0))

    assert profile_spec(_spec(tbl), engine) == {}  # type: ignore[arg-type]
    assert engine.statements == []


def test_a_disabled_table_is_skipped() -> None:
    tbl = TableSpec(
        name="Beacon",
        schema="dbo",
        columns=[_col("BeaconName")],
        enabled=False,
    )
    engine = _Recorder(_row(1))

    assert profile_spec(_spec(tbl), engine) == {}  # type: ignore[arg-type]
    assert engine.statements == []


def test_wide_tables_are_split_into_batches() -> None:
    """Very wide tables hit expression-count limits, hence the batching."""
    tbl = TableSpec(
        name="Wide",
        schema="dbo",
        columns=[_col(f"C{i}") for i in range(BATCH + 5)],
    )
    engine = _Recorder(_row(BATCH))

    profile_spec(_spec(tbl), engine)  # type: ignore[arg-type]

    assert len(engine.statements) == 2
    assert engine.statements[0].count("MAX(LENGTH(") == BATCH
    assert engine.statements[1].count("MAX(LENGTH(") == 5


def test_a_sample_wraps_the_source_in_a_limit() -> None:
    tbl = TableSpec(name="Beacon", schema="dbo", columns=[_col("Name")])
    engine = _Recorder(_row(1))

    profile_spec(_spec(tbl), engine, sample=1000)  # type: ignore[arg-type]

    assert "LIMIT 1000" in engine.statements[0]


def test_distinct_counts_are_opt_in() -> None:
    tbl = TableSpec(name="Beacon", schema="dbo", columns=[_col("Name")])
    plain, asked = _Recorder(_row(1)), _Recorder(_row(1, _dist_0=7))

    profile_spec(_spec(tbl), plain)  # type: ignore[arg-type]
    assert "COUNT(DISTINCT" not in plain.statements[0]

    profile_spec(_spec(tbl), asked, include_distinct=True)  # type: ignore[arg-type]
    assert "COUNT(DISTINCT" in asked.statements[0]
    assert tbl.column("Name").observed_distinct == 7  # type: ignore[union-attr]


def test_a_failed_query_leaves_the_table_unprofiled() -> None:
    """One unreadable table should not end the run."""

    class _Angry(_Recorder):
        def execute(self, clause: Any) -> _Recorder:
            raise RuntimeError("no such table")

    tbl = TableSpec(name="Beacon", schema="dbo", columns=[_col("Name")])

    assert profile_spec(_spec(tbl), _Angry(None)) == {}  # type: ignore[arg-type]
    assert tbl.column("Name").observed_max_length is None  # type: ignore[union-attr]


def test_a_null_fraction_is_recorded() -> None:
    tbl = TableSpec(name="Beacon", schema="dbo", columns=[_col("Name")])
    engine = _Recorder(_row(1, _null_0=25))

    profile_spec(_spec(tbl), engine)  # type: ignore[arg-type]

    assert tbl.column("Name").observed_null_fraction == 0.25  # type: ignore[union-attr]


# --- what it warns about ---------------------------------------------------


def test_an_unprofiled_string_column_is_reported() -> None:
    tbl = TableSpec(name="Beacon", schema="dbo", columns=[_col("Name")])

    warnings = profile_warnings(_spec(tbl))

    assert any("NVARCHAR(MAX)" in w and "Name" in w for w in warnings)


def test_a_column_with_an_override_is_not_reported() -> None:
    tbl = TableSpec(
        name="Beacon",
        schema="dbo",
        columns=[_col("Name", type_override="NVARCHAR(50)")],
    )

    assert profile_warnings(_spec(tbl)) == []


def test_a_length_beyond_the_bucket_range_is_reported() -> None:
    tbl = TableSpec(
        name="Beacon",
        schema="dbo",
        columns=[_col("Name", observed_max_length=9000)],
    )

    assert any("NVARCHAR(MAX)" in w for w in profile_warnings(_spec(tbl)))


def test_an_entirely_null_column_is_reported() -> None:
    tbl = TableSpec(
        name="Beacon",
        schema="dbo",
        columns=[
            _col("Name", observed_max_length=0, observed_null_fraction=1.0)
        ],
    )

    assert any("entirely NULL" in w for w in profile_warnings(_spec(tbl)))


def test_a_row_too_wide_for_the_replica_is_reported() -> None:
    tbl = TableSpec(
        name="Wide",
        schema="dbo",
        columns=[_col(f"C{i}", observed_max_length=4000) for i in range(20)],
    )

    assert any("8060-byte limit" in w for w in profile_warnings(_spec(tbl)))
