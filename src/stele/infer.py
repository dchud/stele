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

import copy
import logging
import re
from collections.abc import Callable, Sequence
from contextlib import nullcontext
from dataclasses import dataclass, field
from typing import Any, Literal

from sqlalchemy import (
    CTE,
    Engine,
    Executable,
    MetaData,
    and_,
    case,
    exists,
    func,
    literal,
    or_,
    select,
)
from sqlalchemy.sql import ColumnElement, Select

from .progress import Progress
from .spec import (
    DEFAULT_MIN_SCORE,
    ColumnSpec,
    ForeignKeySpec,
    ModelSpec,
    TableSpec,
)
from .tables import columns_of, core_table
from .types import is_keyable

log = logging.getLogger("stele.infer")

#: How many unmatched values to name. Enough to see a pattern,
#: few enough that the evidence line stays one line.
_ORPHAN_EXAMPLES = 5

#: Containment at or above which the data has not argued against a
#: reference. Below it, the child holds values the parent never had.
MIN_PLAUSIBLE_CONTAINMENT = 0.95

#: What a child carrying every column of a parent's composite key scores.
#: Below a single-column name match, because the whole key matching by name
#: is still a name matching; above the threshold, because all of it matched.
COMPOSITE_SCORE = 0.7

#: What a proposal with no name evidence starts at. Low enough that the
#: largest bonus validation adds (0.19, for containment at 0.999) leaves it
#: under `DEFAULT_MIN_SCORE`, so a discovery reaches the overlay for a
#: person to accept rather than applying itself.
DISCOVERY_SCORE = 0.3

#: How many discovered candidates are checked against the data. Each costs
#: warehouse queries, and range pruning alone can leave thousands.
DEFAULT_MAX_DISCOVERIES = 50


def _base_type(col: ColumnSpec | None) -> str:
    """The source type without its precision, for comparing two columns."""
    if col is None:
        return ""
    return (col.source_type or "").split("(")[0].strip().lower()


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
    #: A few of the child values absent from the parent. A ratio says a
    #: check failed; these say whether the cause is a handful of bad rows
    #: or a parent that is not in the mirrored subset.
    orphan_examples: list[str] = field(default_factory=list)
    #: What argued for this proposal: one column's name, every column of a
    #: composite key by name, or the statistics with no name evidence at
    #: all. It decides how the overlay presents a proposal it did not apply.
    basis: Literal["name", "composite", "data"] = "name"

    @property
    def containment(self) -> float | None:
        if self.distinct_values is None or not self.distinct_values:
            return None
        return (self.matched_values or 0) / self.distinct_values

    @property
    def contradicted(self) -> bool:
        """The data was read, and it argued against this reference."""
        cont = self.containment
        return cont is not None and cont < MIN_PLAUSIBLE_CONTAINMENT


@dataclass
class DiscoveryReport:
    """What `--discover` weighed, and what came through it.

    The counts are the point as much as the proposals are: whether range
    pruning is selective enough to be worth its queries is a question about
    a particular catalog, and these are the numbers that answer it.
    """

    #: Pairs the statistics could speak to: the types agree, and both
    #: columns carry an observation the other can be compared against.
    pairs: int = 0
    #: Pairs no statistic ruled out.
    survivors: int = 0
    #: Survivors kept, after the cap.
    proposed: int = 0


@dataclass
class InferenceResult:
    primary_keys: list[PKProposal] = field(default_factory=list)
    foreign_keys: list[FKProposal] = field(default_factory=list)
    #: Set when `--discover` ran.
    discovery: DiscoveryReport | None = None


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


def _keyless_tables(spec: ModelSpec) -> list[TableSpec]:
    """Tables an inferred key would be worth proposing for.

    A history table is not one: it takes its key from its primary once that
    is resolved, through pairing.
    """
    return [
        t for t in spec.tables if not t.primary_key and t.history_of is None
    ]


def _candidate_proposals(tbl: TableSpec) -> list[PKProposal]:
    """Every key this table's column names argue for, best first."""
    return [
        PKProposal(table=tbl.key, columns=cols, score=score, reason=reason)
        for cols, score, reason in _pk_candidates(tbl)
    ]


def propose_primary_keys(spec: ModelSpec) -> list[PKProposal]:
    out = []
    for tbl in _keyless_tables(spec):
        candidates = _candidate_proposals(tbl)
        if candidates:
            out.append(candidates[0])
    return out


def _pk_candidates(tbl: TableSpec) -> list[tuple[list[str], float, str]]:
    wanted = _key_names(tbl.name)
    scored: list[tuple[list[str], float, str]] = []
    for col in tbl.columns:
        if not is_keyable(col.source_type):
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
            if not is_keyable(col.source_type):
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
                child_t = _base_type(col)
                parent_t = _base_type(parent.column(parent_col))
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
    """Tables whose key spans more than one column.

    A single column matching a key name says which table the key belongs to
    but not how many columns it spans, so one name matching a composite key
    is not a proposal. `propose_composite_keys` is what reaches them, and
    only from a child carrying every one of the columns.
    """
    return [
        (tbl.key, list(tbl.primary_key))
        for tbl in spec.primary_tables
        if len(tbl.primary_key) > 1
    ]


def propose_composite_keys(spec: ModelSpec) -> list[FKProposal]:
    """References to a composite key, from a child carrying every key name.

    One column matching a key name says nothing about how many columns the
    key spans, which is why a lone name match against a composite key is
    not proposed. A child carrying all of them does say it: the whole key
    is there, under the parent's own names.
    """
    out: list[FKProposal] = []
    for parent in spec.primary_tables:
        keys = parent.primary_key
        if len(keys) < 2:
            continue
        pcols = [parent.column(k) for k in keys]
        if any(c is None for c in pcols):
            continue
        for child in spec.primary_tables:
            if child.key == parent.key:
                continue
            ccols = [child.column(k) for k in keys]
            if any(c is None for c in ccols):
                continue
            mismatched = [
                (cc.name, _base_type(cc), _base_type(pc))
                for cc, pc in zip(ccols, pcols, strict=True)
                if cc is not None and _base_type(cc) != _base_type(pc)
            ]
            if mismatched:
                name, child_t, parent_t = mismatched[0]
                score = round(COMPOSITE_SCORE / 2, 2)
                reason = (
                    f"carries every column of {parent.name}'s composite "
                    f"key, but {name} type differs "
                    f"({child_t} vs {parent_t})"
                )
            else:
                score = COMPOSITE_SCORE
                reason = (
                    f"carries every column of {parent.name}'s composite "
                    "key, types agree"
                )
            out.append(
                FKProposal(
                    table=child.key,
                    columns=[c.name for c in ccols if c is not None],
                    referred_table=parent.key,
                    referred_columns=list(keys),
                    score=score,
                    reason=reason,
                    basis="composite",
                )
            )
    return out


# ---------------------------------------------------------------------------
# candidates from statistics
# ---------------------------------------------------------------------------


def has_statistics(spec: ModelSpec) -> bool:
    """Whether `profile` has left discovery anything to prune with."""
    return any(
        c.observed_min_value is not None or c.observed_distinct is not None
        for t in spec.tables
        for c in t.columns
    )


def _reference_targets(
    spec: ModelSpec,
) -> list[tuple[TableSpec, ColumnSpec]]:
    """Tables a discovered reference could point at, and the column it hits.

    One key column, because containment against a tuple needs the tuple's
    statistics and `profile` observes columns. Not a key the data rejected,
    because a column that is not unique is not something to point at.
    """
    out: list[tuple[TableSpec, ColumnSpec]] = []
    for tbl in spec.primary_tables:
        if len(tbl.primary_key) != 1 or tbl.primary_key_verified is False:
            continue
        col = tbl.column(tbl.primary_key[0])
        if col is not None and is_keyable(col.source_type):
            out.append((tbl, col))
    return out


@dataclass
class _Fit:
    """What the statistics had to say about one candidate pair."""

    #: Whether any statistic both columns carry could have ruled it out.
    tested: bool = False
    #: Whether one did.
    ruled_out: bool = False
    #: The share of the parent's key space the child could occupy.
    coverage: float = 0.0
    evidence: str = ""


def _fit(child: ColumnSpec, parent: ColumnSpec) -> _Fit:
    """What the statistics say about the child's values inside the parent's.

    Every statistic both columns carry has to leave room for containment.
    The coverage is the share of the parent's key space the child could
    occupy, which is what ranks one survivor above another: a child using
    most of a parent's keys is a likelier reference than one using a sliver
    of it. Distinct counts say that better than a range does, so they win
    where both are known.

    A pair no statistic could have ruled out is not a discovery either.
    Nothing was tested, so nothing was found, and counting it would make
    pruning look selective where it only meant "not profiled".
    """
    fit = _Fit()
    notes: list[str] = []

    cmin, cmax = child.observed_min_value, child.observed_max_value
    pmin, pmax = parent.observed_min_value, parent.observed_max_value
    if (
        cmin is not None
        and cmax is not None
        and pmin is not None
        and pmax is not None
    ):
        fit.tested = True
        if cmin < pmin or cmax > pmax:
            fit.ruled_out = True
            return fit
        width = pmax - pmin + 1
        fit.coverage = (cmax - cmin + 1) / width if width > 0 else 0.0
        notes.append(f"range [{cmin}, {cmax}] inside [{pmin}, {pmax}]")

    child_n, parent_n = child.observed_distinct, parent.observed_distinct
    if child_n is not None and parent_n is not None:
        fit.tested = True
        if child_n > parent_n:
            fit.ruled_out = True
            return fit
        fit.coverage = child_n / parent_n if parent_n else 0.0
        notes.append(f"{child_n} distinct of {parent_n}")

    fit.evidence = "; ".join(notes)
    return fit


def propose_from_statistics(
    spec: ModelSpec,
    *,
    claimed: set[tuple[str, tuple[str, ...]]] | None = None,
    limit: int = DEFAULT_MAX_DISCOVERIES,
) -> tuple[list[FKProposal], DiscoveryReport]:
    """References the column names conceal, from profiled statistics alone.

    Name matching proposes nothing for an opaquely named column, for a
    table's own key, or for a self-reference, so those are exactly the
    references nothing ever checks. A pair survives here when every
    statistic the two columns carry is consistent with the child's values
    sitting inside the parent's - which costs no query, because only
    aggregates left the warehouse, during `profile`.

    Range containment is weak on its own: every small code sits inside a
    larger integer range. So survivors are ranked by how much of the
    parent's key space the child covers, and only `limit` of them go on to
    be checked against the data.

    A child column some name already argued for is left alone. The gap this
    fills is the columns names say nothing about, and a data-only rival to
    a name match is noise in front of the operator reading both.
    """
    taken = claimed or set()
    report = DiscoveryReport()
    scored: list[tuple[float, str, str, FKProposal]] = []

    for parent, pcol in _reference_targets(spec):
        for child in spec.primary_tables:
            for ccol in child.columns:
                if child.key == parent.key and ccol.name == pcol.name:
                    continue
                if (child.key, (ccol.name,)) in taken:
                    continue
                if not is_keyable(ccol.source_type):
                    continue
                if _base_type(ccol) != _base_type(pcol):
                    continue
                fit = _fit(ccol, pcol)
                if not fit.tested:
                    continue
                report.pairs += 1
                if fit.ruled_out:
                    continue
                report.survivors += 1
                scored.append(
                    (
                        fit.coverage,
                        child.key,
                        ccol.name,
                        FKProposal(
                            table=child.key,
                            columns=[ccol.name],
                            referred_table=parent.key,
                            referred_columns=[pcol.name],
                            score=DISCOVERY_SCORE,
                            reason=f"no name evidence; {fit.evidence}",
                            basis="data",
                        ),
                    )
                )

    scored.sort(key=lambda s: (-s[0], s[1], s[2], s[3].referred_table))
    kept = [entry[-1] for entry in scored[:limit]]
    report.proposed = len(kept)
    return kept, report


# ---------------------------------------------------------------------------
# data-driven validation
# ---------------------------------------------------------------------------


def primary_key_statement(
    tbl: TableSpec, columns: Sequence[str]
) -> Select[Any]:
    """Rows, null rows and duplicate groups for one candidate key."""
    table = core_table(tbl)
    cols = columns_of(table, columns)
    total = select(func.count()).select_from(table).scalar_subquery()
    nulls = (
        select(func.count())
        .select_from(table)
        .where(or_(*(c.is_(None) for c in cols)))
        .scalar_subquery()
    )
    groups = (
        select(*cols)
        .select_from(table)
        .group_by(*cols)
        .having(func.count() > 1)
        .subquery()
    )
    duplicates = select(func.count()).select_from(groups).scalar_subquery()
    return select(
        total.label("total_rows"),
        nulls.label("null_rows"),
        duplicates.label("duplicate_groups"),
    )


def _key_sets(
    child: TableSpec,
    parent: TableSpec,
    child_columns: Sequence[str],
    parent_columns: Sequence[str],
    *,
    sample: int | None,
) -> tuple[CTE, CTE, ColumnElement[bool]]:
    """The two key sets a containment check compares, and their join.

    CTEs rather than inline subqueries because the containment counts read
    the child set twice, once to size it and once to join it, and a name is
    what says the two are the same set.
    """
    md = MetaData()
    child_table = core_table(child, md)
    parent_table = core_table(parent, md)
    ccols = columns_of(child_table, child_columns)
    pcols = columns_of(parent_table, parent_columns)

    keys = (
        select(*ccols).distinct().where(and_(*(c.is_not(None) for c in ccols)))
    )
    if sample:
        keys = keys.limit(sample)
    c = keys.cte("c")
    p = select(*pcols).distinct().cte("p")
    join_on = and_(*(a == b for a, b in zip(c.c, p.c, strict=True)))
    return c, p, join_on


def foreign_key_statement(
    child: TableSpec,
    parent: TableSpec,
    child_columns: Sequence[str],
    parent_columns: Sequence[str],
    *,
    sample: int | None = None,
) -> Select[Any]:
    """How many distinct child keys there are, and how many the parent has."""
    c, p, join_on = _key_sets(
        child, parent, child_columns, parent_columns, sample=sample
    )
    return select(
        select(func.count())
        .select_from(c)
        .scalar_subquery()
        .label("distinct_values"),
        select(func.count())
        .select_from(c.join(p, join_on))
        .scalar_subquery()
        .label("matched_values"),
    )


def orphan_statement(
    child: TableSpec,
    parent: TableSpec,
    child_columns: Sequence[str],
    parent_columns: Sequence[str],
    *,
    sample: int | None = None,
) -> Select[Any]:
    """A few child keys the parent does not have."""
    c, p, join_on = _key_sets(
        child, parent, child_columns, parent_columns, sample=sample
    )
    return (
        select(*c.c)
        .where(~exists(select(literal(1)).select_from(p).where(join_on)))
        .limit(_ORPHAN_EXAMPLES)
    )


def null_fraction_statement(
    tbl: TableSpec, columns: Sequence[str]
) -> Select[Any]:
    """Rows in the child table, and rows where any key column is null."""
    table = core_table(tbl)
    cols = columns_of(table, columns)
    any_null = or_(*(c.is_(None) for c in cols))
    return select(
        func.count().label("total"),
        func.sum(case((any_null, 1), else_=0)).label("nulls"),
    ).select_from(table)


def validate_primary_key(
    engine: Engine, spec: ModelSpec, p: PKProposal
) -> PKProposal:
    tbl = spec.table(p.table)
    if tbl is None:
        return p
    try:
        stmt = primary_key_statement(tbl, p.columns)
    except KeyError as exc:
        log.warning("cannot check %s: %s", p.table, exc)
        return p

    row = _one(engine, stmt)
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

    try:
        containment = foreign_key_statement(
            child, parent, p.columns, p.referred_columns, sample=sample
        )
        orphans = orphan_statement(
            child, parent, p.columns, p.referred_columns, sample=sample
        )
        nulls = null_fraction_statement(child, p.columns)
    except KeyError as exc:
        log.warning(
            "cannot check %s(%s): %s", p.table, ", ".join(p.columns), exc
        )
        return p

    row = _one(engine, containment)
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
        elif cont >= MIN_PLAUSIBLE_CONTAINMENT:
            p.score = min(0.85, p.score + 0.05)
            p.reason += f"; containment {cont:.3f} - some orphans"
        else:
            p.score = round(p.score * cont, 2)
            p.reason += (
                f"; WEAK containment {cont:.3f} "
                "- parent may be outside the mirrored subset"
            )

    if p.distinct_values and (p.matched_values or 0) < p.distinct_values:
        p.orphan_examples = [
            ", ".join(str(v) for v in row.values())
            for row in _rows(engine, orphans)
        ]
        if p.orphan_examples:
            p.reason += f"; unmatched: {', '.join(p.orphan_examples)}"

    nrow = _one(engine, nulls)
    if nrow and nrow["total"]:
        p.null_fraction = round((nrow["nulls"] or 0) / nrow["total"], 4)
    return p


def infer(
    spec: ModelSpec,
    engine: Engine | None = None,
    *,
    validate: bool = False,
    sample: int | None = None,
    min_score: float = DEFAULT_MIN_SCORE,
    discover: bool = False,
    max_discoveries: int = DEFAULT_MAX_DISCOVERIES,
    progress: Callable[[int], Progress] | None = None,
) -> InferenceResult:
    """Propose keys and references, checking them where asked to.

    `progress` is called once per checking pass with the number of units
    that pass will run, and returns something to report them through. It
    is only consulted when there is a connection to spend, since without
    one nothing here takes measurable time.
    """
    result = InferenceResult(primary_keys=[], foreign_keys=[])
    checking = validate and engine is not None
    reporter = progress if checking and progress is not None else None

    keyless_tables = _keyless_tables(spec)
    pk_progress = reporter(len(keyless_tables)) if reporter else None
    for keyless in keyless_tables:
        candidates = _candidate_proposals(keyless)
        if not candidates:
            continue
        # Names alone pick the best candidate. Where the data can settle it,
        # a rejected candidate hands over to the next one rather than
        # costing the table its key - and with it every relationship that
        # needs the table as a target. When none survives, the strongest is
        # still what gets reported, because its counts are the evidence.
        chosen = candidates[0]
        if validate and engine is not None:
            step = (
                pk_progress.step(keyless.key) if pk_progress else nullcontext()
            )
            with step:
                for p in candidates:
                    log.debug(
                        "validating PK %s(%s)", p.table, ", ".join(p.columns)
                    )
                    validate_primary_key(engine, spec, p)
                    if p.verified:
                        chosen = p
                        break
        result.primary_keys.append(chosen)
    if pk_progress:
        pk_progress.finish()

    # A foreign key proposal needs a target, and a target needs a key, so
    # the accepted keys are applied to a copy. The caller's spec comes back
    # as it went in: `apply_to_spec` is what writes proposals into a spec,
    # and it can only count what it applied if nothing applied them first.
    working = copy.deepcopy(spec)
    for p in result.primary_keys:
        if p.score >= min_score:
            tbl = working.table(p.table)
            if tbl is not None and not tbl.primary_key:
                tbl.primary_key = list(p.columns)
                tbl.primary_key_origin = "inferred"
                tbl.primary_key_verified = p.verified

    result.foreign_keys = propose_foreign_keys(working)
    result.foreign_keys += propose_composite_keys(working)
    if discover:
        # Names get first claim on a column. What is left is the gap.
        claimed = {(f.table, tuple(f.columns)) for f in result.foreign_keys}
        found, result.discovery = propose_from_statistics(
            working, claimed=claimed, limit=max_discoveries
        )
        result.foreign_keys += found
    if validate and engine is not None:
        fk_progress = reporter(len(result.foreign_keys)) if reporter else None
        for f in result.foreign_keys:
            log.debug(
                "validating FK %s(%s) -> %s",
                f.table,
                ", ".join(f.columns),
                f.referred_table,
            )
            step = (
                fk_progress.step(f"{f.table} -> {f.referred_table}")
                if fk_progress
                else nullcontext()
            )
            with step:
                validate_foreign_key(engine, working, f, sample=sample)
        if fk_progress:
            fk_progress.finish()

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


def _one(engine: Engine, stmt: Executable) -> dict | None:
    try:
        with engine.connect() as conn:
            row = conn.execute(stmt).mappings().first()
            return dict(row) if row else None
    except Exception as exc:  # noqa: BLE001
        log.warning(
            "validation query failed: %s", str(exc).split("\n")[0][:200]
        )
        return None


def _rows(engine: Engine, stmt: Executable) -> list[dict]:
    try:
        with engine.connect() as conn:
            return [dict(r) for r in conn.execute(stmt).mappings()]
    except Exception as exc:  # noqa: BLE001
        log.warning(
            "validation query failed: %s", str(exc).split("\n")[0][:200]
        )
        return []


def validate_declared(
    spec: ModelSpec, engine: Engine, *, sample: int | None = None
) -> list[FKProposal]:
    """Check the references already written into the spec against the data.

    Proposals come from matching column names, so the references name
    matching cannot see are exactly the ones an operator declares by hand -
    opaque names, a table's own key, a composite, a self-reference. Those
    are the ones nothing else in the pipeline ever checks.
    """
    out: list[FKProposal] = []
    for tbl in spec.primary_tables:
        for fk in tbl.foreign_keys:
            if not fk.enabled:
                continue
            proposal = FKProposal(
                table=tbl.key,
                columns=list(fk.columns),
                referred_table=fk.referred_table,
                referred_columns=list(fk.referred_columns),
                score=1.0,
                reason=f"declared, origin {fk.origin}",
            )
            validate_foreign_key(engine, spec, proposal, sample=sample)
            out.append(proposal)
    return out


def apply_to_spec(
    spec: ModelSpec,
    result: InferenceResult,
    *,
    min_score: float = DEFAULT_MIN_SCORE,
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
