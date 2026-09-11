"""Reference pages for the package `generate` writes.

Two documents describe one model and they answer different questions. The
data dictionary says what `dbo.Order` holds and what the data showed about
it. These pages say what `Order` is called in Python, what its attributes
are named, and which relationships it carries. A person reading SQL wants
the first; a person writing a query in this package wants the second.

They are rendered from the same `build_modules()` call that writes the
package, so a class cannot be documented as something other than what was
emitted. That is the reason this hangs off `generate` rather than off
`site`: only `generate` resolves a table name into a class name, and it
does so through overlay overrides and `--snake-case`, neither of which
reaches the tbls document.

Rendering here rather than in the documentation project keeps that project
free of stele: what lands there is Markdown.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

from .generate import Generator, RenderedModule, _env
from .spec import ModelSpec

#: Where the dictionary's pages sit, relative to these. Used to link a
#: class to the table it maps.
DICTIONARY_RELATIVE = Path("..") / "database"

#: Written into every page. A page without it was not written here, and is
#: left alone however stale it looks - pointing `--docs` at the wrong
#: directory should cost nothing.
MARKER = "Written by `stele generate --docs`"


@dataclass
class ApiDocsReport:
    """Pages written, and pages a previous run left that no longer apply."""

    pages: list[str] = field(default_factory=list)
    removed: list[str] = field(default_factory=list)


def _links(
    modules: list[RenderedModule],
    table_keys: dict[str, str],
    dictionary: Path | None,
) -> tuple[object, object]:
    """How a class and a table are addressed from a module page.

    tbls names a page for the schema-qualified table, `dbo.Order.md`,
    while a rendered class carries the bare name. `table_keys` carries the
    qualified one, so the link lands on a page that exists.
    """
    module_of = {
        cls.class_name: mod.module_name
        for mod in modules
        for cls in mod.classes
    }

    def class_link(name: str) -> str:
        module = module_of.get(name)
        anchor = name.lower()
        return f"#{anchor}" if module is None else f"{module}.md#{anchor}"

    def dictionary_link(class_name: str) -> str:
        """Empty where there is no dictionary page to point at."""
        key = table_keys.get(class_name)
        if dictionary is None or key is None:
            return ""
        return f"{dictionary.as_posix()}/{key}.md"

    return class_link, dictionary_link


def _prune_stale(out: Path, keeping: set[str]) -> list[str]:
    """Drop pages a previous run wrote and this one does not.

    A class that goes away upstream would otherwise leave a page behind,
    still linked from nothing and still reading as though it described
    something. Only pages carrying the marker are removed.
    """
    removed: list[str] = []
    for path in sorted(out.glob("*.md")):
        if path.name in keeping:
            continue
        if MARKER not in path.read_text(encoding="utf-8")[:200]:
            continue
        path.unlink()
        removed.append(path.name)
    return removed


def write(
    spec: ModelSpec,
    out: Path,
    *,
    package: str,
    preserve_names: bool = True,
    dictionary: Path | None = DICTIONARY_RELATIVE,
) -> ApiDocsReport:
    """Write a page per module under `out`, and an index over them.

    `dictionary` is where the rendered tbls pages sit relative to these, so
    a class links to the table it maps. Pass None where there are none to
    link to, and the links are left out rather than written broken.
    """
    gen = Generator(spec, preserve_names=preserve_names)
    modules = [mod for mod in gen.build_modules() if mod.classes]
    env = _env()
    # History tables are described on their primary rather than given a
    # page, so a class whose table has none gets no link.
    table_keys = {
        gen.class_name(tbl.key): tbl.key
        for tbl in spec.primary_tables
        if tbl.enabled
    }
    class_link, dictionary_link = _links(modules, table_keys, dictionary)

    out.mkdir(parents=True, exist_ok=True)
    report = ApiDocsReport()
    report.removed = _prune_stale(
        out, {"index.md"} | {f"{mod.module_name}.md" for mod in modules}
    )

    template = env.get_template("api_module.md.jinja")
    for mod in modules:
        (out / f"{mod.module_name}.md").write_text(
            template.render(
                mod=mod,
                package=package,
                class_link=class_link,
                dictionary_link=dictionary_link,
            ),
            encoding="utf-8",
        )
        report.pages.append(f"{mod.module_name}.md")

    first = next(
        (cls.class_name for mod in modules for cls in mod.classes), "Table"
    )
    (out / "index.md").write_text(
        env.get_template("api_index.md.jinja").render(
            package=package,
            modules=modules,
            first_class=first,
            dictionary_index=(
                f"{dictionary.as_posix()}/README.md"
                if dictionary is not None
                else "../database/README.md"
            ),
        ),
        encoding="utf-8",
    )
    report.pages.append("index.md")
    return report
