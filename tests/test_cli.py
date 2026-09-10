"""The command line's own behaviour, ahead of any command it dispatches."""

from __future__ import annotations

import logging
from collections.abc import Iterator

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
