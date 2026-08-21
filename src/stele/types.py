"""Source type -> portable SQLAlchemy type.

The rule this module exists to enforce: never emit a dialect-specific type as
the primary type. Emit a generic SQLAlchemy type and attach a `.with_variant()`
only where the backends genuinely diverge. That is what lets one generated
class bind to both Databricks and SQL Server.

The federation caveat: Lakehouse Federation collapses source types on the way
in, so `NVARCHAR(50)`, `CHAR(2)` and `VARCHAR(MAX)` all arrive as STRING. We
cannot recover the original width from the catalog, so string columns get a
length from `stele profile` observations (or the overlay), falling back to
NVARCHAR(MAX) with a warning comment in the generated source.
"""

from __future__ import annotations

import math
import re
from dataclasses import dataclass

from .spec import ColumnSpec

# Round profiled lengths up to these, so small data drift does not force a
# schema change on the replica.
LENGTH_BUCKETS = [10, 20, 50, 100, 200, 255, 500, 1000, 2000, 4000]

# Above this, use NVARCHAR(MAX) rather than a bucket.
MAX_NVARCHAR = 4000


@dataclass
class RenderedType:
    """A type ready to be written into generated source."""

    # e.g. "String().with_variant(NVARCHAR(50), 'mssql')"
    expression: str
    # e.g. "int" / "str" / "datetime.datetime"
    python_type: str
    # Names this expression needs imported from sqlalchemy
    sa_imports: frozenset[str] = frozenset()
    # Names this expression needs from sqlalchemy.dialects.mssql
    mssql_imports: frozenset[str] = frozenset()
    # Modules needed for the annotation, e.g. "datetime", "decimal"
    stdlib_imports: frozenset[str] = frozenset()
    # Emitted as a trailing comment on the column line
    note: str | None = None
    # True if this type has no faithful SQL Server equivalent
    lossy: bool = False


_DECIMAL_RE = re.compile(r"^decimal\s*\(\s*(\d+)\s*,\s*(\d+)\s*\)", re.I)
_VARCHAR_RE = re.compile(r"^n?(?:var)?char\s*\(\s*(\d+|max)\s*\)", re.I)
_INTERVAL_RE = re.compile(r"^interval", re.I)


def bucket_length(observed: int | None) -> int | None:
    """Round an observed max length up to a stable bucket."""
    if observed is None or observed <= 0:
        return None
    if observed > MAX_NVARCHAR:
        return None  # caller uses MAX
    for b in LENGTH_BUCKETS:
        if observed <= b:
            return b
    return None


def _simple(expr: str, py: str, *sa: str, **kw) -> RenderedType:
    return RenderedType(
        expression=expr,
        python_type=py,
        sa_imports=frozenset(sa),
        **kw,
    )


def resolve(col: ColumnSpec) -> RenderedType:
    """Map one column to a portable type expression."""
    if col.type_override:
        return _from_override(col.type_override)

    t = (col.source_type or "").strip().lower()

    # --- exact integer types -------------------------------------------
    if t in {"tinyint", "byte"}:
        return _simple("SmallInteger()", "int", "SmallInteger")
    if t in {"smallint", "short"}:
        return _simple("SmallInteger()", "int", "SmallInteger")
    if t in {"int", "integer"}:
        return _simple("Integer()", "int", "Integer")
    if t in {"bigint", "long"}:
        return _simple("BigInteger()", "int", "BigInteger")

    # --- approximate numeric -------------------------------------------
    if t in {"float", "real"}:
        return _simple("Float(precision=24)", "float", "Float")
    if t in {"double", "double precision"}:
        return _simple("Float(precision=53)", "float", "Float")

    # --- exact numeric --------------------------------------------------
    m = _DECIMAL_RE.match(t)
    if m:
        p, s = int(m.group(1)), int(m.group(2))
        return RenderedType(
            expression=f"Numeric(precision={p}, scale={s}, asdecimal=True)",
            python_type="decimal.Decimal",
            sa_imports=frozenset({"Numeric"}),
            stdlib_imports=frozenset({"decimal"}),
        )
    if t in {"decimal", "numeric"}:
        return RenderedType(
            expression="Numeric(precision=38, scale=18, asdecimal=True)",
            python_type="decimal.Decimal",
            sa_imports=frozenset({"Numeric"}),
            stdlib_imports=frozenset({"decimal"}),
            note="unqualified DECIMAL in source; precision/scale assumed",
        )

    # --- boolean ---------------------------------------------------------
    if t in {"boolean", "bool"}:
        return _simple("Boolean()", "bool", "Boolean")

    # --- temporal --------------------------------------------------------
    if t == "date":
        return RenderedType(
            expression="Date()",
            python_type="datetime.date",
            sa_imports=frozenset({"Date"}),
            stdlib_imports=frozenset({"datetime"}),
        )
    if t in {"timestamp_ntz", "timestampntz"}:
        return RenderedType(
            expression=(
                "DateTime(timezone=False)"
                ".with_variant(DATETIME2(precision=6), 'mssql')"
            ),
            python_type="datetime.datetime",
            sa_imports=frozenset({"DateTime"}),
            mssql_imports=frozenset({"DATETIME2"}),
            stdlib_imports=frozenset({"datetime"}),
        )
    if t in {"timestamp", "timestamp_ltz"}:
        # Databricks TIMESTAMP is tz-aware (UTC); SQL Server datetime2
        # is naive. Kept tz-aware here so the difference is visible
        # rather than silent; the runtime helpers normalise to
        # UTC-naive when history.naive_utc.
        return RenderedType(
            expression=(
                "DateTime(timezone=True)"
                ".with_variant(DATETIME2(precision=6), 'mssql')"
            ),
            python_type="datetime.datetime",
            sa_imports=frozenset({"DateTime"}),
            mssql_imports=frozenset({"DATETIME2"}),
            stdlib_imports=frozenset({"datetime"}),
            note="Databricks TIMESTAMP is UTC-aware; mssql datetime2 is naive",
        )
    if _INTERVAL_RE.match(t):
        return RenderedType(
            expression="Interval()",
            python_type="datetime.timedelta",
            sa_imports=frozenset({"Interval"}),
            stdlib_imports=frozenset({"datetime"}),
            lossy=True,
            note="INTERVAL has no SQL Server equivalent",
        )

    # --- binary ----------------------------------------------------------
    if t in {"binary", "varbinary"}:
        return _simple("LargeBinary()", "bytes", "LargeBinary")

    # --- strings ---------------------------------------------------------
    m = _VARCHAR_RE.match(t)
    if m:
        raw = m.group(1).lower()
        if raw == "max":
            return _string_type(None, note="source declared VARCHAR(MAX)")
        return _string_type(int(raw))
    if t in {"string", "text", "varchar", "char"}:
        return _string_type(
            bucket_length(col.observed_max_length), profiled=True
        )

    # --- complex / unsupported -------------------------------------------
    if t.startswith(("array", "map", "struct")):
        return RenderedType(
            expression="JSON()",
            python_type="typing.Any",
            sa_imports=frozenset({"JSON"}),
            stdlib_imports=frozenset({"typing"}),
            lossy=True,
            note=(
                f"complex type {col.source_type!r}; not portable to SQL Server"
            ),
        )
    if t in {"variant", "object"}:
        return RenderedType(
            expression="JSON()",
            python_type="typing.Any",
            sa_imports=frozenset({"JSON"}),
            stdlib_imports=frozenset({"typing"}),
            lossy=True,
            note=f"{col.source_type} mapped to JSON",
        )

    # --- fallback ---------------------------------------------------------
    return _string_type(
        None,
        note=(
            f"unrecognised source type {col.source_type!r}; "
            "fell back to string"
        ),
    )


def _string_type(
    length: int | None, *, profiled: bool = False, note: str | None = None
) -> RenderedType:
    if length is None:
        return RenderedType(
            expression="String().with_variant(NVARCHAR(None), 'mssql')",
            python_type="str",
            sa_imports=frozenset({"String"}),
            mssql_imports=frozenset({"NVARCHAR"}),
            note=note
            or (
                "no length known; NVARCHAR(MAX) on mssql - run "
                "`stele profile` "
                "or pin type_override in the overlay"
            ),
        )
    n = note
    if profiled and n is None:
        n = f"length {length} from profiled data, not from the source catalog"
    return RenderedType(
        expression=(
            f"String({length}).with_variant(NVARCHAR({length}), 'mssql')"
        ),
        python_type="str",
        sa_imports=frozenset({"String"}),
        mssql_imports=frozenset({"NVARCHAR"}),
        note=n,
    )


_OVERRIDE_SA = {
    "string",
    "integer",
    "biginteger",
    "smallinteger",
    "numeric",
    "float",
    "boolean",
    "date",
    "datetime",
    "largebinary",
    "json",
    "interval",
    "text",
}
_OVERRIDE_MSSQL = {
    "nvarchar",
    "varchar",
    "datetime2",
    "uniqueidentifier",
    "bit",
    "money",
}

_PY_BY_ROOT = {
    "string": "str",
    "text": "str",
    "nvarchar": "str",
    "varchar": "str",
    "integer": "int",
    "biginteger": "int",
    "smallinteger": "int",
    "bit": "bool",
    "numeric": "decimal.Decimal",
    "money": "decimal.Decimal",
    "float": "float",
    "boolean": "bool",
    "date": "datetime.date",
    "datetime": "datetime.datetime",
    "datetime2": "datetime.datetime",
    "largebinary": "bytes",
    "json": "typing.Any",
    "interval": "datetime.timedelta",
    "uniqueidentifier": "str",
}


def _from_override(expr: str) -> RenderedType:
    """Accept a type expression from the overlay, e.g. 'NVARCHAR(50)'."""
    root = re.split(r"[(\s]", expr.strip(), maxsplit=1)[0]
    lowered = root.lower()
    py = _PY_BY_ROOT.get(lowered, "typing.Any")
    stdlib = set()
    if py.startswith("datetime"):
        stdlib.add("datetime")
    elif py.startswith("decimal"):
        stdlib.add("decimal")
    elif py.startswith("typing"):
        stdlib.add("typing")
    return RenderedType(
        expression=expr,
        python_type=py,
        sa_imports=frozenset({root} if lowered in _OVERRIDE_SA else set()),
        mssql_imports=frozenset(
            {root} if lowered in _OVERRIDE_MSSQL else set()
        ),
        stdlib_imports=frozenset(stdlib),
        note="type pinned by overlay",
    )


def estimated_row_bytes(cols: list[ColumnSpec]) -> int:
    """Rough in-row byte estimate, used to warn about SQL Server's 8060-byte
    row limit before you try to create the replica."""
    total = 0
    for c in cols:
        rt = resolve(c)
        m = re.search(r"NVARCHAR\((\d+)\)", rt.expression)
        if m:
            total += int(m.group(1)) * 2
        elif "NVARCHAR(None)" in rt.expression:
            total += 24  # off-row pointer
        elif (
            "BigInteger" in rt.expression
            or "Float(precision=53)" in rt.expression
        ):
            total += 8
        elif "Numeric" in rt.expression:
            total += 17
        elif "DateTime" in rt.expression:
            total += 8
        elif "Integer()" in rt.expression:
            total += 4
        else:
            total += 4
    return total


def ceil_pow2(n: int) -> int:
    return 1 << max(0, math.ceil(math.log2(max(n, 1))))
