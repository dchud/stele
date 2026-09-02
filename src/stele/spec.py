"""The intermediate model spec.

This is the durable artifact of the whole pipeline. `introspect` writes it,
humans edit an overlay on top of it, `generate` reads it. Keeping a reviewable
YAML file between the database and the generated code is what makes
regeneration safe: upstream drift shows up as a diff, and hand annotations
live in a separate file that is merged in rather than overwritten.
"""

from __future__ import annotations

import dataclasses
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal, cast

import yaml

SPEC_VERSION = 1


# --------------------------------------------------------------------------
# SCD2 configuration
# --------------------------------------------------------------------------


@dataclass
class HistoryConfig:
    """How the ``_history`` tables encode validity intervals.

    Every one of these settings changes query results silently rather than
    raising, so they are explicit here rather than guessed at generation time.
    """

    suffix: str = "_history"
    start_column: str = "StartDate"
    end_column: str = "EndDate"

    # How the currently-valid row marks its open end.
    #   "null"     -> EndDate IS NULL
    #   "sentinel" -> EndDate = end_sentinel
    end_open: Literal["null", "sentinel"] = "null"
    end_sentinel: str | None = "9999-12-31T00:00:00"

    # [start, end) or [start, end]. Decides < vs <= in as_of().
    interval: Literal["half_open", "closed"] = "half_open"

    # Does the currently-valid row also appear in the history table? If False,
    # as_of(now) has to UNION the primary table to be complete.
    current_row_in_history: bool = True

    # Treat all timestamps as UTC-naive. Databricks TIMESTAMP is tz-aware and
    # SQL Server datetime2 is naive; normalising here keeps as_of() from
    # shifting between backends.
    naive_utc: bool = True


# --------------------------------------------------------------------------
# Column / table / relationship
# --------------------------------------------------------------------------


@dataclass
class ColumnSpec:
    name: str
    # Raw type string as reported by the source catalog, kept verbatim for
    # audit purposes even after mapping.
    source_type: str
    nullable: bool = True
    ordinal: int = 0
    comment: str | None = None

    # Populated by `stele profile`. Used to pick a sane NVARCHAR length for the
    # SQL Server replica instead of defaulting everything to MAX.
    observed_max_length: int | None = None
    observed_null_fraction: float | None = None
    observed_distinct: int | None = None

    # Overlay escape hatch: pin an authoritative type, bypassing inference.
    # e.g. "NVARCHAR(50)" or "Numeric(18, 4)"
    type_override: str | None = None


@dataclass
class ForeignKeySpec:
    """A relationship. On federated catalogs these are never discovered from
    constraints; they come from `stele infer` proposals or hand editing."""

    columns: list[str]
    referred_table: str  # "schema.table" or bare table name
    referred_columns: list[str]
    name: str | None = None

    # "catalog" | "inferred" | "manual"
    origin: str = "manual"

    # Populated by `stele infer --validate`.
    confidence: float | None = None
    containment: float | None = None
    evidence: str | None = None

    # Names for the generated relationship() attributes. Defaults are derived
    # at generation time if left unset.
    relationship_name: str | None = None
    backref_name: str | None = None

    # Skip this FK during generation without deleting the record of it.
    enabled: bool = True


@dataclass
class TableSpec:
    name: str
    schema: str
    catalog: str | None = None
    comment: str | None = None
    table_type: str = "TABLE"
    columns: list[ColumnSpec] = field(default_factory=list)

    # Declared PK if the catalog has one; otherwise supplied by overlay or
    # proposed by `infer`.
    primary_key: list[str] = field(default_factory=list)
    primary_key_origin: str = "none"  # catalog | inferred | manual | none
    primary_key_verified: bool | None = None  # set by `infer --validate`

    foreign_keys: list[ForeignKeySpec] = field(default_factory=list)

    # Name of the paired history table, if one was found.
    history_table: str | None = None

    # Set on the history table itself, pointing back at its primary.
    history_of: str | None = None

    # Generated class name; defaults to a PascalCase form of `name`.
    class_name: str | None = None

    # Exclude from generation entirely.
    enabled: bool = True

    @property
    def key(self) -> str:
        return f"{self.schema}.{self.name}"

    def column(self, name: str) -> ColumnSpec | None:
        lowered = name.lower()
        for col in self.columns:
            if col.name.lower() == lowered:
                return col
        return None


@dataclass
class ModelSpec:
    spec_version: int = SPEC_VERSION
    catalog: str | None = None
    schemas: list[str] = field(default_factory=list)
    history: HistoryConfig = field(default_factory=HistoryConfig)
    tables: list[TableSpec] = field(default_factory=list)

    # Free-form provenance: when introspected, against what, by whom.
    generated_at: str | None = None
    source: str | None = None

    def table(self, key: str) -> TableSpec | None:
        lowered = key.lower()
        for tbl in self.tables:
            if tbl.key.lower() == lowered or tbl.name.lower() == lowered:
                return tbl
        return None

    @property
    def primary_tables(self) -> list[TableSpec]:
        return [t for t in self.tables if t.enabled and t.history_of is None]

    @property
    def history_tables(self) -> list[TableSpec]:
        return [
            t for t in self.tables if t.enabled and t.history_of is not None
        ]


# --------------------------------------------------------------------------
# YAML I/O
# --------------------------------------------------------------------------


def _strip_defaults(obj: Any) -> Any:
    """Drop None values and empty collections so the YAML stays readable."""
    if isinstance(obj, dict):
        out = {}
        for k, v in obj.items():
            if v is None:
                continue
            if isinstance(v, (list, dict)) and not v:
                continue
            out[k] = _strip_defaults(v)
        return out
    if isinstance(obj, list):
        return [_strip_defaults(v) for v in obj]
    return obj


def dump_spec(spec: ModelSpec, path: Path) -> None:
    payload = _strip_defaults(dataclasses.asdict(spec))
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as fh:
        fh.write(
            "# Generated by `stele introspect`. Safe to regenerate.\n"
            "# Do not hand-edit: put changes in your overlay file instead,\n"
            "# then re-run `stele generate --overlay <file>`.\n"
        )
        yaml.safe_dump(
            payload, fh, sort_keys=False, allow_unicode=True, width=100
        )


def _build[T](cls: type[T], data: dict[str, Any]) -> T:
    fields = {f.name for f in dataclasses.fields(cast(Any, cls))}
    unknown = set(data) - fields
    if unknown:
        raise ValueError(f"unknown keys for {cls.__name__}: {sorted(unknown)}")
    return cls(**{k: v for k, v in data.items() if k in fields})


def load_spec(path: Path) -> ModelSpec:
    with path.open(encoding="utf-8") as fh:
        raw = yaml.safe_load(fh) or {}
    return spec_from_dict(raw)


def spec_from_dict(raw: dict[str, Any]) -> ModelSpec:
    tables = []
    for tdata in raw.get("tables", []) or []:
        tdata = dict(tdata)
        cols = [
            _build(ColumnSpec, dict(c)) for c in tdata.pop("columns", []) or []
        ]
        fks = [
            _build(ForeignKeySpec, dict(f))
            for f in tdata.pop("foreign_keys", []) or []
        ]
        tbl = _build(TableSpec, tdata)
        tbl.columns = cols
        tbl.foreign_keys = fks
        tables.append(tbl)

    hist_raw = raw.get("history") or {}
    model = _build(
        ModelSpec,
        {k: v for k, v in raw.items() if k not in {"tables", "history"}},
    )
    model.history = _build(HistoryConfig, dict(hist_raw))
    model.tables = tables
    return model
