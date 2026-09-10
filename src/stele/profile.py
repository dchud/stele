"""Recover the type information federation threw away.

Lakehouse Federation reports every character column as STRING regardless of
whether the source declared NVARCHAR(2), NVARCHAR(50) or NVARCHAR(MAX). If you
generate the SQL Server replica straight from that, every column becomes
NVARCHAR(MAX): you lose index eligibility (900/1700-byte key limits), you blow
past the 8060-byte row limit, and the optimiser gets bad cardinality estimates.

Profiling gives an observed maximum, which is a lower bound on the true
declared width - so `generate` rounds up to a bucket, and anything important
should be pinned via `type_override` in the overlay once you can confirm it.
"""

from __future__ import annotations

import logging
import time
from collections.abc import Callable, Sequence
from contextlib import nullcontext
from dataclasses import dataclass, field
from typing import Any

from sqlalchemy import Engine, Integer, case, distinct, func, select
from sqlalchemy.sql import FromClause, Select

from .progress import Progress, format_duration
from .spec import ColumnSpec, ModelSpec, TableSpec
from .tables import core_table
from .types import MAX_NVARCHAR, estimated_row_bytes, is_range_type

log = logging.getLogger("stele.profile")

_STRINGY = ("string", "varchar", "char", "text")


def _is_stringy(col: ColumnSpec) -> bool:
    return (col.source_type or "").lower().startswith(_STRINGY)


def profiled_columns(tbl: TableSpec) -> list[ColumnSpec]:
    """The columns one pass has anything to say about.

    A character column for its width and null rate, an integer column for
    the range `infer --discover` prunes with. A column that is neither is
    not asked about, which is what keeps a table of timestamps from costing
    a query.
    """
    return [
        c
        for c in tbl.columns
        if _is_stringy(c) or is_range_type(c.source_type)
    ]


# Columns per query. Databricks handles wide aggregates fine, but very wide
# tables can hit expression-count limits, so batch them.
BATCH = 40

#: Attempts per statement. The connector retries at the HTTP layer already;
#: this covers what still reaches us as an exception, such as a connection
#: dropped while results were being read.
ATTEMPTS = 3

#: Seconds before the second attempt, doubling after that.
BACKOFF = 2.0

#: Tables that may fail in a row before the run stops. One table can fail
#: for reasons of its own; three in a row is the warehouse, the network or
#: the credential, and the remaining hundreds will fail the same way.
ABORT_AFTER = 3

#: Tables profiled between writes of the spec. Small enough that an
#: interrupt costs little, large enough that the file is not rewritten for
#: every table.
CHECKPOINT_EVERY = 10


class ProfileAborted(RuntimeError):
    """Enough tables failed in a row that the cause is not any one table."""


@dataclass
class ProfileReport:
    """What one pass did, including what it chose not to do."""

    #: Row count per table this run observed.
    counts: dict[str, int] = field(default_factory=dict)
    #: Tables a resumed run left alone, because they carry the
    #: observations this invocation would have produced.
    skipped: list[str] = field(default_factory=list)
    #: Tables that failed every attempt. Their columns keep whatever they
    #: had, so a later `--resume` picks them up.
    failed: list[str] = field(default_factory=list)


def already_profiled(tbl: TableSpec, *, include_distinct: bool) -> bool:
    """Whether `tbl` carries the observations this run would produce.

    Not "has been profiled at some point": a spec profiled without
    `--distinct` has row counts and lengths but no distinct counts, and a
    later `--distinct --resume` that skipped it would report success while
    recording nothing. So what counts as done depends on what is asked
    for.

    The row count is the completion marker because `_profile_table` sets
    it only once every batch has landed - a table interrupted midway does
    not carry one, and is redone whole.
    """
    if tbl.observed_row_count is None:
        return False
    if include_distinct:
        return all(
            c.observed_distinct is not None for c in profiled_columns(tbl)
        )
    return True


def tables_to_profile(
    spec: ModelSpec, *, resume: bool = False, include_distinct: bool = False
) -> tuple[list[TableSpec], list[str]]:
    """The tables this run will read, and the keys it will skip."""
    todo: list[TableSpec] = []
    skipped: list[str] = []
    for tbl in spec.tables:
        if not tbl.enabled:
            continue
        if resume and already_profiled(tbl, include_distinct=include_distinct):
            skipped.append(tbl.key)
        else:
            todo.append(tbl)
    return todo, skipped


def profile_statement(
    tbl: TableSpec,
    columns: Sequence[ColumnSpec],
    *,
    sample: int | None = None,
    include_distinct: bool = False,
) -> Select[Any]:
    """One aggregate pass over `columns`: rows, widths, nulls, ranges.

    The results are positional - `_len_0` describes `columns[0]` - because
    a column name is not always a usable result key and the caller already
    holds the list in order. Which labels a column gets follows from its
    type, so a reader walking the same list finds the same answers.
    """
    table = core_table(tbl)
    src: FromClause = table
    if sample:
        src = select(table).limit(sample).subquery()

    exprs: list[Any] = [func.count().label("_total")]
    for j, col in enumerate(columns):
        c = src.c[col.name]
        if _is_stringy(col):
            exprs.append(
                func.max(func.length(c, type_=Integer)).label(f"_len_{j}")
            )
            exprs.append(
                func.sum(case((c.is_(None), 1), else_=0)).label(f"_null_{j}")
            )
        if is_range_type(col.source_type):
            exprs.append(func.min(c).label(f"_min_{j}"))
            exprs.append(func.max(c).label(f"_max_{j}"))
        if include_distinct:
            exprs.append(func.count(distinct(c)).label(f"_dist_{j}"))
    return select(*exprs).select_from(src)


def profile_spec(
    spec: ModelSpec,
    engine: Engine,
    *,
    sample: int | None = None,
    include_distinct: bool = False,
    resume: bool = False,
    attempts: int = ATTEMPTS,
    checkpoint: Callable[[], None] | None = None,
    checkpoint_every: int = CHECKPOINT_EVERY,
    progress: Progress | None = None,
    report: ProfileReport | None = None,
) -> ProfileReport:
    """Populate observed_* fields, table by table.

    `checkpoint` is called every `checkpoint_every` tables. The caller
    passes something that writes the spec, so an interrupted run leaves
    behind what it had rather than nothing, and `resume` picks it up.

    Pass `report` to keep hold of it: an interrupt or a `ProfileAborted`
    leaves the caller with what the run managed before it stopped, which
    is what it needs to say before exiting.

    Raises `ProfileAborted` when `ABORT_AFTER` tables fail in a row.
    """
    report = ProfileReport() if report is None else report
    todo, skipped = tables_to_profile(
        spec, resume=resume, include_distinct=include_distinct
    )
    report.skipped.extend(skipped)

    consecutive = 0
    since_checkpoint = 0
    for tbl in todo:
        step = progress.step(tbl.key) if progress else nullcontext()
        try:
            with step:
                n = _profile_table(
                    tbl,
                    engine,
                    sample=sample,
                    include_distinct=include_distinct,
                    attempts=attempts,
                )
        except Exception as exc:
            report.failed.append(tbl.key)
            consecutive += 1
            log.warning("profile of %s failed: %s", tbl.key, _reason(exc))
            if consecutive >= ABORT_AFTER:
                raise ProfileAborted(
                    f"{consecutive} table(s) failed in a row, most recently "
                    f"{tbl.key}: {_reason(exc)}"
                ) from exc
            continue

        consecutive = 0
        if n is not None:
            report.counts[tbl.key] = n
        since_checkpoint += 1
        if checkpoint and since_checkpoint >= checkpoint_every:
            checkpoint()
            since_checkpoint = 0

    return report


def _reason(exc: BaseException) -> str:
    """The first line of a driver error, which is the informative one."""
    return str(exc).split("\n")[0][:200]


def _execute(engine: Engine, stmt: Select[Any], *, attempts: int) -> Any:
    """Run one statement, retrying what looks transient.

    The connector retries at the HTTP layer with its own policy; this
    covers what gets past it, most often a connection dropped mid-result.
    The last attempt raises, so the caller decides what a dead table means.
    """
    for attempt in range(1, attempts + 1):
        try:
            with engine.connect() as conn:
                return conn.execute(stmt).mappings().first()
        except Exception as exc:
            if attempt == attempts:
                raise
            delay = BACKOFF * 2 ** (attempt - 1)
            log.warning(
                "attempt %d of %d failed, retrying in %s: %s",
                attempt,
                attempts,
                format_duration(delay),
                _reason(exc),
            )
            time.sleep(delay)
    return None


def _profile_table(
    tbl: TableSpec,
    engine: Engine,
    *,
    sample: int | None,
    include_distinct: bool,
    attempts: int = ATTEMPTS,
) -> int | None:
    """Observe one table, or return None when it has nothing to observe.

    The row count is assigned last, after every batch has landed, so it
    doubles as the marker `already_profiled` reads. A table that raises
    partway through leaves the columns it managed and no row count, and a
    resumed run does it again from the top.
    """
    observable = profiled_columns(tbl)
    if not observable:
        return None

    total_rows: int | None = None
    for i in range(0, len(observable), BATCH):
        batch = observable[i : i + BATCH]
        stmt = profile_statement(
            tbl, batch, sample=sample, include_distinct=include_distinct
        )
        row = _execute(engine, stmt, attempts=attempts)
        if row is None:
            continue

        total_rows = int(row["_total"] or 0)
        for j, col in enumerate(batch):
            if _is_stringy(col):
                length = row.get(f"_len_{j}")
                col.observed_max_length = (
                    int(length) if length is not None else 0
                )
                nulls = row.get(f"_null_{j}")
                if total_rows:
                    col.observed_null_fraction = round(
                        (nulls or 0) / total_rows, 4
                    )
            if is_range_type(col.source_type):
                col.observed_min_value = _as_int(row.get(f"_min_{j}"))
                col.observed_max_value = _as_int(row.get(f"_max_{j}"))
            if include_distinct:
                d = row.get(f"_dist_{j}")
                col.observed_distinct = int(d) if d is not None else None

    tbl.observed_row_count = total_rows
    return total_rows


def _as_int(value: Any) -> int | None:
    """An observed bound, or None where the column held only nulls."""
    return None if value is None else int(value)


def profile_warnings(spec: ModelSpec) -> list[str]:
    """Flag things that will bite on the SQL Server side."""
    out: list[str] = []
    for tbl in spec.tables:
        if not tbl.enabled:
            continue
        unprofiled = [
            c.name
            for c in tbl.columns
            if (c.source_type or "").lower().startswith(_STRINGY)
            and c.observed_max_length is None
            and not c.type_override
        ]
        if unprofiled:
            out.append(
                f"{tbl.key}: {len(unprofiled)} string column(s) have no "
                "length and will become NVARCHAR(MAX) on SQL Server: "
                f"{', '.join(unprofiled[:6])}"
                + (" ..." if len(unprofiled) > 6 else "")
            )
        for c in tbl.columns:
            if c.observed_max_length and c.observed_max_length > MAX_NVARCHAR:
                out.append(
                    f"{tbl.key}.{c.name}: observed length "
                    f"{c.observed_max_length} "
                    f"exceeds {MAX_NVARCHAR}; will use NVARCHAR(MAX)"
                )
            if c.observed_max_length == 0 and c.observed_null_fraction == 1.0:
                out.append(
                    f"{tbl.key}.{c.name}: entirely NULL - "
                    "type cannot be inferred"
                )
        est = estimated_row_bytes(tbl.columns)
        if est > 8060:
            out.append(
                f"{tbl.key}: estimated in-row size {est} bytes exceeds "
                "SQL Server's "
                "8060-byte limit; some columns will need to go off-row"
            )
    return out
