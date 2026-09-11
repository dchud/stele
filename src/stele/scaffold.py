"""A starter MkDocs site for the pages tbls renders.

tbls writes one flat directory: a page per table named `schema.table.md`,
a page per viewpoint, and an index. MkDocs with no `nav` of its own builds
a navbar from every file it finds, so a few hundred tables become a few
hundred flat entries and the site is a sidebar with content hidden behind
it.

What fixes that is short but has to be assembled from three files, and
each has a way of failing quietly. Glob patterns work in `awesome-nav`'s
`.nav.yml` and are ignored without complaint in `mkdocs.yml`. A glob in a
parent directory does not reach into a child. `tbls doc --rm-dist` clears
its output directory, which takes `.nav.yml` with it, so the file has to
live elsewhere and be copied back after each render.

One rule decides what is written and what is left alone: **the nav file is
derived from the model, so stele owns it and rewrites it; `mkdocs.yml` and
the requirements describe a site rather than a model, so they are written
once and never overwritten.**
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

import yaml

from .dictionary import Document

#: Where the nav file is kept. Outside the directory tbls renders into,
#: because `--rm-dist` empties that one, and copied in by the recipe.
NAV_PATH = Path("nav") / "database.nav.yml"

#: The directory the recipe renders into, relative to `docs/`.
DOCS_SUBDIR = "database"

#: Versions this was built and checked against.
REQUIREMENTS = """\
# The dictionary's pages are grouped by `awesome-nav`, which reads the
# nav file `stele dictionary --mkdocs` writes.
mkdocs-material>=9.7.7
mkdocs-awesome-nav>=3.3.0
"""


@dataclass
class ScaffoldReport:
    """What the run wrote, and what it found already there."""

    written: list[str] = field(default_factory=list)
    kept: list[str] = field(default_factory=list)


def schemas_of(doc: Document) -> list[str]:
    """Schemas the document describes, in the order it lists them."""
    out: list[str] = []
    for table in doc.tables:
        schema = table.name.rpartition(".")[0]
        if schema and schema not in out:
            out.append(schema)
    return out


def nav_document(doc: Document) -> str:
    """The `.nav.yml` grouping the rendered pages by schema.

    A group per schema, matched by glob rather than listed, so a table
    added upstream lands in the right place with nothing to edit.
    `awesome-nav` appends files no glob matched, so the viewpoint pages
    are named rather than left to fall through - they read better under a
    heading of their own than after the last schema.
    """
    nav: list[dict[str, object]] = [{"Overview": "README.md"}]
    nav.extend({schema: [f"{schema}.*.md"]} for schema in schemas_of(doc))
    if doc.viewpoints:
        nav.append({"Cross-cutting views": ["viewpoint-*.md"]})

    body = yaml.safe_dump(
        {"title": "Data dictionary", "nav": nav},
        sort_keys=False,
        allow_unicode=True,
    )
    return (
        "# Written by `stele dictionary --mkdocs`, and rewritten on every\n"
        "# run. Copy it into the rendered directory after `tbls doc`, which\n"
        "# clears that directory and would otherwise take this with it.\n"
        f"{body}"
    )


def mkdocs_config(site_name: str) -> str:
    """A site that can carry a few hundred table pages.

    `navigation.prune` is the one doing the work at that size: without it
    MkDocs writes the whole nav into every page, so the cost of the
    sidebar is paid once per table rather than once.
    """
    return f"""\
site_name: {site_name}

theme:
  name: material
  features:
    # Renders only the branch of the nav you are in. At a few hundred
    # tables this is the difference between a usable site and a slow one.
    - navigation.prune
    - navigation.indexes
    - navigation.top
    - toc.follow

plugins:
  - search
  # Reads the nav file `stele dictionary --mkdocs` writes, which groups
  # the table pages by schema. Glob patterns work there and nowhere else.
  - awesome-nav

nav:
  - Home: index.md
  - Data dictionary: {DOCS_SUBDIR}
"""


INDEX_PAGE = """\
# {site_name}

The [data dictionary]({subdir}/README.md) describes every table in the
model: its columns, what was observed in the data, and where each key and
reference came from.
"""


def scaffold(doc: Document, root: Path, *, site_name: str) -> ScaffoldReport:
    """Write the starter site under `root`, keeping what is already there.

    Only the nav file is rewritten. The rest describes a site rather than
    a model, and a second run finding them changed means somebody edited
    them on purpose.
    """
    report = ScaffoldReport()

    nav = root / NAV_PATH
    nav.parent.mkdir(parents=True, exist_ok=True)
    nav.write_text(nav_document(doc), encoding="utf-8")
    report.written.append(str(NAV_PATH))

    once = {
        Path("mkdocs.yml"): mkdocs_config(site_name),
        Path("requirements.txt"): REQUIREMENTS,
        Path("docs") / "index.md": INDEX_PAGE.format(
            site_name=site_name, subdir=DOCS_SUBDIR
        ),
    }
    for relative, content in once.items():
        target = root / relative
        if target.exists():
            report.kept.append(str(relative))
            continue
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")
        report.written.append(str(relative))

    return report
