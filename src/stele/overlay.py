"""Merge a hand-maintained overlay onto an introspected spec.

This is the part that makes the tool usable against a federated catalog. The
introspected spec is disposable and regenerated whenever upstream drifts; the
overlay holds everything a human knows that the catalog does not - which, for
foreign tables, is most of the interesting structure.

Overlay shape mirrors the spec but every key is optional::

    history:
      end_open: sentinel
      end_sentinel: "9999-12-31T00:00:00"
    tables:
      sales.Customer:
        class_name: Customer
        primary_key: [CustomerId]
        columns:
          CustomerName:
            type_override: "NVARCHAR(200)"
        foreign_keys:
          - columns: [RegionId]
            referred_table: sales.Region
            referred_columns: [RegionId]
            relationship_name: region
            backref_name: customers
      sales.AuditLog:
        enabled: false
"""

from __future__ import annotations

import dataclasses
import json
import logging
from pathlib import Path
from typing import TYPE_CHECKING, Any

import yaml

from .spec import ColumnSpec, ForeignKeySpec, ModelSpec, TableSpec

if TYPE_CHECKING:  # annotations only; overlay does not depend on infer
    from .infer import FKProposal, PKProposal

log = logging.getLogger("stele.overlay")

# A tuple rather than a set: primary_key stamps an origin and
# primary_key_origin sets one, so the order they are applied in decides which
# survives, and set iteration order is not stable across processes.
_TABLE_SCALARS = (
    "class_name",
    "enabled",
    "primary_key",
    "primary_key_origin",
    "history_table",
    "history_of",
    "comment",
)

# Keys that hold structures rather than values, handled after the scalars.
_TABLE_CONTAINERS = frozenset({"columns", "foreign_keys", "foreign_keys_mode"})
_COLUMN_SCALARS = {
    "type_override",
    "nullable",
    "comment",
    "observed_max_length",
    # A table introspection never saw declares these; on a table it did see,
    # they correct a catalog that reported the column wrongly.
    "source_type",
    "ordinal",
}


def load_overlay(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    with path.open(encoding="utf-8") as fh:
        return yaml.safe_load(fh) or {}


def apply_overlay(spec: ModelSpec, overlay: dict[str, Any]) -> list[str]:
    """Mutate `spec` in place. Returns applied-change descriptions."""
    changes: list[str] = []

    for key, value in (overlay.get("history") or {}).items():
        if hasattr(spec.history, key):
            setattr(spec.history, key, value)
            changes.append(f"history.{key} = {value!r}")
        else:
            log.warning("overlay: unknown history key %r", key)

    for table_key, tdata in (overlay.get("tables") or {}).items():
        tbl = spec.table(table_key)
        if tbl is None:
            log.warning(
                "overlay: table %r not present in spec; ignored", table_key
            )
            continue
        changes.extend(_apply_table(tbl, tdata or {}))

    # Overlay may declare tables that introspection did not see at all.
    for table_key, tdata in (overlay.get("add_tables") or {}).items():
        if spec.table(table_key) is not None:
            continue
        schema, _, name = table_key.rpartition(".")
        tbl = TableSpec(
            name=name,
            schema=schema or (spec.schemas[0] if spec.schemas else ""),
        )
        spec.tables.append(tbl)
        changes.extend(_add_columns(tbl, tdata or {}))
        changes.extend(_apply_table(tbl, tdata or {}))
        if not tbl.columns:
            log.warning(
                "overlay: added table %s has no columns and cannot be "
                "mapped; give it a columns: entry",
                table_key,
            )
        changes.append(f"added table {table_key}")

    return changes


def _add_columns(tbl: TableSpec, tdata: dict[str, Any]) -> list[str]:
    """Build the columns of a table introspection never saw.

    `_apply_table` corrects columns that exist. A table added by hand has
    none, so its columns arrive here first and are corrected there after.
    Ordinals follow the order they are written in, which is the only order
    the file carries.
    """
    changes: list[str] = []
    for ordinal, (name, cdata) in enumerate(
        (tdata.get("columns") or {}).items(), start=1
    ):
        cdata = cdata or {}
        source_type = cdata.get("source_type")
        if not source_type and not cdata.get("type_override"):
            log.warning(
                "overlay: added column %s.%s has neither source_type nor "
                "type_override, so nothing says what it holds; ignored",
                tbl.key,
                name,
            )
            continue
        tbl.columns.append(
            ColumnSpec(
                name=name,
                source_type=source_type or "",
                nullable=bool(cdata.get("nullable", True)),
                ordinal=cdata.get("ordinal", ordinal),
            )
        )
        changes.append(
            f"{tbl.key}.{name} added as "
            f"{source_type or cdata['type_override']}"
        )
    return changes


def _apply_table(tbl: TableSpec, tdata: dict[str, Any]) -> list[str]:
    changes: list[str] = []

    stray = sorted(set(tdata) - set(_TABLE_SCALARS) - _TABLE_CONTAINERS)
    if stray:
        log.warning(
            "overlay: unknown key(s) %s on %s; ignored",
            ", ".join(repr(k) for k in stray),
            tbl.key,
        )

    for key in _TABLE_SCALARS:
        if key in tdata:
            setattr(tbl, key, tdata[key])
            # A hand-written key is manual unless the overlay says otherwise.
            if key == "primary_key" and "primary_key_origin" not in tdata:
                tbl.primary_key_origin = "manual"
            changes.append(f"{tbl.key}.{key} = {tdata[key]!r}")

    for col_name, cdata in (tdata.get("columns") or {}).items():
        col = tbl.column(col_name)
        if col is None:
            log.warning(
                "overlay: column %s.%s not found; ignored", tbl.key, col_name
            )
            continue
        for key, val in (cdata or {}).items():
            if key in _COLUMN_SCALARS:
                setattr(col, key, val)
                changes.append(f"{tbl.key}.{col_name}.{key} = {val!r}")
            else:
                log.warning("overlay: unknown column key %r", key)

    fks = tdata.get("foreign_keys")
    if fks is not None:
        mode = tdata.get("foreign_keys_mode", "replace")
        allowed = {f.name for f in dataclasses.fields(ForeignKeySpec)}
        incoming = []
        for f in fks:
            unknown = set(f) - allowed
            if unknown:
                log.warning(
                    "overlay: unknown FK keys %s on %s",
                    sorted(unknown),
                    tbl.key,
                )
            spec_fk = ForeignKeySpec(
                **{k: v for k, v in f.items() if k in allowed}
            )
            spec_fk.origin = f.get("origin", "manual")
            incoming.append(spec_fk)
        if mode == "replace":
            tbl.foreign_keys = incoming
            changes.append(
                f"{tbl.key}: replaced foreign keys ({len(incoming)})"
            )
        else:
            existing = {
                (tuple(f.columns), f.referred_table) for f in tbl.foreign_keys
            }
            for f in incoming:
                if (tuple(f.columns), f.referred_table) not in existing:
                    tbl.foreign_keys.append(f)
            changes.append(
                f"{tbl.key}: merged foreign keys (+{len(incoming)})"
            )

    return changes


def _flow(names: list[str]) -> str:
    """A YAML flow sequence that survives whatever the catalog named a column.

    Nothing stops a source column from being called ``Rate, %`` or ``ref: id``.
    Written bare, the comma ends the entry and the colon starts a mapping, and
    the file the operator edits is not the file that was meant. JSON is a
    subset of YAML, so its quoting is the shortest correct answer.
    """
    return json.dumps(names)


def write_overlay_stub(
    spec: ModelSpec,
    pk_props: list[PKProposal],
    fk_props: list[FKProposal],
    path: Path,
    *,
    min_score: float = 0.5,
) -> None:
    """Emit an editable overlay from inference output.

    High-confidence proposals are written live; anything below `min_score` is
    written commented out with its evidence, so nothing is silently dropped and
    nothing questionable is silently accepted.
    """
    lines: list[str] = [
        "# Overlay for `stele generate --overlay this-file`.",
        "# Written by `stele infer --write-overlay`. Hand-edit freely:",
        "# this file is never overwritten by `introspect`, only by",
        "# re-running infer with --force.",
        "#",
        "# Proposals below the confidence threshold are commented out",
        "# with the evidence that produced them. Uncomment to accept.",
        "",
    ]

    by_table: dict[str, dict[str, Any]] = {}
    for p in pk_props:
        by_table.setdefault(p.table, {})["pk"] = p
    for f in fk_props:
        by_table.setdefault(f.table, {}).setdefault("fks", []).append(f)

    lines.append("tables:")
    for table_key in sorted(by_table):
        entry = by_table[table_key]
        # A table whose every proposal was rejected has nothing to say yet.
        # Its key is commented out with them, so the file parses to what it
        # means rather than to an entry holding nothing.
        before = len(lines)
        lines.append(f"  {table_key}:")
        key_line = len(lines) - 1
        pk = entry.get("pk")
        if pk is not None:
            detail = (
                f"score={pk.score:.2f} rows={pk.total_rows} "
                f"dups={pk.duplicate_groups} nulls={pk.null_rows} "
                f":: {pk.reason}"
            )
            if pk.score >= min_score:
                lines.append(f"    # {detail}")
                lines.append(f"    primary_key: {_flow(pk.columns)}")
            else:
                lines.append(f"    # REJECTED {detail}")
                lines.append(f"    # primary_key: {_flow(pk.columns)}")

        fks = entry.get("fks") or []
        if fks:
            accepted = [f for f in fks if f.score >= min_score]
            rejected = [f for f in fks if f.score < min_score]
            if accepted:
                lines.append("    foreign_keys_mode: replace")
                lines.append("    foreign_keys:")
                for f in accepted:
                    cont = (
                        f"{f.containment:.3f}"
                        if f.containment is not None
                        else "n/a"
                    )
                    lines.append(
                        f"      # score={f.score:.2f} "
                        f"containment={cont} :: {f.reason}"
                    )
                    lines.append(f"      - columns: {_flow(f.columns)}")
                    lines.append(f"        referred_table: {f.referred_table}")
                    lines.append(
                        "        referred_columns: "
                        f"{_flow(f.referred_columns)}"
                    )
                    lines.append("        origin: inferred")
                    lines.append(f"        confidence: {f.score:.2f}")
            for f in rejected:
                cont = (
                    f"{f.containment:.3f}"
                    if f.containment is not None
                    else "n/a"
                )
                lines.append(
                    f"    # REJECTED score={f.score:.2f} "
                    f"containment={cont} :: {f.reason}"
                )
                lines.append(f"    #   - columns: {_flow(f.columns)}")
                lines.append(f"    #     referred_table: {f.referred_table}")
                lines.append(
                    f"    #     referred_columns: {_flow(f.referred_columns)}"
                )
        if not any(
            ln.lstrip().startswith(("primary_key:", "foreign_keys"))
            for ln in lines[before:]
        ):
            lines[key_line] = f"  # {table_key}:"
        lines.append("")

    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
