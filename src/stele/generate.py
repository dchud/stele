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

import logging
import re
from dataclasses import dataclass, field
from pathlib import Path

from jinja2 import Environment, PackageLoader, StrictUndefined

from . import types as typemap
from .spec import ForeignKeySpec, ModelSpec, TableSpec

log = logging.getLogger("stele.generate")

_PY_KEYWORDS = {
    "class", "def", "import", "from", "return", "pass", "None", "True", "False",
    "and", "or", "not", "in", "is", "if", "else", "elif", "for", "while", "type",
    "id", "metadata", "registry",
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
    if name.endswith("y") and not name.endswith(("ay", "ey", "iy", "oy", "uy")):
        return name[:-1] + "ies"
    return name + "s"


def safe_attr(name: str) -> str:
    if name in _PY_KEYWORDS or not re.match(r"^[A-Za-z_]", name):
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

    # -- column rendering ------------------------------------------------

    def _render_columns(self, tbl: TableSpec, mod: RenderedModule) -> list[RenderedColumn]:
        pk = {c.lower() for c in tbl.primary_key}
        fk_by_col: dict[str, tuple[ForeignKeySpec, int]] = {}
        for fk in tbl.foreign_keys:
            if not fk.enabled:
                continue
            for i, c in enumerate(fk.columns):
                fk_by_col[c.lower()] = (fk, i)

        out: list[RenderedColumn] = []
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
                if parent is not None:
                    token = f"{{{schema_const(parent.schema)}}}"
                    fk_target = (
                        f'f"{token}.{parent.name}.{fk.referred_columns[idx]}"'
                    )

            if rt.lossy:
                self.report.lossy_columns.append(f"{tbl.key}.{col.name}: {rt.note}")

            out.append(
                RenderedColumn(
                    attr=safe_attr(col.name if self.preserve_names else snake(col.name)),
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

    # -- relationship rendering ------------------------------------------

    def _render_fk_relationships(
        self, tbl: TableSpec, mod: RenderedModule
    ) -> list[RenderedRelationship]:
        rels: list[RenderedRelationship] = []
        seen: set[str] = set()
        for fk in tbl.foreign_keys:
            if not fk.enabled:
                continue
            parent = self.spec.table(fk.referred_table)
            if parent is None or not parent.enabled:
                self.report.warnings.append(
                    f"{tbl.key}: FK targets unknown table {fk.referred_table}; skipped"
                )
                continue
            target = self.class_name(parent.key)
            self._note_typing(mod, target)

            attr = fk.relationship_name or snake(parent.name)
            # Disambiguate multiple FKs to the same parent by the column stem.
            if attr in seen:
                stem = snake(re.sub(r"(?i)(id|key|code)$", "", fk.columns[0])) or attr
                attr = f"{stem}_{attr}"
            seen.add(attr)

            back = fk.backref_name or plural(snake(tbl.name))
            note = None
            if fk.origin == "inferred":
                note = (
                    f"inferred relationship (confidence {fk.confidence}"
                    + (f", containment {fk.containment}" if fk.containment else "")
                    + ")"
                )
            rels.append(
                RenderedRelationship(
                    attr=safe_attr(attr),
                    target_class=target,
                    kind="many_to_one",
                    back_populates=safe_attr(back),
                    uselist=False,
                    note=note,
                )
            )
        return rels

    def _render_backrefs(self, tbl: TableSpec, mod: RenderedModule) -> list[RenderedRelationship]:
        """Collection sides pointing back at this table."""
        rels: list[RenderedRelationship] = []
        seen: set[str] = set()
        for other in self.spec.primary_tables:
            for fk in other.foreign_keys:
                if not fk.enabled:
                    continue
                parent = self.spec.table(fk.referred_table)
                if parent is None or parent.key != tbl.key:
                    continue
                target = self.class_name(other.key)
                self._note_typing(mod, target)
                attr = fk.backref_name or plural(snake(other.name))
                if attr in seen:
                    attr = f"{snake(fk.columns[0])}_{attr}"
                seen.add(attr)
                forward = fk.relationship_name or snake(tbl.name)
                rels.append(
                    RenderedRelationship(
                        attr=safe_attr(attr),
                        target_class=target,
                        kind="one_to_many",
                        back_populates=safe_attr(forward),
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
                f"{hist.key}: business key column(s) {missing} absent from history table; "
                "no history relationship generated"
            )
            return None

        conds = " , ".join(
            f"{pcls}.{safe_attr(c)} == foreign({hcls}.{safe_attr(c)})"
            for c in primary.primary_key
        )
        primaryjoin = f"and_({conds})" if len(primary.primary_key) > 1 else conds
        return RenderedRelationship(
            attr="history",
            target_class=hcls,
            kind="history",
            primaryjoin=f'"{primaryjoin}"',
            order_by=f'"{hcls}.{safe_attr(self.spec.history.start_column)}"',
            viewonly=True,
            uselist=True,
            note="no FK constraint exists; joined on the business key, read-only",
        )

    # -- module assembly -------------------------------------------------

    def build_modules(self) -> list[RenderedModule]:
        modules: list[RenderedModule] = []
        for primary in self.spec.primary_tables:
            mod = RenderedModule(module_name=self._module_names[primary.key], classes=[])
            classes: list[RenderedClass] = []

            pcols = self._render_columns(primary, mod)
            prels = self._render_fk_relationships(primary, mod)
            prels += self._render_backrefs(primary, mod)

            pclass = RenderedClass(
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
                    "Set primary_key in the overlay - identity map behaviour is "
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
                    classes.append(hclass)
                    link = self._render_history_link(primary, hist, mod)
                    if link:
                        pclass.relationships.append(link)
                    mod.stdlib_imports.add("datetime")

            mod.classes = classes
            modules.append(mod)
            self.report.modules.append(mod.module_name)
            self.report.classes.extend(c.class_name for c in classes)

        for hist in self.spec.history_tables:
            if hist.history_of and self.spec.table(hist.history_of) is None:
                self.report.unpaired_history.append(hist.key)

        return modules

    def _scd2_dict(self, primary: TableSpec) -> dict:
        h = self.spec.history
        return {
            "start_attr": safe_attr(h.start_column),
            "end_attr": safe_attr(h.end_column),
            "end_open": h.end_open,
            "end_sentinel": h.end_sentinel,
            "interval": h.interval,
            "current_in_history": h.current_row_in_history,
            "naive_utc": h.naive_utc,
            "business_key": [safe_attr(c) for c in primary.primary_key],
        }


def schema_const(schema: str) -> str:
    return "SCHEMA_" + re.sub(r"[^A-Za-z0-9]+", "_", schema).upper()


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


def generate(spec: ModelSpec, outdir: Path, *, preserve_names: bool = True) -> GenerationReport:
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
                sa_imports=sorted(mod.sa_imports | {"ForeignKey"}),
                mssql_imports=sorted(mod.mssql_imports),
                stdlib_imports=sorted(mod.stdlib_imports),
                typing_imports=sorted(
                    (t.split(':', 1)[0], t.split(':', 1)[1]) for t in mod.typing_targets
                ),
                schema_consts=sorted({c.schema_token_const for c in mod.classes}),
            ),
            encoding="utf-8",
        )

    (outdir / "__init__.py").write_text(
        env.get_template("init.py.jinja").render(
            modules=modules,
            spec=spec,
            schemas=[(schema_const(s), s) for s in schemas],
        ),
        encoding="utf-8",
    )
    return gen.report
