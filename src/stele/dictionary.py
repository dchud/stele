"""A tbls document rendered from the spec.

`tbls doc json:///path/to/dictionary.json ./out` renders what this writes:
a page per table, a page per viewpoint, and ER diagrams, with no database
connection at either end. The catalog is read once, by `introspect` and
`profile`; everything here comes out of `model.yaml`.

tbls fixes the format with a published JSON Schema that sets
`additionalProperties: false` on every object, so what stele knows has to
land in fields that already exist. Provenance fits: a key or a reference
is a constraint whose `def` is free text, and labels mark what the catalog
never declared. The observed statistics have no structured home and reach
the reader as prose in each column's `extra_def`.

Two placements are chosen for how tbls renders them rather than for where
they belong. Observed facts about a table go in its `comment`, because
tbls prints that at the top of the page and hides `def` behind a
disclosure in a SQL fence. Every reference is emitted twice, as a relation
and as a foreign key constraint, because the relation draws the diagram
edge while the constraint is what puts the evidence on the page as text.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, fields, is_dataclass
from pathlib import Path
from typing import Any

from . import __version__
from .spec import ColumnSpec, ForeignKeySpec, ModelSpec, TableSpec

#: Labels stele attaches. tbls treats `virtual` as "not read from the
#: database", which is true of every one of these.
INFERRED = "inferred"
MANUAL = "manual"
UNVERIFIED = "unverified"
HISTORY = "history"

UNCERTAIN = (INFERRED, UNVERIFIED)


# --------------------------------------------------------------------------
# The tbls document, as its schema defines it
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class Label:
    name: str
    virtual: bool = True


@dataclass(frozen=True)
class Column:
    name: str
    type: str
    nullable: bool
    comment: str | None = None
    extra_def: str | None = None


@dataclass(frozen=True)
class Constraint:
    name: str
    type: str
    table: str
    def_: str
    columns: list[str] | None = None
    referenced_table: str | None = None
    referenced_columns: list[str] | None = None


@dataclass(frozen=True)
class Relation:
    table: str
    columns: list[str]
    parent_table: str
    parent_columns: list[str]
    def_: str
    virtual: bool = True


@dataclass(frozen=True)
class Table:
    name: str
    type: str
    columns: list[Column]
    comment: str | None = None
    constraints: list[Constraint] | None = None
    labels: list[Label] | None = None


@dataclass(frozen=True)
class Viewpoint:
    name: str
    desc: str
    tables: list[str] | None = None
    labels: list[str] | None = None


@dataclass(frozen=True)
class Driver:
    name: str
    database_version: str | None = None


@dataclass(frozen=True)
class Document:
    name: str
    tables: list[Table]
    desc: str | None = None
    driver: Driver | None = None
    labels: list[Label] | None = None
    relations: list[Relation] | None = None
    viewpoints: list[Viewpoint] | None = None


def to_json(doc: Document) -> str:
    """Serialise, dropping unset fields and renaming the one keyword."""
    return json.dumps(_plain(doc), indent=2) + "\n"


def _plain(value: Any) -> Any:
    if is_dataclass(value) and not isinstance(value, type):
        out: dict[str, Any] = {}
        for f in fields(value):
            got = getattr(value, f.name)
            if got is None:
                continue
            out["def" if f.name == "def_" else f.name] = _plain(got)
        return out
    if isinstance(value, list):
        return [_plain(v) for v in value]
    return value


# --------------------------------------------------------------------------
# Prose the schema has nowhere structured to put
# --------------------------------------------------------------------------


def column_statistics(col: ColumnSpec) -> str | None:
    """What `profile` observed, as one line for `extra_def`.

    The row count is on the table rather than repeated down every column.
    """
    parts: list[str] = []
    if col.observed_null_fraction is not None:
        parts.append(
            "no nulls"
            if col.observed_null_fraction == 0
            else f"{col.observed_null_fraction:.1%} null"
        )
    if col.observed_distinct is not None:
        parts.append(f"{col.observed_distinct:,} distinct")
    if col.observed_max_length is not None:
        parts.append(f"longest observed {col.observed_max_length:,}")
    if (
        col.observed_min_value is not None
        and col.observed_max_value is not None
    ):
        parts.append(
            f"range [{col.observed_min_value:,}, {col.observed_max_value:,}]"
        )
    return "; ".join(parts) or None


def _origin_phrase(origin: str) -> str:
    return {
        "catalog": "declared by the catalog",
        "inferred": "inferred by stele",
        "manual": "asserted in the overlay",
    }.get(origin, "of unrecorded origin")


def _verification_phrase(verified: bool | None) -> str:
    if verified is True:
        return "verified against the data"
    if verified is False:
        return "the data contradicted it"
    return "not verified against the data"


def table_comment(tbl: TableSpec, spec: ModelSpec) -> str | None:
    """The operator's prose, then the facts `profile` observed.

    tbls prints a table's `comment` uncollapsed at the top of its page and
    its `def` behind a disclosure, so the row count belongs here.
    """
    facts: list[str] = []
    if tbl.observed_row_count is not None:
        facts.append(f"{tbl.observed_row_count:,} rows.")
    if tbl.history_of:
        facts.append(f"SCD2 history for {tbl.history_of}.")
    elif tbl.history_table:
        facts.append(
            f"SCD2 history in {tbl.history_table}, keyed by "
            f"{spec.history.start_column}/{spec.history.end_column} - the "
            "same columns as this table plus the interval."
        )
    blocks = [b for b in (tbl.comment, " ".join(facts)) if b]
    return "\n\n".join(blocks) or None


def primary_key_constraint(tbl: TableSpec) -> Constraint | None:
    """The primary key, with where it came from and whether data agreed."""
    if not tbl.primary_key:
        return None
    cols = ", ".join(tbl.primary_key)
    return Constraint(
        name=f"PK_{tbl.name}",
        type="PRIMARY KEY",
        table=tbl.key,
        columns=list(tbl.primary_key),
        def_=(
            f"PRIMARY KEY ({cols}) - {_origin_phrase(tbl.primary_key_origin)}"
            f"; {_verification_phrase(tbl.primary_key_verified)}"
        ),
    )


def _evidence(fk: ForeignKeySpec) -> str:
    """Why the reference is believed, and how far the data bore it out.

    `evidence` records what argued for the proposal and `containment` what
    validation measured, so a validated reference has both to say.
    """
    parts = [_origin_phrase(fk.origin)]
    if fk.evidence:
        parts.append(fk.evidence)
    if fk.containment is not None:
        parts.append(f"containment {fk.containment:.3f}")
    return "; ".join(parts)


def foreign_key_constraint(tbl: TableSpec, fk: ForeignKeySpec) -> Constraint:
    """A reference as a constraint, which is what renders it as text."""
    cols = ", ".join(fk.columns)
    referred = ", ".join(fk.referred_columns)
    return Constraint(
        name=fk.name or f"FK_{tbl.name}_{'_'.join(fk.columns)}",
        type="FOREIGN KEY",
        table=tbl.key,
        columns=list(fk.columns),
        referenced_table=fk.referred_table,
        referenced_columns=list(fk.referred_columns),
        def_=(
            f"FOREIGN KEY ({cols}) REFERENCES {fk.referred_table} "
            f"({referred}) - {_evidence(fk)}"
        ),
    )


def relation(tbl: TableSpec, fk: ForeignKeySpec) -> Relation:
    """The same reference as a diagram edge, labelled with its evidence."""
    return Relation(
        table=tbl.key,
        columns=list(fk.columns),
        parent_table=fk.referred_table,
        parent_columns=list(fk.referred_columns),
        def_=_evidence(fk),
        virtual=fk.origin != "catalog",
    )


def table_labels(tbl: TableSpec) -> list[Label]:
    """What a reader should know before trusting the shape of this table."""
    names: list[str] = []
    origins = {fk.origin for fk in tbl.foreign_keys if fk.enabled}
    if tbl.primary_key_origin == "inferred" or "inferred" in origins:
        names.append(INFERRED)
    if tbl.primary_key_origin == "manual" or "manual" in origins:
        names.append(MANUAL)
    if tbl.primary_key and tbl.primary_key_verified is not True:
        names.append(UNVERIFIED)
    if tbl.history_table or tbl.history_of:
        names.append(HISTORY)
    return [Label(name=n) for n in names]


# --------------------------------------------------------------------------
# The document
# --------------------------------------------------------------------------


def build(spec: ModelSpec, *, include_history: bool = False) -> Document:
    """A tbls document describing `spec`.

    History tables are described on the table they belong to rather than
    listed alongside it, unless `include_history` asks for them: they
    double the table count and repeat their primary's columns. Either way
    the primary names its companion and carries the `history` label, so
    `tbls doc --label history` and the viewpoints can select on it.
    """
    sources = spec.tables if include_history else spec.primary_tables
    chosen = [t for t in sources if t.enabled]

    tables: list[Table] = []
    relations: list[Relation] = []
    for tbl in chosen:
        constraints: list[Constraint] = []
        pk = primary_key_constraint(tbl)
        if pk is not None:
            constraints.append(pk)
        for fk in tbl.foreign_keys:
            if not fk.enabled:
                continue
            constraints.append(foreign_key_constraint(tbl, fk))
            relations.append(relation(tbl, fk))
        labels = table_labels(tbl)
        tables.append(
            Table(
                name=tbl.key,
                type=tbl.table_type,
                comment=table_comment(tbl, spec),
                columns=[
                    Column(
                        name=col.name,
                        type=col.source_type,
                        nullable=col.nullable,
                        comment=col.comment,
                        extra_def=column_statistics(col),
                    )
                    for col in sorted(tbl.columns, key=lambda c: c.ordinal)
                ],
                constraints=constraints or None,
                labels=labels or None,
            )
        )

    return Document(
        name=spec.catalog or "model",
        desc=(
            "Written by stele from the catalog's metadata and a sample of "
            "its data. A key or reference labelled `inferred` was proposed "
            "by a heuristic rather than declared by the source; read the "
            "provenance on the constraint before relying on it."
        ),
        driver=Driver(name="stele", database_version=__version__),
        tables=tables,
        relations=relations or None,
        viewpoints=_viewpoints(tables),
    )


def _viewpoints(tables: list[Table]) -> list[Viewpoint] | None:
    """A view per schema where there are several, and one for what stele
    guessed at."""
    out: list[Viewpoint] = []
    schemas: dict[str, list[str]] = {}
    for tbl in tables:
        schema = tbl.name.rpartition(".")[0] or tbl.name
        schemas.setdefault(schema, []).append(tbl.name)
    if len(schemas) > 1:
        out.extend(
            Viewpoint(
                name=schema,
                desc=f"Tables in the {schema} schema.",
                tables=names,
            )
            for schema, names in sorted(schemas.items())
        )

    uncertain = any(
        label.name in UNCERTAIN
        for tbl in tables
        for label in (tbl.labels or [])
    )
    if uncertain:
        out.append(
            Viewpoint(
                name="Inferred shape",
                desc=(
                    "Tables whose primary key or references stele proposed "
                    "rather than read from the catalog, and those the data "
                    "has not confirmed. Read the provenance on each "
                    "constraint before relying on it."
                ),
                labels=list(UNCERTAIN),
            )
        )
    return out or None


def write(
    spec: ModelSpec, out: Path, *, include_history: bool = False
) -> Document:
    """Write the document to `out` and return it."""
    doc = build(spec, include_history=include_history)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(to_json(doc), encoding="utf-8")
    return doc
