"""Core ``Table`` objects built from the spec.

`infer` and `profile` read `model.yaml`, not the generated package, so the
statements they build have no mapped classes to hang from. They get Core
tables from here instead, carrying the same schema token the generated
classes carry. That is what lets one statement compile for the source
catalog and for the replica: the dialect decides quoting and row limiting,
and the binding's ``schema_translate_map`` decides the schema.

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
from typing import Any

from sqlalchemy import Column, MetaData, Table
from sqlalchemy.sql import ColumnElement
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


def columns_of(table: Table, names: Sequence[str]) -> list[ColumnElement[Any]]:
    """Resolve column names against a Core table, ignoring case.

    Names arriving here can come from an overlay someone typed, where the
    case need not match the catalog's. A name matching nothing raises: the
    statement cannot be built without it, and the caller reports that the
    same way it reports a statement the warehouse rejected.
    """
    by_lower = {c.name.lower(): c for c in table.columns}
    out: list[ColumnElement[Any]] = []
    for name in names:
        col = by_lower.get(name.lower())
        if col is None:
            raise KeyError(f"{table.name} has no column named {name!r}")
        out.append(col)
    return out


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
