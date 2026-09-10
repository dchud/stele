"""What profiling reads, and what it warns about afterwards.

`profile_spec` wants a warehouse, so the query side is exercised through a
recording engine rather than a database. What the statements compile to is
`test_statements`; what is asserted here is the round trip - which tables get
read, how many passes a wide one takes, and how a row of answers lands back
in the spec. `profile_warnings` is a pure function over a spec and is tested
directly.
"""

from __future__ import annotations

from typing import Any

import pytest

from stele.profile import (
    BATCH,
    ProfileAborted,
    ProfileReport,
    already_profiled,
    profile_spec,
    profile_warnings,
)
from stele.spec import ColumnSpec, ModelSpec, TableSpec


def _col(name: str, type_: str = "string", **kw: Any) -> ColumnSpec:
    return ColumnSpec(name=name, source_type=type_, **kw)


def _spec(*tables: TableSpec) -> ModelSpec:
    return ModelSpec(catalog="cat", schemas=["dbo"], tables=list(tables))


class _Recorder:
    """Stands in for an Engine, keeping the statements and answering rows."""

    def __init__(self, row: dict[str, Any] | None) -> None:
        self.row = row
        self.statements: list[Any] = []

    # -- the sliver of the Engine/Connection surface profiling touches ----
    def connect(self) -> _Recorder:
        return self

    def __enter__(self) -> _Recorder:
        return self

    def __exit__(self, *exc: object) -> None:
        return None

    def execute(self, clause: Any) -> _Recorder:
        self.statements.append(clause)
        return self

    def mappings(self) -> _Recorder:
        return self

    def first(self) -> dict[str, Any] | None:
        return self.row


def _lengths_measured(stmt: Any) -> int:
    """How many columns one pass measures, by its result labels."""
    return sum(1 for c in stmt.selected_columns if c.key.startswith("_len_"))


def _row(n: int, total: int = 100, **over: Any) -> dict[str, Any]:
    out: dict[str, Any] = {"_total": total}
    for i in range(n):
        out[f"_len_{i}"] = 12
        out[f"_null_{i}"] = 0
    out.update(over)
    return out


# --- what it reads ---------------------------------------------------------


def test_a_column_is_asked_what_its_type_can_answer() -> None:
    """A width from the character column, a range from the integer one."""
    tbl = TableSpec(
        name="Beacon",
        schema="dbo",
        columns=[_col("BeaconId", "bigint"), _col("BeaconName")],
    )
    engine = _Recorder(
        {"_total": 100, "_min_0": 4, "_max_0": 91, "_len_1": 12, "_null_1": 0}
    )

    counts = profile_spec(_spec(tbl), engine).counts  # type: ignore[arg-type]

    assert counts == {"dbo.Beacon": 100}
    beacon_id = tbl.column("BeaconId")
    name = tbl.column("BeaconName")
    assert beacon_id.observed_min_value == 4  # type: ignore[union-attr]
    assert beacon_id.observed_max_value == 91  # type: ignore[union-attr]
    assert beacon_id.observed_max_length is None  # type: ignore[union-attr]
    assert name.observed_max_length == 12  # type: ignore[union-attr]
    assert name.observed_min_value is None  # type: ignore[union-attr]


def test_a_table_with_nothing_observable_is_not_queried() -> None:
    """A pass asks for widths and ranges; a timestamp has neither."""
    tbl = TableSpec(
        name="Beacon", schema="dbo", columns=[_col("SeenAt", "timestamp")]
    )
    engine = _Recorder(_row(0))

    assert profile_spec(_spec(tbl), engine).counts == {}  # type: ignore[arg-type]
    assert engine.statements == []


def test_a_disabled_table_is_skipped() -> None:
    tbl = TableSpec(
        name="Beacon",
        schema="dbo",
        columns=[_col("BeaconName")],
        enabled=False,
    )
    engine = _Recorder(_row(1))

    assert profile_spec(_spec(tbl), engine).counts == {}  # type: ignore[arg-type]
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
    assert _lengths_measured(engine.statements[0]) == BATCH
    assert _lengths_measured(engine.statements[1]) == 5


def test_a_distinct_count_reaches_the_spec_when_asked_for() -> None:
    tbl = TableSpec(name="Beacon", schema="dbo", columns=[_col("Name")])
    engine = _Recorder(_row(1, _dist_0=7))

    profile_spec(_spec(tbl), engine, include_distinct=True)  # type: ignore[arg-type]

    assert tbl.column("Name").observed_distinct == 7  # type: ignore[union-attr]


def test_a_failed_query_leaves_the_table_unprofiled() -> None:
    """One unreadable table should not end the run."""

    class _Angry(_Recorder):
        def execute(self, clause: Any) -> _Recorder:
            raise RuntimeError("no such table")

    tbl = TableSpec(name="Beacon", schema="dbo", columns=[_col("Name")])

    report = profile_spec(_spec(tbl), _Angry(None), attempts=1)  # type: ignore[arg-type]
    assert report.counts == {}
    assert report.failed == ["dbo.Beacon"]
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


# --- surviving a long run --------------------------------------------------


def test_a_table_is_done_when_it_carries_a_row_count() -> None:
    """The row count lands only after every batch, so it marks completion."""
    tbl = TableSpec(name="Beacon", schema="dbo", columns=[_col("Name")])
    assert not already_profiled(tbl, include_distinct=False)
    tbl.observed_row_count = 100
    assert already_profiled(tbl, include_distinct=False)


def test_distinct_counts_make_a_profiled_table_unfinished_again() -> None:
    """A pass without --distinct has not done what --distinct asks for."""
    tbl = TableSpec(
        name="Beacon",
        schema="dbo",
        columns=[_col("Name", observed_max_length=12)],
        observed_row_count=100,
    )
    assert already_profiled(tbl, include_distinct=False)
    assert not already_profiled(tbl, include_distinct=True)

    tbl.columns[0].observed_distinct = 7
    assert already_profiled(tbl, include_distinct=True)


def test_resume_reads_the_spec_rather_than_the_warehouse() -> None:
    done = TableSpec(
        name="Done",
        schema="dbo",
        columns=[_col("Name")],
        observed_row_count=100,
    )
    todo = TableSpec(name="Todo", schema="dbo", columns=[_col("Name")])
    engine = _Recorder(_row(1))

    report = profile_spec(
        _spec(done, todo),
        engine,  # type: ignore[arg-type]
        resume=True,
    )

    assert report.skipped == ["dbo.Done"]
    assert list(report.counts) == ["dbo.Todo"]
    assert len(engine.statements) == 1


def test_without_resume_every_table_is_read_again() -> None:
    done = TableSpec(
        name="Done",
        schema="dbo",
        columns=[_col("Name")],
        observed_row_count=100,
    )
    engine = _Recorder(_row(1))

    report = profile_spec(_spec(done), engine)  # type: ignore[arg-type]

    assert report.skipped == []
    assert len(engine.statements) == 1


def test_the_spec_is_written_as_the_run_goes() -> None:
    """An interrupt should cost the last few tables, not all of them."""
    tables = [
        TableSpec(name=f"T{i}", schema="dbo", columns=[_col("Name")])
        for i in range(5)
    ]
    writes: list[int] = []
    report = ProfileReport()
    profile_spec(
        _spec(*tables),
        _Recorder(_row(1)),  # type: ignore[arg-type]
        checkpoint=lambda: writes.append(len(report.counts)),
        checkpoint_every=2,
        report=report,
    )
    # Five tables, written after the second and the fourth.
    assert writes == [2, 4]


def test_a_statement_is_retried_before_the_table_is_given_up(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("stele.profile.BACKOFF", 0.0)

    class _Flaky(_Recorder):
        def __init__(self, row: dict[str, Any] | None, fail: int) -> None:
            super().__init__(row)
            self.left = fail

        def execute(self, clause: Any) -> _Recorder:
            if self.left:
                self.left -= 1
                raise RuntimeError("connection reset")
            return super().execute(clause)

    tbl = TableSpec(name="Beacon", schema="dbo", columns=[_col("Name")])
    engine = _Flaky(_row(1), fail=2)

    report = profile_spec(_spec(tbl), engine)  # type: ignore[arg-type]

    assert report.failed == []
    assert report.counts == {"dbo.Beacon": 100}


def test_enough_failures_in_a_row_stop_the_run(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Three in a row is the credential or the network, not the tables."""
    monkeypatch.setattr("stele.profile.BACKOFF", 0.0)

    class _Angry(_Recorder):
        def execute(self, clause: Any) -> _Recorder:
            raise RuntimeError("invalid access token")

    tables = [
        TableSpec(name=f"T{i}", schema="dbo", columns=[_col("Name")])
        for i in range(10)
    ]
    report = ProfileReport()
    with pytest.raises(ProfileAborted, match="invalid access token"):
        profile_spec(
            _spec(*tables),
            _Angry(None),  # type: ignore[arg-type]
            attempts=1,
            report=report,
        )

    # The caller keeps what the run managed before it stopped.
    assert report.failed == ["dbo.T0", "dbo.T1", "dbo.T2"]


def test_one_bad_table_among_good_ones_does_not_stop_the_run(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("stele.profile.BACKOFF", 0.0)

    class _OneBad(_Recorder):
        def execute(self, clause: Any) -> _Recorder:
            name = str(clause).lower()
            if "t1" in name:
                raise RuntimeError("no such table")
            return super().execute(clause)

    tables = [
        TableSpec(name=f"T{i}", schema="dbo", columns=[_col("Name")])
        for i in range(4)
    ]
    report = profile_spec(
        _spec(*tables),
        _OneBad(_row(1)),  # type: ignore[arg-type]
        attempts=1,
    )

    assert report.failed == ["dbo.T1"]
    assert sorted(report.counts) == ["dbo.T0", "dbo.T2", "dbo.T3"]
