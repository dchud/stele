"""Read the catalog and build a ModelSpec.

Strategy: prefer ``<catalog>.information_schema`` because it answers for the
whole schema in four queries rather than N per-table Inspector round trips,
which matters when the model is large. Fall back to the SQLAlchemy Inspector
when information_schema is unavailable or empty, which does happen on some
federated catalogs.

Constraint discovery is attempted but expected to come back empty on
federated foreign catalogs: Lakehouse Federation exposes no DDL surface, so
there is nothing to declare informational PK/FKs on. Use `stele infer` to
propose them and the overlay to record them.
"""

from __future__ import annotations

import datetime as dt
import logging
import re
from collections import defaultdict

from sqlalchemy import Engine, inspect, text

from .spec import (
    ColumnSpec,
    ForeignKeySpec,
    HistoryConfig,
    ModelSpec,
    TableSpec,
)

log = logging.getLogger("stele.introspect")

_IDENT_OK = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


def quote_ident(name: str) -> str:
    """Backtick-quote an identifier for Databricks SQL."""
    if not _IDENT_OK.match(name):
        if "`" in name:
            raise ValueError(
                f"refusing to quote identifier containing backtick: {name!r}"
            )
        return f"`{name}`"
    return name


def qualify(*parts: str | None) -> str:
    return ".".join(quote_ident(p) for p in parts if p)


# ---------------------------------------------------------------------------
# information_schema queries
# ---------------------------------------------------------------------------

_Q_TABLES = """
SELECT table_schema, table_name, table_type, comment
FROM {cat}.information_schema.tables
WHERE table_schema IN :schemas
ORDER BY table_schema, table_name
"""

_Q_COLUMNS = """
SELECT table_schema, table_name, column_name, ordinal_position,
       full_data_type, data_type, is_nullable, comment
FROM {cat}.information_schema.columns
WHERE table_schema IN :schemas
ORDER BY table_schema, table_name, ordinal_position
"""

_Q_CONSTRAINTS = """
SELECT tc.table_schema, tc.table_name, tc.constraint_name, tc.constraint_type,
       kcu.column_name, kcu.ordinal_position
FROM {cat}.information_schema.table_constraints tc
JOIN {cat}.information_schema.key_column_usage kcu
  ON tc.constraint_catalog = kcu.constraint_catalog
 AND tc.constraint_schema  = kcu.constraint_schema
 AND tc.constraint_name    = kcu.constraint_name
WHERE tc.table_schema IN :schemas
ORDER BY tc.constraint_name, kcu.ordinal_position
"""

_Q_REFERENTIAL = """
SELECT rc.constraint_schema, rc.constraint_name,
       ccu.table_schema AS referred_schema,
       ccu.table_name   AS referred_table,
       ccu.column_name  AS referred_column
FROM {cat}.information_schema.referential_constraints rc
JOIN {cat}.information_schema.constraint_column_usage ccu
  ON rc.unique_constraint_catalog = ccu.constraint_catalog
 AND rc.unique_constraint_schema  = ccu.constraint_schema
 AND rc.unique_constraint_name    = ccu.constraint_name
WHERE rc.constraint_schema IN :schemas
"""


def _in_list(values: list[str]) -> str:
    return (
        "(" + ", ".join("'" + v.replace("'", "''") + "'" for v in values) + ")"
    )


def _try(engine: Engine, sql: str) -> list[dict] | None:
    try:
        with engine.connect() as conn:
            return [dict(r) for r in conn.execute(text(sql)).mappings()]
    except Exception as exc:  # noqa: BLE001 - we want to degrade, not die
        log.warning(
            "query failed, will fall back: %s", str(exc).split("\n")[0][:200]
        )
        return None


# ---------------------------------------------------------------------------


def introspect(
    engine: Engine,
    catalog: str,
    schemas: list[str],
    *,
    history: HistoryConfig | None = None,
    include: re.Pattern | None = None,
    exclude: re.Pattern | None = None,
) -> ModelSpec:
    history = history or HistoryConfig()
    cat = quote_ident(catalog)
    schema_list = _in_list(schemas)

    rows = _try(
        engine, _Q_TABLES.format(cat=cat).replace(":schemas", schema_list)
    )
    if rows is None:
        log.info("information_schema unavailable; using SQLAlchemy Inspector")
        tables = _introspect_via_inspector(engine, catalog, schemas)
    else:
        tables = _introspect_via_info_schema(engine, cat, schema_list, rows)

    # filtering
    def keep(t: TableSpec) -> bool:
        if include and not include.search(t.name):
            return False
        return not (exclude and exclude.search(t.name))

    kept = [t for t in tables if keep(t)]
    if include or exclude:
        _warn_broken_pairs(tables, kept, history.suffix)
    tables = kept

    spec = ModelSpec(
        catalog=catalog,
        schemas=list(schemas),
        history=history,
        tables=sorted(tables, key=lambda t: (t.schema, t.name)),
        generated_at=dt.datetime.now(dt.UTC).isoformat(timespec="seconds"),
        source=f"{engine.url.host or 'unknown'}/{catalog}",
    )
    pair_history_tables(spec)
    return spec


def _warn_broken_pairs(
    before: list[TableSpec], after: list[TableSpec], suffix: str
) -> None:
    """Say when a filter split a pair.

    A pattern written for the primary table rarely matches its ``_history``
    companion, and losing the companion loses versioning for that table
    without failing anything. The reverse strands the history table.
    """
    kept = {t.key for t in after}
    dropped = {t.key for t in before} - kept
    low = suffix.lower()
    for t in after:
        companion = f"{t.schema}.{t.name}{suffix}"
        if companion in dropped:
            log.warning(
                "%s: %s was filtered out, so it generates with no history",
                t.key,
                companion,
            )
        if t.name.lower().endswith(low):
            primary = f"{t.schema}.{t.name[: -len(suffix)]}"
            if primary in dropped:
                log.warning(
                    "%s: %s was filtered out, so it generates nothing",
                    t.key,
                    primary,
                )


def _introspect_via_info_schema(
    engine: Engine, cat: str, schema_list: str, table_rows: list[dict]
) -> list[TableSpec]:
    tables: dict[tuple[str, str], TableSpec] = {}
    for r in table_rows:
        key = (r["table_schema"], r["table_name"])
        tables[key] = TableSpec(
            name=r["table_name"],
            schema=r["table_schema"],
            catalog=cat.strip("`"),
            table_type=(r.get("table_type") or "TABLE"),
            comment=r.get("comment"),
        )

    col_rows = (
        _try(
            engine, _Q_COLUMNS.format(cat=cat).replace(":schemas", schema_list)
        )
        or []
    )
    for r in col_rows:
        tbl = tables.get((r["table_schema"], r["table_name"]))
        if tbl is None:
            continue
        tbl.columns.append(
            ColumnSpec(
                name=r["column_name"],
                source_type=(
                    r.get("full_data_type") or r.get("data_type") or "string"
                ),
                nullable=str(r.get("is_nullable", "YES")).upper()
                in {"YES", "TRUE", "1"},
                ordinal=int(r.get("ordinal_position") or 0),
                comment=r.get("comment"),
            )
        )

    _attach_constraints(engine, cat, schema_list, tables)
    return list(tables.values())


def _attach_constraints(
    engine: Engine,
    cat: str,
    schema_list: str,
    tables: dict[tuple[str, str], TableSpec],
) -> None:
    con_rows = _try(
        engine, _Q_CONSTRAINTS.format(cat=cat).replace(":schemas", schema_list)
    )
    if not con_rows:
        log.info(
            "no declared PK/FK constraints found - expected on "
            "federated catalogs; "
            "run `stele infer` to propose them"
        )
        return

    grouped: dict[tuple, list[dict]] = defaultdict(list)
    for r in con_rows:
        grouped[
            (
                r["table_schema"],
                r["table_name"],
                r["constraint_name"],
                r["constraint_type"],
            )
        ].append(r)

    ref_rows = (
        _try(
            engine,
            _Q_REFERENTIAL.format(cat=cat).replace(":schemas", schema_list),
        )
        or []
    )
    referred: dict[tuple[str, str], list[dict]] = defaultdict(list)
    for r in ref_rows:
        referred[(r["constraint_schema"], r["constraint_name"])].append(r)

    for (schema, tname, cname, ctype), rows in grouped.items():
        tbl = tables.get((schema, tname))
        if tbl is None:
            continue
        cols = [
            r["column_name"]
            for r in sorted(rows, key=lambda x: x.get("ordinal_position") or 0)
        ]
        if ctype == "PRIMARY KEY":
            tbl.primary_key = cols
            tbl.primary_key_origin = "catalog"
        elif ctype == "FOREIGN KEY":
            refs = referred.get((schema, cname), [])
            if not refs:
                continue
            tbl.foreign_keys.append(
                ForeignKeySpec(
                    name=cname,
                    columns=cols,
                    referred_table=f"{refs[0]['referred_schema']}.{refs[0]['referred_table']}",
                    referred_columns=[r["referred_column"] for r in refs],
                    origin="catalog",
                    confidence=1.0,
                )
            )


def _introspect_via_inspector(
    engine: Engine, catalog: str, schemas: list[str]
) -> list[TableSpec]:
    insp = inspect(engine)
    out: list[TableSpec] = []
    for schema in schemas:
        for tname in insp.get_table_names(schema=schema):
            tbl = TableSpec(name=tname, schema=schema, catalog=catalog)
            for i, c in enumerate(
                insp.get_columns(tname, schema=schema), start=1
            ):
                tbl.columns.append(
                    ColumnSpec(
                        name=c["name"],
                        source_type=str(c["type"]).lower(),
                        nullable=bool(c.get("nullable", True)),
                        ordinal=i,
                        comment=c.get("comment"),
                    )
                )
            try:
                pk = insp.get_pk_constraint(tname, schema=schema) or {}
                if pk.get("constrained_columns"):
                    tbl.primary_key = list(pk["constrained_columns"])
                    tbl.primary_key_origin = "catalog"
            except Exception:  # noqa: BLE001
                pass
            try:
                for fk in insp.get_foreign_keys(tname, schema=schema) or []:
                    tbl.foreign_keys.append(
                        ForeignKeySpec(
                            name=fk.get("name"),
                            columns=list(fk["constrained_columns"]),
                            referred_table=(
                                f"{fk.get('referred_schema') or schema}"
                                f".{fk['referred_table']}"
                            ),
                            referred_columns=list(fk["referred_columns"]),
                            origin="catalog",
                            confidence=1.0,
                        )
                    )
            except Exception:  # noqa: BLE001
                pass
            out.append(tbl)
    return out


# ---------------------------------------------------------------------------
# history pairing
# ---------------------------------------------------------------------------


def pair_history_tables(spec: ModelSpec) -> None:
    """Match ``X`` to ``X_history`` and record the pairing both ways."""
    suffix = spec.history.suffix.lower()
    by_name = {(t.schema.lower(), t.name.lower()): t for t in spec.tables}

    for tbl in spec.tables:
        low = tbl.name.lower()
        if not low.endswith(suffix):
            continue
        base = tbl.name[: -len(suffix)]
        primary = by_name.get((tbl.schema.lower(), base.lower()))
        if primary is None:
            log.warning(
                "%s looks like a history table but %s.%s was not found",
                tbl.key,
                tbl.schema,
                base,
            )
            continue
        tbl.history_of = primary.key
        primary.history_table = tbl.key

        # A history row is identified by the business key plus its interval
        # start. If the primary has a known PK, reuse it.
        if not tbl.primary_key and primary.primary_key:
            start = spec.history.start_column
            if tbl.column(start):
                tbl.primary_key = [*primary.primary_key, start]
                tbl.primary_key_origin = "inferred"


def diff_columns(spec: ModelSpec) -> dict[str, dict[str, list[str]]]:
    """Compare each primary table's columns to its history table's.

    You said the history tables look like 'same model plus StartDate/EndDate'.
    This verifies that, and surfaces the cases where it is not true - which is
    where generated code would otherwise quietly produce wrong results.
    """
    report: dict[str, dict[str, list[str]]] = {}
    for primary in spec.primary_tables:
        if not primary.history_table:
            continue
        hist = spec.table(primary.history_table)
        if hist is None:
            continue
        pcols = {c.name.lower() for c in primary.columns}
        hcols = {c.name.lower() for c in hist.columns}
        expected = {
            spec.history.start_column.lower(),
            spec.history.end_column.lower(),
        }
        extra = sorted(hcols - pcols - expected)
        missing = sorted(pcols - hcols)
        absent_interval = sorted(expected - hcols)
        if extra or missing or absent_interval:
            report[primary.key] = {
                "history_only": extra,
                "missing_from_history": missing,
                "interval_columns_absent": absent_interval,
            }
    return report
