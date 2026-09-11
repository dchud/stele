"""The starter MkDocs site, and which parts of it stele keeps owning."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
import yaml

from stele.dictionary import build, to_json
from stele.scaffold import (
    NAV_PATH,
    SiteInputs,
    nav_document,
    read_document,
    scaffold,
    schemas_of,
)
from stele.spec import ColumnSpec, ModelSpec, TableSpec


def _spec(*schemas: str) -> ModelSpec:
    tables = [
        TableSpec(
            name=f"T{i}",
            schema=schema,
            primary_key=[f"T{i}Id"],
            primary_key_origin="inferred",
            columns=[
                ColumnSpec(name=f"T{i}Id", source_type="BIGINT"),
            ],
        )
        for i, schema in enumerate(schemas)
    ]
    return ModelSpec(catalog="acme", schemas=list(schemas), tables=tables)


def _inputs(spec: ModelSpec, tmp_path: Path) -> SiteInputs:
    """Round-trip through the document, which is the command's only input."""
    path = tmp_path / "dictionary.json"
    path.write_text(to_json(build(spec)))
    return read_document(path)


def _nav(spec: ModelSpec, tmp_path: Path) -> dict[str, Any]:
    return yaml.safe_load(nav_document(_inputs(spec, tmp_path)))  # type: ignore[no-any-return]


def test_schemas_come_back_in_the_order_the_document_lists_them() -> None:
    names = [t.name for t in build(_spec("dbo", "ref", "dbo", "stg")).tables]
    assert schemas_of(names) == ["dbo", "ref", "stg"]


def test_the_nav_groups_pages_by_schema(tmp_path: Path) -> None:
    nav = _nav(_spec("dbo", "ref"), tmp_path)["nav"]

    assert nav[0] == {"Overview": "README.md"}
    assert {"dbo": ["dbo.*.md"]} in nav
    assert {"ref": ["ref.*.md"]} in nav


def test_groups_are_globs_so_a_new_table_needs_no_edit(tmp_path: Path) -> None:
    """The catalog gains tables; the nav file should not have to."""
    nav = _nav(_spec("dbo"), tmp_path)["nav"]
    (group,) = [entry for entry in nav if "dbo" in entry]
    assert group["dbo"] == ["dbo.*.md"]


def test_viewpoint_pages_are_named_rather_than_left_to_fall_through(
    tmp_path: Path,
) -> None:
    """awesome-nav appends what no glob matched, which reads worse."""
    nav = _nav(_spec("dbo", "ref"), tmp_path)["nav"]
    assert {"Cross-cutting views": ["viewpoint-*.md"]} in nav


def test_a_document_with_no_viewpoints_gets_no_such_group(
    tmp_path: Path,
) -> None:
    spec = ModelSpec(
        catalog="acme",
        tables=[
            TableSpec(
                name="Owner",
                schema="ref",
                primary_key=["OwnerId"],
                primary_key_origin="catalog",
                primary_key_verified=True,
                columns=[ColumnSpec(name="OwnerId", source_type="BIGINT")],
            )
        ],
    )
    nav = _nav(spec, tmp_path)["nav"]
    assert not [e for e in nav if "Cross-cutting views" in e]


def test_the_nav_file_says_it_has_to_be_copied_after_rendering(
    tmp_path: Path,
) -> None:
    """`tbls doc --rm-dist` empties the directory this belongs in."""
    text = nav_document(_inputs(_spec("dbo"), tmp_path))
    assert "clears that directory" in text


# --- what a second run does ------------------------------------------------


def test_the_first_run_writes_a_site(tmp_path: Path) -> None:
    report = scaffold(_inputs(_spec("dbo"), tmp_path), tmp_path)

    assert str(NAV_PATH) in report.written
    assert "mkdocs.yml" in report.written
    assert report.kept == []
    assert (tmp_path / "mkdocs.yml").exists()
    assert (tmp_path / "requirements.txt").exists()
    assert (tmp_path / "docs" / "index.md").exists()


def test_a_second_run_rewrites_the_nav_and_keeps_the_rest(
    tmp_path: Path,
) -> None:
    """The nav is derived; the others describe a site and are yours."""
    scaffold(_inputs(_spec("dbo"), tmp_path), tmp_path)
    (tmp_path / "mkdocs.yml").write_text("site_name: Mine\n")

    report = scaffold(_inputs(_spec("dbo", "ref"), tmp_path), tmp_path)

    assert report.written == [str(NAV_PATH)]
    assert sorted(report.kept) == sorted(
        ["mkdocs.yml", "requirements.txt", str(Path("docs") / "index.md")]
    )
    assert (tmp_path / "mkdocs.yml").read_text() == "site_name: Mine\n"
    # The new schema reached the file stele owns.
    nav = yaml.safe_load((tmp_path / NAV_PATH).read_text())["nav"]
    assert {"ref": ["ref.*.md"]} in nav


def test_the_config_prunes_the_nav(tmp_path: Path) -> None:
    """Without it MkDocs writes the whole nav into every page."""
    scaffold(_inputs(_spec("dbo"), tmp_path), tmp_path)
    config = yaml.safe_load((tmp_path / "mkdocs.yml").read_text())

    assert "navigation.prune" in config["theme"]["features"]
    assert "awesome-nav" in config["plugins"]
    assert {"Data dictionary": "database"} in config["nav"]


# --- the document is the only input ----------------------------------------


def test_a_site_needs_only_the_document(tmp_path: Path) -> None:
    """A documentation repository holds no spec, overlay or credentials."""
    document = tmp_path / "dictionary.json"
    document.write_text(to_json(build(_spec("dbo", "ref"))))

    site = tmp_path / "elsewhere"
    report = scaffold(read_document(document), site)

    assert str(NAV_PATH) in report.written
    nav = yaml.safe_load((site / NAV_PATH).read_text())["nav"]
    assert {"dbo": ["dbo.*.md"]} in nav
    assert {"ref": ["ref.*.md"]} in nav


def test_the_site_is_named_for_the_catalog(tmp_path: Path) -> None:
    document = tmp_path / "dictionary.json"
    document.write_text(to_json(build(_spec("dbo"))))

    assert read_document(document).site_name == "acme data dictionary"


def test_a_file_that_is_not_a_document_says_so(tmp_path: Path) -> None:
    path = tmp_path / "model.yaml"
    path.write_text('{"schemas": ["dbo"]}')

    with pytest.raises(ValueError, match="not a tbls document"):
        read_document(path)
