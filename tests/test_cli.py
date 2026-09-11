"""The command line's own behaviour, ahead of any command it dispatches."""

from __future__ import annotations

import logging
from collections.abc import Iterator
from pathlib import Path

import pytest

from stele.cli import configure_logging


@pytest.fixture(autouse=True)
def _restore_logging() -> Iterator[None]:
    """Put the root logger back; these tests reconfigure it globally."""
    root = logging.getLogger()
    handlers, level = list(root.handlers), root.level
    stele_level = logging.getLogger("stele").level
    yield
    root.handlers[:] = handlers
    root.setLevel(level)
    logging.getLogger("stele").setLevel(stele_level)


def test_stele_speaks_at_info_and_libraries_do_not() -> None:
    """The driver narrates every HTTP 200; asking for stele is not asking
    for that."""
    configure_logging()
    assert logging.getLogger("stele.profile").isEnabledFor(logging.INFO)
    assert not logging.getLogger("databricks.sql").isEnabledFor(logging.INFO)


def test_stele_records_still_reach_a_handler() -> None:
    """Root sits above stele's level, which gates records but not
    propagation."""
    configure_logging()
    records: list[logging.LogRecord] = []

    class Capture(logging.Handler):
        def emit(self, record: logging.LogRecord) -> None:
            records.append(record)

    logging.getLogger().addHandler(Capture())
    logging.getLogger("stele.profile").info("profiled dbo.Order")
    assert [r.getMessage() for r in records] == ["profiled dbo.Order"]


def test_verbose_opens_the_libraries_back_up() -> None:
    """When the connection is the problem, the driver's records are the
    ones worth reading."""
    configure_logging(verbose=True)
    assert logging.getLogger("stele").isEnabledFor(logging.DEBUG)
    assert logging.getLogger("databricks.sql").isEnabledFor(logging.DEBUG)


# --- what `stele site` says about a site it did not create -----------------


def _document(tmp_path: Path) -> Path:
    from stele.dictionary import build, to_json
    from stele.spec import ColumnSpec, ModelSpec, TableSpec

    spec = ModelSpec(
        catalog="acme",
        tables=[
            TableSpec(
                name="Order",
                schema="dbo",
                primary_key=["OrderId"],
                primary_key_origin="inferred",
                columns=[ColumnSpec(name="OrderId", source_type="BIGINT")],
            )
        ],
    )
    path = tmp_path / "dictionary.json"
    path.write_text(to_json(build(spec)))
    return path


def _site(tmp_path: Path, out: Path) -> None:
    from stele.cli import main

    main(["site", "--document", str(_document(tmp_path)), "--out", str(out)])


def test_a_second_run_over_its_own_site_has_nothing_to_say(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Advice nobody needs trains people to skip the output."""
    out = tmp_path / "site"
    _site(tmp_path, out)
    capsys.readouterr()

    _site(tmp_path, out)

    assert "already here" not in capsys.readouterr().out


def test_a_site_that_predates_stele_is_told_what_it_lacks(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    out = tmp_path / "theirs"
    (out / "docs").mkdir(parents=True)
    (out / "mkdocs.yml").write_text("site_name: Team docs\n")
    (out / "docs" / ".nav.yml").write_text("nav:\n  - index.md\n")

    _site(tmp_path, out)

    printed = capsys.readouterr().out
    assert "awesome-nav" in printed
    assert "Data dictionary: database" in printed
    # No reference pages there, so nothing says to nav them.
    assert "API reference" not in printed
