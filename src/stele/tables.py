"""Core ``Table`` objects built from the spec.

`infer` and `profile` read `model.yaml`, not the generated package, so the
statements they build have no mapped classes to hang from. They get Core
tables from here instead, carrying the same schema token the generated
classes carry. That is what lets one statement compile for the source
catalog and for the replica: the dialect decides quoting, row limiting and
sampling, and the binding's ``schema_translate_map`` decides the schema.

The catalog is not part of the name. A three-part `catalog.schema.table` is
Databricks-shaped and cannot address SQL Server, so the catalog belongs to
the connection, which is where the generated model layer already keeps it.

Columns carry no type. These statements count rows, measure lengths and
compare keys; none of that reads a value back through a type, and inventing
a type per column here would be a second mapping alongside `types.resolve`,
free to disagree with it.
"""

from __future__ import annotations

from collections.abc import Sequence
from decimal import Decimal
from typing import Any

from sqlalchemy import Column, MetaData, Table, func, literal_column
from sqlalchemy.exc import CompileError
from sqlalchemy.ext.compiler import compiles
from sqlalchemy.sql import ColumnElement, TableSample
from sqlalchemy.sql.compiler import SQLCompiler
from sqlalchemy.types import NullType

from .runtime.base import schema_token
from .spec import ModelSpec, TableSpec


def core_table(tbl: TableSpec, metadata: MetaData | None = None) -> Table:
    """The spec table as a Core ``Table``, addressed by schema token.

    Idempotent within one ``MetaData``, because a self-reference asks for
    the same table on both sides of the check.
    """
    md = MetaData() if metadata is None else metadata
    schema = schema_token(tbl.schema)
    existing = md.tables.get(f"{schema}.{tbl.name}")
    if existing is not None:
        return existing
    return Table(
        tbl.name,
        md,
        *(Column(c.name, NullType()) for c in tbl.columns),
        schema=schema,
    )


def columns_of(table: Table, names: Sequence[str]) -> list[Column[Any]]:
    """Resolve column names against a Core table, ignoring case.

    Names arriving here can come from an overlay someone typed, where the
    case need not match the catalog's. A name matching nothing raises: the
    statement cannot be built without it, and the caller reports that the
    same way it reports a statement the warehouse rejected.
    """
    by_lower = {c.name.lower(): c for c in table.columns}
    out: list[Column[Any]] = []
    for name in names:
        col = by_lower.get(name.lower())
        if col is None:
            raise KeyError(f"{table.name} has no column named {name!r}")
        out.append(col)
    return out


#: The seed every sample is drawn with. Over unchanged data, a fixed seed
#: draws the same rows on every run.
SAMPLE_SEED = 1


class Sample(TableSample):
    """A table cut to a percentage of its rows, drawn at random.

    SQLAlchemy spells ``TABLESAMPLE`` as ``system(5)`` after the alias.
    Databricks wants ``(5 PERCENT)`` between the table and its alias, and
    SQL Server wants ``SYSTEM (5 PERCENT)``. The compile rules below attach
    to this subclass rather than to ``TableSample``, so a ``tablesample()``
    built anywhere else keeps SQLAlchemy's rendering.
    """

    inherit_cache = True


def sampled(table: Table, percent: float) -> Sample:
    """`table` cut to about `percent` of its rows, drawn with `SAMPLE_SEED`.

    Any row in the table can land in the sample. ``LIMIT`` is not a sample:
    it reads whichever rows the engine produces first, which on Delta is
    whichever files it opens first, and those follow load order.

    Both numbers go into the SQL as literals, which is what the clause
    takes. As literals they are part of the statement's cache key, so two
    sample sizes never share a compiled statement.
    """
    if not 0 < percent <= 100:
        raise ValueError(f"a sample is above 0 and at most 100%: {percent}")
    literal = format(Decimal(str(percent)).normalize(), "f")
    return Sample._construct(
        table,
        sampling=func.system(literal_column(literal)),
        seed=literal_column(str(SAMPLE_SEED)),
    )


def _sample_numbers(
    element: Sample, compiler: SQLCompiler, **kw: Any
) -> tuple[str, str]:
    """The percentage and the seed, as the clause spells them."""
    (percent,) = element.sampling.clauses
    seed = element.seed
    if not isinstance(seed, ColumnElement):
        raise CompileError("a sample is drawn with a seed")
    return compiler.process(percent, **kw), compiler.process(seed, **kw)


@compiles(Sample, "databricks")
def _databricks_sample(
    element: Sample, compiler: SQLCompiler, **kw: Any
) -> str:
    """``t TABLESAMPLE (5 PERCENT) REPEATABLE (1) AS a``: the alias last."""
    kw.pop("asfrom", None)
    aliased = compiler.visit_alias(element, asfrom=True, **kw)
    suffix = compiler.get_render_as_alias_suffix(
        compiler.visit_alias(element, ashint=True)
    )
    if not aliased.endswith(suffix):
        raise CompileError(f"cannot place TABLESAMPLE in {aliased!r}")
    percent, seed = _sample_numbers(element, compiler, **kw)
    return (
        f"{aliased.removesuffix(suffix)} TABLESAMPLE ({percent} PERCENT) "
        f"REPEATABLE ({seed}){suffix}"
    )


@compiles(Sample, "mssql")
def _mssql_sample(element: Sample, compiler: SQLCompiler, **kw: Any) -> str:
    """``t AS a TABLESAMPLE SYSTEM (5 PERCENT) REPEATABLE (1)``.

    SQL Server samples pages rather than rows, so the rows in a sample
    arrive in clusters of whatever shares a page.
    """
    kw.pop("asfrom", None)
    aliased = compiler.visit_alias(element, asfrom=True, **kw)
    percent, seed = _sample_numbers(element, compiler, **kw)
    return (
        f"{aliased} TABLESAMPLE SYSTEM ({percent} PERCENT) REPEATABLE ({seed})"
    )


def schema_translation(
    spec: ModelSpec, schemas: dict[str, str] | None = None
) -> dict[str | None, str]:
    """A ``schema_translate_map`` covering every schema the spec names.

    Identity by default: introspection recorded the source catalog's own
    schema names, so a token resolves back to the name it was built from.
    A replica free to name its schemas differently passes `schemas`.
    """
    real = schemas or {}
    return {
        schema_token(s): real.get(s, s)
        for s in sorted({t.schema for t in spec.tables})
    }
