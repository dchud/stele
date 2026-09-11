"""A starter MkDocs site for the pages tbls renders.

tbls writes one flat directory: a page per table named `schema.table.md`,
a page per viewpoint, and an index. MkDocs with no `nav` of its own builds
a navbar from every file it finds, so a few hundred tables become a few
hundred flat entries and the site is a sidebar with content hidden behind
it.

What fixes that is short but has to be assembled from three files, and
each has a way of failing quietly. Glob patterns work in `awesome-nav`'s
`.nav.yml` and are ignored without complaint in `mkdocs.yml`. A glob in a
parent directory does not reach into a child. And `tbls doc --rm-dist`
clears its output directory, so the nav file has to be written after the
pages it describes, not before them.

One rule decides what is written and what is left alone: **the nav file
beside the rendered pages follows the schemas in the document, so stele
owns it and rewrites it. Everything else describes a site rather than a
model, so it is written once and never overwritten.**

Everything here reads `dictionary.json` and nothing else, and writes into
a directory that need not be this project. Pointing it at a documentation
repository is the intended use: stele and tbls run where the model is, and
what lands over there is a site whose only dependency is MkDocs.
"""

from __future__ import annotations

import json
import re
from collections.abc import Iterable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

#: The directory tbls renders into, relative to `docs/`.
DOCS_SUBDIR = "database"

#: Where `stele generate --docs` writes its reference pages, relative to
#: `docs/`. The two sit side by side: one describes the data, the other the
#: package that reads it.
API_SUBDIR = "api"

#: The site's top-level order and titles. Written once and never
#: overwritten: it describes the site rather than the model, and a site
#: that already has one has an order somebody chose. `awesome-nav` appends
#: anything it does not name, so pages arriving later still appear - just
#: at the end, until someone says where they belong.
ROOT_NAV_PATH = Path("docs") / ".nav.yml"

#: The nav file, written into the rendered directory itself. That is only
#: safe after `tbls doc`, which clears that directory - so this runs last,
#: and what would otherwise be a copy step does not exist.
NAV_PATH = Path("docs") / DOCS_SUBDIR / ".nav.yml"

#: Versions this was built and checked against.
MKDOCS_MATERIAL = "mkdocs-material>=9.7.7"
AWESOME_NAV = "mkdocs-awesome-nav>=3.3.0"


@dataclass
class ScaffoldReport:
    """What the run wrote, and what it found already there."""

    written: list[str] = field(default_factory=list)
    kept: list[str] = field(default_factory=list)
    #: Whether tbls had already rendered the pages the nav describes. A
    #: run before `tbls doc` writes a nav file that `--rm-dist` then
    #: deletes, which is worth saying rather than leaving to be noticed.
    rendered: bool = False


@dataclass(frozen=True)
class SiteInputs:
    """Everything the site needs, all of it read from the document.

    Read from the document rather than the spec on purpose. A repository
    that holds only the published `dictionary.json` - which is the point
    of publishing it - can still build and refresh its own site, with no
    `model.yaml`, no overlay and no warehouse credentials.
    """

    site_name: str
    schemas: list[str]
    viewpoints: bool


def schemas_of(table_names: Iterable[str]) -> list[str]:
    """Schemas the names belong to, in the order they first appear."""
    out: list[str] = []
    for name in table_names:
        schema = name.rpartition(".")[0]
        if schema and schema not in out:
            out.append(schema)
    return out


def read_document(path: Path) -> SiteInputs:
    """What `stele dictionary` wrote, read back for the site around it."""
    raw: Any = json.loads(path.read_text(encoding="utf-8"))
    tables = raw.get("tables") if isinstance(raw, dict) else None
    if not isinstance(tables, list) or not all(
        isinstance(t, dict) for t in tables
    ):
        raise ValueError(
            f"{path} is not a tbls document; "
            "`stele dictionary --out` writes one"
        )
    name = raw.get("name") or "model"
    return SiteInputs(
        site_name=f"{name} data dictionary",
        schemas=schemas_of(str(t.get("name", "")) for t in tables),
        viewpoints=bool(raw.get("viewpoints")),
    )


def root_nav(*, api: bool) -> str:
    """Top-level order, and the titles a directory name would not give."""
    nav: list[object] = ["index.md", {"Data dictionary": DOCS_SUBDIR}]
    if api:
        nav.append({"API reference": API_SUBDIR})
    body = yaml.safe_dump({"nav": nav}, sort_keys=False)
    return f"# Written by `stele site`, and rewritten on every run.\n{body}"


def nav_document(inputs: SiteInputs) -> str:
    """The `.nav.yml` grouping the rendered pages by schema.

    A group per schema, matched by glob rather than listed, so a table
    added upstream lands in the right place with nothing to edit.
    `awesome-nav` appends files no glob matched, so the viewpoint pages
    are named rather than left to fall through - they read better under a
    heading of their own than after the last schema.
    """
    nav: list[dict[str, object]] = [{"Overview": "README.md"}]
    nav.extend({schema: [f"{schema}.*.md"]} for schema in inputs.schemas)
    if inputs.viewpoints:
        nav.append({"Cross-cutting views": ["viewpoint-*.md"]})

    body = yaml.safe_dump(
        {"title": "Data dictionary", "nav": nav},
        sort_keys=False,
        allow_unicode=True,
    )
    return (
        "# Written by `stele site`, after `tbls doc` and never before:\n"
        "# `--rm-dist` clears this directory, and would take this with it.\n"
        f"{body}"
    )


def _yaml_scalar(value: str) -> str:
    """Quoted where it has to be. A catalog name is a database
    identifier, and `foo: bar` in one would end the key."""
    dumped = yaml.safe_dump(value, default_flow_style=True).strip()
    # safe_dump ends a bare scalar document with the `...` terminator.
    return dumped.removesuffix("...").strip()


def project_name(site_name: str) -> str:
    """A distribution name a documentation project can carry.

    The site is named for the catalog, which is a database identifier and
    need not be one of these.
    """
    slug = re.sub(r"[^a-z0-9]+", "-", site_name.lower()).strip("-")
    return slug or "data-dictionary"


def pyproject_toml(site_name: str) -> str:
    """The site's dependencies, for `uv run mkdocs serve`.

    Not a package - nothing here is imported - so uv is told not to try
    building one.
    """
    return f"""\
[project]
name = "{project_name(site_name)}"
version = "0"
description = {json.dumps(site_name)}
requires-python = ">=3.11"
dependencies = [
    # Grouping the table pages by schema needs awesome-nav, which reads
    # the nav file `stele site` writes beside them.
    "{MKDOCS_MATERIAL}",
    "{AWESOME_NAV}",
]

[tool.uv]
package = false
"""


def mkdocs_config(site_name: str) -> str:
    """A site that can carry a few hundred table pages.

    `navigation.prune` is the one doing the work at that size: without it
    MkDocs writes the whole nav into every page, so the cost of the
    sidebar is paid once per table rather than once.

    There is no `nav` here. `awesome-nav` builds one from the tree and
    says so when it replaces one, and MkDocs warns first about entries it
    cannot resolve on its own. Ordering and titles live in the `.nav.yml`
    beside the pages instead.
    """
    return f"""\
site_name: {_yaml_scalar(site_name)}

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
  # Reads the nav file `stele site` writes, which groups
  # the table pages by schema. Glob patterns work there and nowhere else.
  - awesome-nav
"""


def index_page(site_name: str, *, api: bool) -> str:
    """A home page naming what is actually under it.

    The reference pages are mentioned only where they exist, because this
    file is written once and a link to a missing page fails every build
    until somebody removes it.
    """
    reference = (
        f"""

The [API reference]({API_SUBDIR}/index.md) describes the Python package that
reads them: what each class is called, what its attributes are named, and
which relationships it carries."""
        if api
        else ""
    )
    return f"""\
# {site_name}

The [data dictionary]({DOCS_SUBDIR}/README.md) describes every table in the
model: its columns, what was observed in the data, and where each key and
reference came from.{reference}
"""


def scaffold(inputs: SiteInputs, root: Path) -> ScaffoldReport:
    """Write the starter site under `root`, keeping what is already there.

    Only the dictionary's nav file is rewritten, because its groups follow
    the schemas in the document. Everything else describes a site rather
    than a model - a second run finding them changed means somebody edited
    them on purpose, and a site that already existed had them first.
    """
    report = ScaffoldReport()
    api = (root / "docs" / API_SUBDIR).is_dir()

    nav = root / NAV_PATH
    nav.parent.mkdir(parents=True, exist_ok=True)
    nav.write_text(nav_document(inputs), encoding="utf-8")
    report.written.append(str(NAV_PATH))
    report.rendered = (nav.parent / "README.md").exists()

    once = {
        ROOT_NAV_PATH: root_nav(api=api),
        Path("mkdocs.yml"): mkdocs_config(inputs.site_name),
        Path("pyproject.toml"): pyproject_toml(inputs.site_name),
        Path("docs") / "index.md": index_page(inputs.site_name, api=api),
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
