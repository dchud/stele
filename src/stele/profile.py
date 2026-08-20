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

from sqlalchemy import Engine, text

from .introspect import qualify, quote_ident
from .spec import ModelSpec, TableSpec

log = logging.getLogger("stele.profile")

_STRINGY = ("string", "varchar", "char", "text")

# Columns per query. Databricks handles wide aggregates fine, but very wide
# tables can hit expression-count limits, so batch them.
BATCH = 40


def profile_spec(
    spec: ModelSpec,
    engine: Engine,
    *,
    sample: int | None = None,
    include_distinct: bool = False,
) -> dict[str, int]:
    """Populate observed_* fields on string columns. Returns per-table row counts."""
    counts: dict[str, int] = {}
    for tbl in spec.tables:
        if not tbl.enabled:
            continue
        n = _profile_table(spec, tbl, engine, sample=sample, include_distinct=include_distinct)
        if n is not None:
            counts[tbl.key] = n
    return counts


def _profile_table(
    spec: ModelSpec,
    tbl: TableSpec,
    engine: Engine,
    *,
    sample: int | None,
    include_distinct: bool,
) -> int | None:
    string_cols = [
        c for c in tbl.columns if (c.source_type or "").lower().startswith(_STRINGY)
    ]
    if not string_cols:
        return None

    fq = qualify(spec.catalog, tbl.schema, tbl.name)
    src = f"(SELECT * FROM {fq} LIMIT {int(sample)})" if sample else fq

    total_rows: int | None = None
    for i in range(0, len(string_cols), BATCH):
        batch = string_cols[i : i + BATCH]
        exprs = ["COUNT(*) AS _total"]
        for j, col in enumerate(batch):
            q = quote_ident(col.name)
            exprs.append(f"MAX(LENGTH({q})) AS _len_{j}")
            exprs.append(f"SUM(CASE WHEN {q} IS NULL THEN 1 ELSE 0 END) AS _null_{j}")
            if include_distinct:
                exprs.append(f"COUNT(DISTINCT {q}) AS _dist_{j}")

        sql = f"SELECT {', '.join(exprs)} FROM {src} t"
        try:
            with engine.connect() as conn:
                row = conn.execute(text(sql)).mappings().first()
        except Exception as exc:  # noqa: BLE001
            log.warning("profile of %s failed: %s", tbl.key, str(exc).split("\n")[0][:200])
            continue
        if row is None:
            continue

        total_rows = int(row["_total"] or 0)
        for j, col in enumerate(batch):
            length = row.get(f"_len_{j}")
            col.observed_max_length = int(length) if length is not None else 0
            nulls = row.get(f"_null_{j}")
            if total_rows:
                col.observed_null_fraction = round((nulls or 0) / total_rows, 4)
            if include_distinct:
                d = row.get(f"_dist_{j}")
                col.observed_distinct = int(d) if d is not None else None

    return total_rows


def profile_warnings(spec: ModelSpec) -> list[str]:
    """Flag things that will bite on the SQL Server side."""
    from .types import MAX_NVARCHAR, bucket_length, estimated_row_bytes

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
                f"{tbl.key}: {len(unprofiled)} string column(s) have no length and will "
                f"become NVARCHAR(MAX) on SQL Server: {', '.join(unprofiled[:6])}"
                + (" ..." if len(unprofiled) > 6 else "")
            )
        for c in tbl.columns:
            if c.observed_max_length and c.observed_max_length > MAX_NVARCHAR:
                out.append(
                    f"{tbl.key}.{c.name}: observed length {c.observed_max_length} "
                    f"exceeds {MAX_NVARCHAR}; will use NVARCHAR(MAX)"
                )
            if c.observed_max_length == 0 and c.observed_null_fraction == 1.0:
                out.append(f"{tbl.key}.{c.name}: entirely NULL - type cannot be inferred")
            if bucket_length(c.observed_max_length) and c.observed_null_fraction == 0.0:
                pass
        est = estimated_row_bytes(tbl.columns)
        if est > 8060:
            out.append(
                f"{tbl.key}: estimated in-row size {est} bytes exceeds SQL Server's "
                "8060-byte limit; some columns will need to go off-row"
            )
    return out
