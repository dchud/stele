"""Propose primary and foreign keys, then check the proposals against data.

Necessary because a federated foreign catalog has no declarable constraints,
so nothing about the model's shape is recorded anywhere the catalog can see.
Name heuristics generate candidates; the validation queries turn a candidate
into evidence, or kill it. Anything that survives is written to the overlay
for a human to accept.

Two failure modes this is specifically designed to catch:
  * a column that *looks* like a PK but is not actually unique in the mirror
  * a column that looks like an FK but has poor containment, usually meaning
    it references something outside the mirrored subset
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field

from sqlalchemy import Engine, text

from .introspect import qualify
from .spec import ForeignKeySpec, ModelSpec, TableSpec

log = logging.getLogger("stele.infer")

# Types that can plausibly be a key. Excludes floats and complex types.
_KEYABLE = re.compile(
    r"^(int|integer|bigint|smallint|long|short|string|varchar|char|decimal)",
    re.I,
)


@dataclass
class PKProposal:
    table: str
    columns: list[str]
    score: float
    reason: str
    total_rows: int | None = None
    duplicate_groups: int | None = None
    null_rows: int | None = None

    @property
    def verified(self) -> bool | None:
        if self.duplicate_groups is None:
            return None
        return self.duplicate_groups == 0 and (self.null_rows or 0) == 0


@dataclass
class FKProposal:
    table: str
    columns: list[str]
    referred_table: str
    referred_columns: list[str]
    score: float
    reason: str
    distinct_values: int | None = None
    matched_values: int | None = None
    null_fraction: float | None = None

    @property
    def containment(self) -> float | None:
        if self.distinct_values is None or not self.distinct_values:
            return None
        return (self.matched_values or 0) / self.distinct_values


@dataclass
class InferenceResult:
    primary_keys: list[PKProposal] = field(default_factory=list)
    foreign_keys: list[FKProposal] = field(default_factory=list)


# ---------------------------------------------------------------------------
# name heuristics
# ---------------------------------------------------------------------------


def _norm(s: str) -> str:
    return re.sub(r"[^a-z0-9]", "", s.lower())


def _singular(s: str) -> str:
    low = s.lower()
    for suf, repl in (("ies", "y"), ("ses", "s"), ("s", "")):
        if low.endswith(suf) and len(low) > len(suf) + 2:
            return s[: -len(suf)] + repl
    return s


#: Words that mark a column as the key for the table it names.
_KEY_AFFIXES = ("id", "key", "code", "no", "num", "sk")


def _key_names(table_name: str) -> set[str]:
    """Key names built from the table name, with the affix on either side.

    ``WidgetId`` and ``IdWidget`` name the same thing, and a catalog can
    hold both conventions with a schema committed to each. Offering both
    rather than picking one keeps the rule independent of where a table
    sits; a wrong guess is overridable in the overlay.
    """
    base = _singular(table_name)
    out = set()
    for stem in {table_name, base}:
        for affix in _KEY_AFFIXES:
            out.add(_norm(stem + affix))
            out.add(_norm(affix + stem))
    return out


def propose_primary_keys(spec: ModelSpec) -> list[PKProposal]:
    out = []
    for tbl in spec.tables:
        if tbl.primary_key:
            continue
        if tbl.history_of is not None:
            continue  # handled by pairing once the primary is resolved
        candidates = _pk_candidates(tbl)
        if candidates:
            cols, score, reason = candidates[0]
            out.append(
                PKProposal(
                    table=tbl.key, columns=cols, score=score, reason=reason
                )
            )
    return out


def _pk_candidates(tbl: TableSpec) -> list[tuple[list[str], float, str]]:
    wanted = _key_names(tbl.name)
    scored: list[tuple[list[str], float, str]] = []
    for col in tbl.columns:
        if not _KEYABLE.match(col.source_type or ""):
            continue
        n = _norm(col.name)
        if n in wanted:
            scored.append(
                (
                    [col.name],
                    0.9,
                    f"name matches {tbl.name} plus a key affix",
                )
            )
        elif (
            n in {"id", "key", "sk", "rowid", "guid", "uuid"}
            and col.ordinal <= 3
        ):
            scored.append(
                ([col.name], 0.75, "generic key name in leading position")
            )
        elif (
            n.endswith("id")
            and _norm(tbl.name).startswith(n[:-2])
            and len(n) > 4
        ):
            scored.append(([col.name], 0.7, "prefix of table name plus 'id'"))
        elif (
            n.startswith("id")
            and _norm(tbl.name).startswith(n[2:])
            and len(n) > 4
        ):
            # Matching a stem rather than the whole name is what rescues a
            # plural the singulariser gets wrong: Boxes reduces to Boxe, so
            # no affix form of it spells IdBox, but Boxes does start with Box.
            scored.append(
                ([col.name], 0.7, "'id' plus a prefix of table name")
            )

    # non-nullable and early in the table is a good sign
    def bonus(
        entry: tuple[list[str], float, str],
    ) -> tuple[list[str], float, str]:
        cols, score, reason = entry
        col = tbl.column(cols[0])
        if col and not col.nullable:
            score += 0.05
        if col and col.ordinal == 1:
            score += 0.05
        return (cols, min(score, 0.99), reason)

    return sorted((bonus(e) for e in scored), key=lambda e: -e[1])


def propose_foreign_keys(spec: ModelSpec) -> list[FKProposal]:
    """Match a column against every other table's key columns by name."""
    primaries = spec.primary_tables
    # A key name says nothing about which schema it belongs to, and the same
    # table name in two schemas generates the same names, so a name maps to
    # every table that claims it rather than to whichever came first.
    targets: dict[str, list[tuple[TableSpec, str]]] = {}
    for tbl in primaries:
        keys = tbl.primary_key or []
        if len(keys) != 1:
            continue  # single-column only; composites go in the overlay
        targets_for = _key_names(tbl.name) | {_norm(keys[0])}
        for name in targets_for:
            targets.setdefault(name, []).append((tbl, keys[0]))

    out: list[FKProposal] = []
    for tbl in primaries:
        own_pk = {_norm(c) for c in tbl.primary_key}
        for col in tbl.columns:
            n = _norm(col.name)
            if n in own_pk:
                continue
            if not _KEYABLE.match(col.source_type or ""):
                continue
            # self-reference: real, but confirm by hand
            claimants = sorted(
                (c for c in targets.get(n, []) if c[0].key != tbl.key),
                key=lambda c: c[0].key,
            )
            if not claimants:
                continue

            # A relationship almost always stays inside its schema, so one
            # parent there settles it. With none, the name is genuinely
            # ambiguous and the choice belongs to whoever edits the overlay.
            same_schema = [c for c in claimants if c[0].schema == tbl.schema]
            chosen = same_schema or claimants
            chosen_keys = {c[0].key for c in chosen}
            passed_over = [
                c[0].key for c in claimants if c[0].key not in chosen_keys
            ]

            for parent, parent_col in chosen:
                child_t = (col.source_type or "").split("(")[0].lower()
                parent_c = parent.column(parent_col)
                parent_t = (
                    (parent_c.source_type if parent_c else "")
                    .split("(")[0]
                    .lower()
                )
                if child_t != parent_t:
                    score, reason = (
                        0.4,
                        "name match but type differs "
                        f"({child_t} vs {parent_t})",
                    )
                else:
                    score, reason = (
                        0.8,
                        f"name matches {parent.name} key, types agree",
                    )

                notes = []
                rivals = sorted(chosen_keys - {parent.key})
                if rivals:
                    score *= 0.5
                    notes.append("ambiguous with " + ", ".join(rivals))
                if passed_over:
                    notes.append(
                        "same schema preferred over " + ", ".join(passed_over)
                    )
                if notes:
                    reason = f"{reason} ({'; '.join(notes)})"

                out.append(
                    FKProposal(
                        table=tbl.key,
                        columns=[col.name],
                        referred_table=parent.key,
                        referred_columns=[parent_col],
                        score=round(score, 2),
                        reason=reason,
                    )
                )
    return out


def composite_key_tables(spec: ModelSpec) -> list[tuple[str, list[str]]]:
    """Tables no relationship can be proposed against, and why.

    A key name says which table it belongs to but not how many columns it
    spans, so matching a pair of columns by name is far likelier to be wrong
    than matching one. Those references belong in the overlay - but their
    absence from the proposals is otherwise silent, and an absence is a hard
    thing to notice.
    """
    return [
        (tbl.key, list(tbl.primary_key))
        for tbl in spec.primary_tables
        if len(tbl.primary_key) > 1
    ]


# ---------------------------------------------------------------------------
# data-driven validation
# ---------------------------------------------------------------------------


def validate_primary_key(
    engine: Engine, spec: ModelSpec, p: PKProposal
) -> PKProposal:
    tbl = spec.table(p.table)
    if tbl is None:
        return p
    fq = qualify(spec.catalog, tbl.schema, tbl.name)
    cols = ", ".join(f"t.{c}" for c in (_q(x) for x in p.columns))
    where_null = " OR ".join(f"t.{_q(c)} IS NULL" for c in p.columns)

    sql = f"""
    SELECT
      (SELECT COUNT(*) FROM {fq} t) AS total_rows,
      (SELECT COUNT(*) FROM {fq} t WHERE {where_null}) AS null_rows,
      (SELECT COUNT(*) FROM (
          SELECT {cols} FROM {fq} t GROUP BY {cols} HAVING COUNT(*) > 1
       ) d) AS duplicate_groups
    """
    row = _one(engine, sql)
    if row:
        p.total_rows = int(row["total_rows"])
        p.null_rows = int(row["null_rows"])
        p.duplicate_groups = int(row["duplicate_groups"])
        if p.verified:
            p.score = min(0.99, p.score + 0.15)
            p.reason += "; unique and non-null in data"
        else:
            p.score = 0.1
            p.reason += (
                f"; REJECTED - {p.duplicate_groups} duplicate group(s), "
                f"{p.null_rows} null row(s)"
            )
    return p


def validate_foreign_key(
    engine: Engine,
    spec: ModelSpec,
    p: FKProposal,
    *,
    sample: int | None = None,
) -> FKProposal:
    child = spec.table(p.table)
    parent = spec.table(p.referred_table)
    if child is None or parent is None:
        return p

    child_fq = qualify(spec.catalog, child.schema, child.name)
    parent_fq = qualify(spec.catalog, parent.schema, parent.name)
    ccols = [_q(c) for c in p.columns]
    pcols = [_q(c) for c in p.referred_columns]

    not_null = " AND ".join(f"{c} IS NOT NULL" for c in ccols)
    join_on = " AND ".join(
        f"c.{a} = p.{b}" for a, b in zip(ccols, pcols, strict=True)
    )
    limit = f"LIMIT {int(sample)}" if sample else ""

    sql = f"""
    WITH c AS (
      SELECT DISTINCT {", ".join(ccols)}
      FROM {child_fq}
      WHERE {not_null}
      {limit}
    ),
    p AS (
      SELECT DISTINCT {", ".join(pcols)} FROM {parent_fq}
    )
    SELECT
      (SELECT COUNT(*) FROM c) AS distinct_values,
      (SELECT COUNT(*) FROM c JOIN p ON {join_on}) AS matched_values
    """
    row = _one(engine, sql)
    if row:
        p.distinct_values = int(row["distinct_values"])
        p.matched_values = int(row["matched_values"])
        cont = p.containment
        if cont is None:
            p.reason += "; child column is entirely null"
            p.score = 0.2
        elif cont >= 0.999:
            p.score = min(0.99, p.score + 0.19)
            p.reason += f"; containment {cont:.3f}"
        elif cont >= 0.95:
            p.score = min(0.85, p.score + 0.05)
            p.reason += f"; containment {cont:.3f} - some orphans"
        else:
            p.score = round(p.score * cont, 2)
            p.reason += (
                f"; WEAK containment {cont:.3f} "
                "- parent may be outside the mirrored subset"
            )

    null_cols = " OR ".join(f"{c} IS NULL" for c in ccols)
    null_sql = f"""
    SELECT COUNT(*) AS total,
           SUM(CASE WHEN {null_cols} THEN 1 ELSE 0 END) AS nulls
    FROM {child_fq}
    """
    nrow = _one(engine, null_sql)
    if nrow and nrow["total"]:
        p.null_fraction = round((nrow["nulls"] or 0) / nrow["total"], 4)
    return p


def infer(
    spec: ModelSpec,
    engine: Engine | None = None,
    *,
    validate: bool = False,
    sample: int | None = None,
    min_score: float = 0.5,
) -> InferenceResult:
    result = InferenceResult(
        primary_keys=propose_primary_keys(spec),
        foreign_keys=[],
    )

    if validate and engine is not None:
        for p in result.primary_keys:
            log.info("validating PK %s(%s)", p.table, ", ".join(p.columns))
            validate_primary_key(engine, spec, p)

    # Apply accepted PKs in-memory so FK proposals have targets to match.
    for p in result.primary_keys:
        if p.score >= min_score:
            tbl = spec.table(p.table)
            if tbl is not None and not tbl.primary_key:
                tbl.primary_key = list(p.columns)
                tbl.primary_key_origin = "inferred"
                tbl.primary_key_verified = p.verified

    result.foreign_keys = propose_foreign_keys(spec)
    if validate and engine is not None:
        for f in result.foreign_keys:
            log.info(
                "validating FK %s(%s) -> %s",
                f.table,
                ", ".join(f.columns),
                f.referred_table,
            )
            validate_foreign_key(engine, spec, f, sample=sample)

    result.foreign_keys.sort(key=lambda f: (-f.score, f.table))
    return result


def to_foreign_key_specs(
    props: list[FKProposal], min_score: float
) -> dict[str, list[ForeignKeySpec]]:
    accepted = [p for p in props if p.score >= min_score]

    # Two parents for one column is not a relationship, it is a question.
    claims: dict[tuple[str, tuple[str, ...]], set[str]] = {}
    for p in accepted:
        claims.setdefault((p.table, tuple(p.columns)), set()).add(
            p.referred_table
        )
    contested = {key for key, parents in claims.items() if len(parents) > 1}
    for table_key, columns in sorted(contested):
        log.warning(
            "%s(%s) matches %s; left out, choose one in the overlay",
            table_key,
            ", ".join(columns),
            ", ".join(sorted(claims[(table_key, columns)])),
        )

    out: dict[str, list[ForeignKeySpec]] = {}
    for p in accepted:
        if (p.table, tuple(p.columns)) in contested:
            continue
        out.setdefault(p.table, []).append(
            ForeignKeySpec(
                columns=list(p.columns),
                referred_table=p.referred_table,
                referred_columns=list(p.referred_columns),
                origin="inferred",
                confidence=round(p.score, 2),
                containment=round(p.containment, 4)
                if p.containment is not None
                else None,
                evidence=p.reason,
            )
        )
    return out


def _q(name: str) -> str:
    from .introspect import quote_ident

    return quote_ident(name)


def _one(engine: Engine, sql: str) -> dict | None:
    try:
        with engine.connect() as conn:
            row = conn.execute(text(sql)).mappings().first()
            return dict(row) if row else None
    except Exception as exc:  # noqa: BLE001
        log.warning(
            "validation query failed: %s", str(exc).split("\n")[0][:200]
        )
        return None


def apply_to_spec(
    spec: ModelSpec, result: InferenceResult, *, min_score: float = 0.6
) -> int:
    """Write accepted proposals straight into the spec.

    Convenience for scripted use and tests. The normal path is to write an
    overlay with `write_overlay_stub`, review it, and apply that - proposals
    should get human eyes before they become model structure.
    """
    applied = 0
    for p in result.primary_keys:
        if p.score < min_score:
            continue
        tbl = spec.table(p.table)
        if tbl is not None and not tbl.primary_key:
            tbl.primary_key = list(p.columns)
            tbl.primary_key_origin = "inferred"
            tbl.primary_key_verified = p.verified
            applied += 1
    for table_key, fks in to_foreign_key_specs(
        result.foreign_keys, min_score
    ).items():
        tbl = spec.table(table_key)
        if tbl is None:
            continue
        existing = {
            (tuple(f.columns), f.referred_table) for f in tbl.foreign_keys
        }
        for f in fks:
            if (tuple(f.columns), f.referred_table) not in existing:
                tbl.foreign_keys.append(f)
                applied += 1
    return applied
