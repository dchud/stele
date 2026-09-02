"""Spec -> generated SQLAlchemy package.

Layout produced::

    <outdir>/
      __init__.py       re-exports every model, imports all modules so that
                        string-based relationship targets resolve
      _schemas.py       schema token constants
      customer.py       Customer + CustomerHistory
      order.py          Order + OrderHistory
      ...

One module per *primary* table, with its history class alongside, so a pair
that must stay consistent lives in one file and shows up as one diff.

Everything here is regenerable. Hand edits belong in the overlay, or in
subclasses defined outside the generated package.
"""

from __future__ import annotations

import keyword
import logging
import re
from dataclasses import dataclass, field
from pathlib import Path

from jinja2 import Environment, PackageLoader, StrictUndefined

from . import types as typemap
from .spec import ForeignKeySpec, ModelSpec, TableSpec

log = logging.getLogger("stele.generate")

# Not keywords, so `keyword.iskeyword` does not catch them, but an attribute
# of any of these names is still wrong: the first three are declarative API
# that a mapped column would shadow, and the last two shadow builtins inside
# the class body.
_RESERVED_ATTRS = {
    "metadata",
    "registry",
    "to_dict",
    "id",
    "type",
}


# ---------------------------------------------------------------------------
# naming
# ---------------------------------------------------------------------------


def pascal(name: str) -> str:
    if re.fullmatch(r"[A-Z][A-Za-z0-9]*", name):
        return name
    parts = re.split(r"[^A-Za-z0-9]+", name)
    return "".join(p[:1].upper() + p[1:] for p in parts if p)


def snake(name: str) -> str:
    s = re.sub(r"([A-Z]+)([A-Z][a-z])", r"\1_\2", name)
    s = re.sub(r"([a-z0-9])([A-Z])", r"\1_\2", s)
    return re.sub(r"[^A-Za-z0-9]+", "_", s).lower().strip("_")


def plural(name: str) -> str:
    if name.endswith(("s", "x", "z", "ch", "sh")):
        return name + "es"
    if name.endswith("y") and not name.endswith(
        ("ay", "ey", "iy", "oy", "uy")
    ):
        return name[:-1] + "ies"
    return name + "s"


def safe_attr(name: str) -> str:
    """A name usable as an attribute of a generated class.

    Databricks accepts column names Python cannot use as identifiers at all -
    ``Unit Price``, ``my-col``, ``2fast`` - so the whole name is checked
    rather than its first character, and every character that cannot appear
    where it stands becomes an underscore. A name that is a valid identifier
    but reserved only needs the trailing underscore.
    """
    if not name.isidentifier():
        name = "".join(c if ("_" + c).isidentifier() else "_" for c in name)
        if not name.isidentifier():
            # Empty, or opening on a character legal only after the first.
            name = "_" + name
    if keyword.iskeyword(name) or name in _RESERVED_ATTRS:
        return name + "_"
    return name


# ---------------------------------------------------------------------------
# render context
# ---------------------------------------------------------------------------


@dataclass
class RenderedColumn:
    attr: str
    column_name: str
    type_expr: str
    python_type: str
    nullable: bool
    primary_key: bool
    fk_target: str | None = None
    comment: str | None = None
    note: str | None = None


@dataclass
class RenderedRelationship:
    attr: str
    target_class: str
    kind: str  # "many_to_one" | "one_to_many" | "history"
    back_populates: str | None = None
    primaryjoin: str | None = None
    foreign_keys: str | None = None
    remote_side: str | None = None
    order_by: str | None = None
    viewonly: bool = False
    uselist: bool = True
    note: str | None = None


@dataclass
class RenderedClass:
    class_name: str
    table_name: str
    schema_token_const: str
    columns: list[RenderedColumn]
    relationships: list[RenderedRelationship] = field(default_factory=list)
    is_history: bool = False
    history_of_class: str | None = None
    scd2: dict | None = None
    mapper_primary_key: list[str] | None = None
    table_constraints: list[str] = field(default_factory=list)
    doc: str | None = None
    warnings: list[str] = field(default_factory=list)


@dataclass
class RenderedModule:
    module_name: str
    classes: list[RenderedClass]
    sa_imports: set[str] = field(default_factory=set)
    mssql_imports: set[str] = field(default_factory=set)
    stdlib_imports: set[str] = field(default_factory=set)
    typing_targets: set[str] = field(default_factory=set)


@dataclass
class GenerationReport:
    modules: list[str] = field(default_factory=list)
    classes: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    lossy_columns: list[str] = field(default_factory=list)
    tables_without_pk: list[str] = field(default_factory=list)
    unpaired_history: list[str] = field(default_factory=list)
    renamed_attrs: list[str] = field(default_factory=list)


class Generator:
    def __init__(self, spec: ModelSpec, *, preserve_names: bool = True):
        self.spec = spec
        self.preserve_names = preserve_names
        self.report = GenerationReport()
        self._class_names: dict[str, str] = {}
        self._module_names: dict[str, str] = {}
        self._module_of_class: dict[str, str] = {}
        self._assign_names()
        self._index_modules()

    # -- naming pass -----------------------------------------------------

    def _assign_names(self) -> None:
        used_classes: set[str] = set()
        used_modules: set[str] = set()
        for tbl in self.spec.tables:
            if not tbl.enabled:
                continue
            base = tbl.class_name or pascal(tbl.name)
            name = base
            i = 2
            while name in used_classes:
                name = f"{base}{i}"
                i += 1
            used_classes.add(name)
            self._class_names[tbl.key] = name

            if tbl.history_of is None:
                mod = snake(tbl.name) or "table"
                m = mod
                i = 2
                while m in used_modules:
                    m = f"{mod}_{i}"
                    i += 1
                used_modules.add(m)
                self._module_names[tbl.key] = m

    def _index_modules(self) -> None:
        for tbl in self.spec.tables:
            if not tbl.enabled:
                continue
            owner = tbl.history_of or tbl.key
            mod = self._module_names.get(owner)
            if mod:
                self._module_of_class[self._class_names[tbl.key]] = mod

    def class_name(self, key: str) -> str:
        return self._class_names.get(key, pascal(key.rpartition(".")[2]))

    def _base_attr(self, column_name: str) -> str:
        """The attribute name before anything Python forbids is taken out."""
        return column_name if self.preserve_names else snake(column_name)

    def attr_name(self, column_name: str) -> str:
        """What a column is called on the generated class.

        Anywhere a join, a descriptor or a `remote_side` names a column, it
        has to name the attribute rather than the column, or the two disagree
        the moment `--snake-case` is in play.
        """
        return safe_attr(self._base_attr(column_name))

    # -- column rendering ------------------------------------------------

    def _render_columns(
        self, tbl: TableSpec, mod: RenderedModule
    ) -> list[RenderedColumn]:
        pk = {c.lower() for c in tbl.primary_key}
        fk_by_col: dict[str, tuple[ForeignKeySpec, int]] = {}
        for fk in tbl.foreign_keys:
            if not fk.enabled:
                continue
            for i, c in enumerate(fk.columns):
                fk_by_col[c.lower()] = (fk, i)

        out: list[RenderedColumn] = []
        claimed: dict[str, str] = {}
        for col in sorted(tbl.columns, key=lambda c: c.ordinal):
            rt = typemap.resolve(col)
            mod.sa_imports |= rt.sa_imports
            mod.mssql_imports |= rt.mssql_imports
            mod.stdlib_imports |= rt.stdlib_imports

            fk_target = None
            hit = fk_by_col.get(col.name.lower())
            if hit is not None:
                fk, idx = hit
                parent = self.spec.table(fk.referred_table)
                # A reference over several columns is one claim about the
                # tuple, so it becomes a table-level constraint. Rendering it
                # per column would say each column references the parent on
                # its own, which is a different and untrue claim.
                if parent is not None and len(fk.columns) == 1:
                    token = f"{{{schema_const(parent.schema)}}}"
                    fk_target = (
                        f'f"{token}.{parent.name}.{fk.referred_columns[idx]}"'
                    )
                    mod.sa_imports.add("ForeignKey")

            if rt.lossy:
                self.report.lossy_columns.append(
                    f"{tbl.key}.{col.name}: {rt.note}"
                )

            attr = self.attr_name(col.name)
            if attr != self._base_attr(col.name):
                self.report.renamed_attrs.append(
                    f"{tbl.key}.{col.name} -> {attr}"
                )
            first = claimed.setdefault(attr, col.name)
            if first != col.name:
                self.report.warnings.append(
                    f"{tbl.key}: columns {first!r} and {col.name!r} both "
                    f"become the attribute {attr!r}, so only one of them is "
                    "mapped. Rename one of the source columns."
                )

            out.append(
                RenderedColumn(
                    attr=attr,
                    column_name=col.name,
                    type_expr=rt.expression,
                    python_type=rt.python_type,
                    nullable=col.nullable and col.name.lower() not in pk,
                    primary_key=col.name.lower() in pk,
                    comment=col.comment,
                    note=rt.note,
                )
            )
            out[-1].fk_target = fk_target
        return out

    def _render_table_constraints(
        self, tbl: TableSpec, mod: RenderedModule
    ) -> list[str]:
        """One ``ForeignKeyConstraint`` per multi-column reference."""
        out: list[str] = []
        for fk in tbl.foreign_keys:
            if not fk.enabled or len(fk.columns) == 1:
                continue
            parent = self.spec.table(fk.referred_table)
            if parent is None or not parent.enabled:
                continue
            token = f"{{{schema_const(parent.schema)}}}"
            cols = ", ".join(f'"{c}"' for c in fk.columns)
            refs = ", ".join(
                f'f"{token}.{parent.name}.{c}"' for c in fk.referred_columns
            )
            # The template supplies the first line's indent; the rest carry
            # their own, so the constraint reads like hand-written code.
            out.append(
                "ForeignKeyConstraint(\n"
                f"            [{cols}],\n"
                f"            [{refs}],\n"
                "        )"
            )
        if out:
            mod.sa_imports.add("ForeignKeyConstraint")
        return out

    # -- relationship rendering ------------------------------------------

    def _relationship_names(
        self, tbl: TableSpec, fk: ForeignKeySpec, parent: TableSpec
    ) -> tuple[str, str]:
        """The pair of attribute names for one reference, from either end.

        Both ends have to agree or ``back_populates`` points at nothing, so
        this reads only the reference and the two tables. Deriving each side
        from what it happened to have seen already made the two disagree.
        """
        forward = fk.relationship_name or snake(parent.name)
        back = fk.backref_name or plural(snake(tbl.name))

        siblings = [
            f
            for f in tbl.foreign_keys
            if f.enabled and f.referred_table == fk.referred_table
        ]
        if len(siblings) > 1:
            # Two references to one parent: the column says which is which.
            stem = snake(re.sub(r"(?i)(id|key|code)$", "", fk.columns[0]))
            if stem and not fk.relationship_name:
                # BillingCustomerId already says "customer"; saying it twice
                # reads worse than the ambiguity did.
                forward = (
                    stem if stem.endswith(forward) else f"{stem}_{forward}"
                )
            if stem and not fk.backref_name:
                back = f"{stem}_{back}"
        return safe_attr(forward), safe_attr(back)

    def _foreign_keys_arg(
        self, tbl: TableSpec, fk: ForeignKeySpec
    ) -> str | None:
        """Which columns carry this reference, when more than one could.

        With several references between the same two tables SQLAlchemy has
        no way to choose a join, and says so at ``configure_mappers()``.
        """
        siblings = [
            f
            for f in tbl.foreign_keys
            if f.enabled and f.referred_table == fk.referred_table
        ]
        if len(siblings) < 2:
            return None
        child = self.class_name(tbl.key)
        cols = ", ".join(f"{child}.{self.attr_name(c)}" for c in fk.columns)
        return f'"[{cols}]"'

    def _relationship_conflict(
        self, tbl: TableSpec, fk: ForeignKeySpec, parent: TableSpec
    ) -> str | None:
        """Why this reference cannot become a pair of attributes, if it cannot.

        A column and a relationship of the same name are two assignments under
        one name in the class body, and the relationship wins: the column
        disappears from the mapping and from the replica DDL. The column is
        the data, so the relationship is what gives way.
        """
        forward, back = self._relationship_names(tbl, fk, parent)
        for owner, attr, side in (
            (tbl, forward, "relationship"),
            (parent, back, "backref"),
        ):
            taken = {self.attr_name(c.name) for c in owner.columns}
            if attr in taken:
                return (
                    f"{tbl.key}: {side} {attr!r} for the reference to "
                    f"{parent.key} collides with a column of that name on "
                    f"{owner.key}; the column is kept and the relationship "
                    "is not generated. Set relationship_name or backref_name "
                    "in the overlay to give it a different one."
                )
        return None

    def _render_fk_relationships(
        self, tbl: TableSpec, mod: RenderedModule
    ) -> list[RenderedRelationship]:
        rels: list[RenderedRelationship] = []
        for fk in tbl.foreign_keys:
            if not fk.enabled:
                continue
            parent = self.spec.table(fk.referred_table)
            if parent is None or not parent.enabled:
                self.report.warnings.append(
                    f"{tbl.key}: FK targets unknown table "
                    f"{fk.referred_table}; skipped"
                )
                continue
            conflict = self._relationship_conflict(tbl, fk, parent)
            if conflict is not None:
                self.report.warnings.append(conflict)
                continue

            target = self.class_name(parent.key)
            self._note_typing(mod, target)
            attr, back = self._relationship_names(tbl, fk, parent)

            # On a self-join both ends sit on one table, so nothing in the
            # foreign key says which end is the parent. remote_side does.
            remote_side = None
            if parent.key == tbl.key:
                cols = ", ".join(
                    f"{target}.{self.attr_name(c)}"
                    for c in fk.referred_columns
                )
                # One string, evaluated in the declarative namespace, the
                # same way primaryjoin is.
                remote_side = f'"[{cols}]"'

            note = None
            if fk.origin == "inferred":
                note = (
                    f"inferred relationship (confidence {fk.confidence}"
                    + (
                        f", containment {fk.containment}"
                        if fk.containment
                        else ""
                    )
                    + ")"
                )
            rels.append(
                RenderedRelationship(
                    attr=attr,
                    target_class=target,
                    kind="many_to_one",
                    back_populates=back,
                    foreign_keys=self._foreign_keys_arg(tbl, fk),
                    remote_side=remote_side,
                    uselist=False,
                    note=note,
                )
            )
        return rels

    def _render_backrefs(
        self, tbl: TableSpec, mod: RenderedModule
    ) -> list[RenderedRelationship]:
        """Collection sides pointing back at this table."""
        rels: list[RenderedRelationship] = []
        for other in self.spec.primary_tables:
            for fk in other.foreign_keys:
                if not fk.enabled:
                    continue
                parent = self.spec.table(fk.referred_table)
                if parent is None or parent.key != tbl.key:
                    continue
                if self._relationship_conflict(other, fk, parent) is not None:
                    continue  # reported from the other side
                target = self.class_name(other.key)
                self._note_typing(mod, target)
                forward, attr = self._relationship_names(other, fk, parent)
                rels.append(
                    RenderedRelationship(
                        attr=attr,
                        target_class=target,
                        kind="one_to_many",
                        back_populates=forward,
                        foreign_keys=self._foreign_keys_arg(other, fk),
                        uselist=True,
                    )
                )
        return rels

    def _note_typing(self, mod: RenderedModule, target_class: str) -> None:
        owner = self._module_of_class.get(target_class)
        if owner and owner != mod.module_name:
            mod.typing_targets.add(f"{owner}:{target_class}")

    def _render_history_link(
        self, primary: TableSpec, hist: TableSpec, mod: RenderedModule
    ) -> RenderedRelationship | None:
        if not primary.primary_key:
            return None
        pcls = self.class_name(primary.key)
        hcls = self.class_name(hist.key)
        missing = [c for c in primary.primary_key if hist.column(c) is None]
        if missing:
            self.report.warnings.append(
                f"{hist.key}: business key column(s) {missing} absent "
                "from history table; "
                "no history relationship generated"
            )
            return None

        conds = ", ".join(
            f"{pcls}.{self.attr_name(c)}"
            f" == foreign({hcls}.{self.attr_name(c)})"
            for c in primary.primary_key
        )
        primaryjoin = (
            f"and_({conds})" if len(primary.primary_key) > 1 else conds
        )
        return RenderedRelationship(
            attr="history",
            target_class=hcls,
            kind="history",
            primaryjoin=f'"{primaryjoin}"',
            order_by=f'"{hcls}.{self.attr_name(self.spec.history.start_column)}"',
            viewonly=True,
            uselist=True,
            note=(
                "no FK constraint exists; joined on the business key, "
                "read-only"
            ),
        )

    def _render_history_parent_relationships(
        self, primary: TableSpec, hist: TableSpec, mod: RenderedModule
    ) -> list[RenderedRelationship]:
        """Traversals from a version row, joined on the key columns alone.

        No interval logic goes into the join. A session pinned to an instant
        narrows the target to the version valid then, which keeps one
        relationship shape for every parent and leaves the interval semantics
        in one place.

        The history table carries the primary table's foreign key columns but
        none of its constraints, so the join needs ``foreign()`` to say which
        side is dependent.
        """
        rels: list[RenderedRelationship] = []
        seen: set[str] = set()
        hcls = self.class_name(hist.key)

        for fk in primary.foreign_keys:
            if not fk.enabled:
                continue
            parent = self.spec.table(fk.referred_table)
            if parent is None or not parent.enabled:
                continue
            if any(hist.column(c) is None for c in fk.columns):
                continue

            # The parent's own history where it has one; a table that does
            # not version has a single state and its current row is not a
            # guess about the past.
            target_tbl = parent
            versioned = False
            if parent.history_table:
                phist = self.spec.table(parent.history_table)
                if phist is not None and phist.enabled:
                    target_tbl, versioned = phist, True
            if any(target_tbl.column(c) is None for c in fk.referred_columns):
                continue

            target = self.class_name(target_tbl.key)
            self._note_typing(mod, target)

            conds = ", ".join(
                f"{hcls}.{self.attr_name(a)}"
                f" == foreign({target}.{self.attr_name(b)})"
                for a, b in zip(fk.columns, fk.referred_columns, strict=True)
            )
            primaryjoin = f"and_({conds})" if len(fk.columns) > 1 else conds

            attr = fk.relationship_name or snake(parent.name)
            if attr in seen:
                stem = (
                    snake(re.sub(r"(?i)(id|key|code)$", "", fk.columns[0]))
                    or attr
                )
                attr = f"{stem}_{attr}"
            seen.add(attr)

            note = (
                "the version valid at the session's instant; unpinned this "
                "matches every version"
                if versioned
                else f"{parent.name} does not version; this is its only state"
            )
            rels.append(
                RenderedRelationship(
                    attr=safe_attr(attr),
                    target_class=target,
                    kind="history_parent",
                    primaryjoin=f'"{primaryjoin}"',
                    viewonly=True,
                    uselist=False,
                    note=note,
                )
            )
        return rels

    # -- module assembly -------------------------------------------------

    def build_modules(self) -> list[RenderedModule]:
        modules: list[RenderedModule] = []
        for primary in self.spec.primary_tables:
            mod = RenderedModule(
                module_name=self._module_names[primary.key], classes=[]
            )
            classes: list[RenderedClass] = []

            pcols = self._render_columns(primary, mod)
            prels = self._render_fk_relationships(primary, mod)
            prels += self._render_backrefs(primary, mod)

            pclass = RenderedClass(
                table_constraints=self._render_table_constraints(primary, mod),
                class_name=self.class_name(primary.key),
                table_name=primary.name,
                schema_token_const=schema_const(primary.schema),
                columns=pcols,
                relationships=prels,
                doc=primary.comment,
            )
            if not primary.primary_key:
                self.report.tables_without_pk.append(primary.key)
                pclass.mapper_primary_key = _fallback_pk(pcols)
                pclass.warnings.append(
                    "no primary key known; a mapper-level key was guessed. "
                    "Set primary_key in the overlay - identity map "
                    "behaviour is "
                    "undefined until you do."
                )
            classes.append(pclass)

            if primary.history_table:
                hist = self.spec.table(primary.history_table)
                if hist is not None and hist.enabled:
                    hcols = self._render_columns(hist, mod)
                    hclass = RenderedClass(
                        class_name=self.class_name(hist.key),
                        table_name=hist.name,
                        schema_token_const=schema_const(hist.schema),
                        columns=hcols,
                        is_history=True,
                        history_of_class=pclass.class_name,
                        scd2=self._scd2_dict(primary),
                        doc=hist.comment,
                    )
                    if not hist.primary_key:
                        hclass.mapper_primary_key = _fallback_pk(hcols)
                        hclass.warnings.append(
                            "history table has no key; guessed at mapper level"
                        )
                    hclass.relationships = (
                        self._render_history_parent_relationships(
                            primary, hist, mod
                        )
                    )
                    classes.append(hclass)
                    link = self._render_history_link(primary, hist, mod)
                    if link:
                        pclass.relationships.append(link)
                    mod.stdlib_imports.add("datetime")

            mod.classes = classes
            modules.append(mod)
            self.report.modules.append(mod.module_name)
            self.report.classes.extend(c.class_name for c in classes)

        # A history table is only ever reached through its primary, so one
        # whose primary is gone or switched off renders nowhere. Saying so
        # is the difference between a decision and a disappearance.
        for hist in self.spec.history_tables:
            if not hist.history_of:
                continue
            owner = self.spec.table(hist.history_of)
            if owner is None or not owner.enabled:
                self.report.unpaired_history.append(hist.key)

        return modules

    def _scd2_dict(self, primary: TableSpec) -> dict:
        h = self.spec.history
        return {
            "start_attr": self.attr_name(h.start_column),
            "end_attr": self.attr_name(h.end_column),
            "end_open": h.end_open,
            "end_sentinel": h.end_sentinel,
            "interval": h.interval,
            "current_in_history": h.current_row_in_history,
            "naive_utc": h.naive_utc,
            "business_key": [self.attr_name(c) for c in primary.primary_key],
        }


def schema_const(schema: str) -> str:
    return "SCHEMA_" + re.sub(r"[^A-Za-z0-9]+", "_", schema).upper()


def _group_typing(targets: set[str]) -> list[tuple[str, list[str]]]:
    """One import line per module, the way an import sorter would write it."""
    by_module: dict[str, list[str]] = {}
    for target in targets:
        module, _, cls = target.partition(":")
        by_module.setdefault(module, []).append(cls)
    return [(m, sorted(by_module[m])) for m in sorted(by_module)]


def _fallback_pk(cols: list[RenderedColumn]) -> list[str]:
    non_null = [c.attr for c in cols if not c.nullable]
    return non_null[:8] or [c.attr for c in cols[:8]]


# ---------------------------------------------------------------------------
# writing
# ---------------------------------------------------------------------------


def _env() -> Environment:
    env = Environment(
        loader=PackageLoader("stele", "templates"),
        undefined=StrictUndefined,
        trim_blocks=True,
        lstrip_blocks=True,
        keep_trailing_newline=True,
    )
    env.filters["repr"] = repr
    return env


def generate(
    spec: ModelSpec, outdir: Path, *, preserve_names: bool = True
) -> GenerationReport:
    gen = Generator(spec, preserve_names=preserve_names)
    modules = gen.build_modules()
    env = _env()

    outdir.mkdir(parents=True, exist_ok=True)

    schemas = sorted({t.schema for t in spec.tables if t.enabled})
    (outdir / "_schemas.py").write_text(
        env.get_template("schemas.py.jinja").render(
            schemas=[(schema_const(s), s) for s in schemas]
        ),
        encoding="utf-8",
    )

    module_tpl = env.get_template("module.py.jinja")
    for mod in modules:
        (outdir / f"{mod.module_name}.py").write_text(
            module_tpl.render(
                mod=mod,
                sa_imports=sorted(mod.sa_imports),
                needs_relationship=any(c.relationships for c in mod.classes),
                needs_history=any(c.is_history for c in mod.classes),
                mssql_imports=sorted(mod.mssql_imports),
                stdlib_imports=sorted(mod.stdlib_imports),
                typing_imports=_group_typing(mod.typing_targets),
                schema_consts=sorted(
                    {c.schema_token_const for c in mod.classes}
                ),
            ),
            encoding="utf-8",
        )

    (outdir / "__init__.py").write_text(
        env.get_template("init.py.jinja").render(
            # Alphabetical, so the emitted import block is what an import
            # sorter would have written. Every module is imported either
            # way; the order carries no meaning.
            modules=sorted(modules, key=lambda m: m.module_name),
            spec=spec,
            schemas=[(schema_const(s), s) for s in schemas],
        ),
        encoding="utf-8",
    )
    return gen.report
