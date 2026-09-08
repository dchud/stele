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
from collections.abc import Sequence
from typing import Any

from sqlalchemy import Engine, Integer, case, distinct, func, select
from sqlalchemy.sql import FromClause, Select

from .spec import ColumnSpec, ModelSpec, TableSpec
from .tables import core_table

log = logging.getLogger("stele.profile")

_STRINGY = ("string", "varchar", "char", "text")

# Columns per query. Databricks handles wide aggregates fine, but very wide
# tables can hit expression-count limits, so batch them.
BATCH = 40


def profile_statement(
    tbl: TableSpec,
    columns: Sequence[ColumnSpec],
    *,
    sample: int | None = None,
    include_distinct: bool = False,
) -> Select[Any]:
    """One aggregate pass over `columns`: rows, lengths, nulls, distincts.

    The results are positional - `_len_0` describes `columns[0]` - because
    a column name is not always a usable result key and the caller already
    holds the list in order.
    """
    table = core_table(tbl)
    src: FromClause = table
    if sample:
        src = select(table).limit(sample).subquery()

    exprs: list[Any] = [func.count().label("_total")]
    for j, col in enumerate(columns):
        c = src.c[col.name]
        exprs.append(
            func.max(func.length(c, type_=Integer)).label(f"_len_{j}")
        )
        exprs.append(
            func.sum(case((c.is_(None), 1), else_=0)).label(f"_null_{j}")
        )
        if include_distinct:
            exprs.append(func.count(distinct(c)).label(f"_dist_{j}"))
    return select(*exprs).select_from(src)


def profile_spec(
    spec: ModelSpec,
    engine: Engine,
    *,
    sample: int | None = None,
    include_distinct: bool = False,
) -> dict[str, int]:
    """Populate observed_* fields on string columns.

    Returns a row count per table.
    """
    counts: dict[str, int] = {}
    for tbl in spec.tables:
        if not tbl.enabled:
            continue
        n = _profile_table(
            tbl, engine, sample=sample, include_distinct=include_distinct
        )
        if n is not None:
            counts[tbl.key] = n
    return counts


def _profile_table(
    tbl: TableSpec,
    engine: Engine,
    *,
    sample: int | None,
    include_distinct: bool,
) -> int | None:
    string_cols = [
        c
        for c in tbl.columns
        if (c.source_type or "").lower().startswith(_STRINGY)
    ]
    if not string_cols:
        return None

    total_rows: int | None = None
    for i in range(0, len(string_cols), BATCH):
        batch = string_cols[i : i + BATCH]
        stmt = profile_statement(
            tbl, batch, sample=sample, include_distinct=include_distinct
        )
        try:
            with engine.connect() as conn:
                row = conn.execute(stmt).mappings().first()
        except Exception as exc:  # noqa: BLE001
            log.warning(
                "profile of %s failed: %s",
                tbl.key,
                str(exc).split("\n")[0][:200],
            )
            continue
        if row is None:
            continue

        total_rows = int(row["_total"] or 0)
        for j, col in enumerate(batch):
            length = row.get(f"_len_{j}")
            col.observed_max_length = int(length) if length is not None else 0
            nulls = row.get(f"_null_{j}")
            if total_rows:
                col.observed_null_fraction = round(
                    (nulls or 0) / total_rows, 4
                )
            if include_distinct:
                d = row.get(f"_dist_{j}")
                col.observed_distinct = int(d) if d is not None else None

    return total_rows


def profile_warnings(spec: ModelSpec) -> list[str]:
    """Flag things that will bite on the SQL Server side."""
    from .types import MAX_NVARCHAR, estimated_row_bytes

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
