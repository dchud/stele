"""Engine construction.

Two things worth noticing here:

1. ``schema_translate_map``. Generated models carry a *symbolic* schema token
   rather than a literal schema name, and the token is resolved per-engine at
   execution time. That is what lets the same class hierarchy address
   ``main.sales.Customer`` on Databricks and ``ReplicaDb.sales.Customer`` on
   SQL Server without a second set of models.

2. Databricks is opened read-only by default. Delta has no session-scoped
   transaction in the sense the ORM assumes, so a Session that flushes dirty
   objects against Databricks will do something you did not intend. Writes are
   opt-in via ``readonly=False``.
"""

from __future__ import annotations

import os
import urllib.parse
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any

from sqlalchemy import Connection, Engine, create_engine, event

#: The hostname under two names. ``databricks-sql-connector``, whose naming
#: this follows, uses the first; the Databricks CLI and SDK write the second,
#: so anyone who ran ``databricks configure`` already has it exported. The
#: canonical name wins when both are set.
HOST_VARS = ("DATABRICKS_SERVER_HOSTNAME", "DATABRICKS_HOST")


class ConfigurationError(RuntimeError):
    """Connection settings could not be resolved.

    ``missing`` names the fields that came up empty, so a caller can render
    the remedy in terms of whatever it accepts.
    """

    def __init__(self, missing: Sequence[str]) -> None:
        self.missing = tuple(missing)
        super().__init__(
            "missing Databricks connection settings: "
            + ", ".join(self.missing)
        )


def _from_env(*names: str) -> str:
    for name in names:
        value = os.environ.get(name, "").strip()
        if value:
            return value
    return ""


@dataclass
class DatabricksConfig:
    host: str
    http_path: str
    token: str
    catalog: str
    schema: str = "default"

    @classmethod
    def from_env(
        cls,
        catalog: str | None = None,
        schema: str | None = None,
        *,
        host: str | None = None,
        http_path: str | None = None,
        token: str | None = None,
    ) -> DatabricksConfig:
        """Resolve each setting from its argument, then the environment.

        Every setting resolves the same way, so an argument means "use this
        instead of the variable" and passing one leaves the rest to the
        environment. ``DATABRICKS_HOST`` usually carries a full URL, which is
        why the scheme and any trailing slash come off.

        Raises ``ConfigurationError`` when a required setting resolves empty.
        """
        cfg = cls(
            host=host or _from_env(*HOST_VARS),
            http_path=http_path or _from_env("DATABRICKS_HTTP_PATH"),
            token=token or _from_env("DATABRICKS_TOKEN"),
            catalog=catalog or _from_env("DATABRICKS_CATALOG"),
            schema=schema or _from_env("DATABRICKS_SCHEMA") or "default",
        )
        cfg.host = cfg.host.strip().replace("https://", "").strip("/")

        missing = [
            name
            for name, value in (
                ("host", cfg.host),
                ("http_path", cfg.http_path),
                ("token", cfg.token),
                ("catalog", cfg.catalog),
            )
            if not value
        ]
        if missing:
            raise ConfigurationError(missing)
        return cfg

    def url(self) -> str:
        q = urllib.parse.urlencode(
            {
                "http_path": self.http_path,
                "catalog": self.catalog,
                "schema": self.schema,
            }
        )
        return f"databricks://token:{urllib.parse.quote(self.token)}@{self.host}?{q}"


def databricks_engine(
    cfg: DatabricksConfig,
    *,
    readonly: bool = True,
    schema_translate_map: dict[str | None, str] | None = None,
    **kwargs: Any,
) -> Engine:
    engine = create_engine(cfg.url(), **kwargs)
    if schema_translate_map:
        engine = engine.execution_options(
            schema_translate_map=schema_translate_map
        )
    if readonly:
        _install_readonly_guard(engine)
    return engine


def mssql_engine(
    url: str,
    *,
    schema_translate_map: dict[str | None, str] | None = None,
    **kwargs: Any,
) -> Engine:
    """e.g. mssql+pyodbc://user:pw@host/Db?driver=ODBC+Driver+18+for+SQL+Server

    For Windows integrated auth: append ``&trusted_connection=yes`` and omit
    credentials.
    """
    engine = create_engine(url, **kwargs)
    if schema_translate_map:
        engine = engine.execution_options(
            schema_translate_map=schema_translate_map
        )
    return engine


_WRITE_PREFIXES = (
    "insert",
    "update",
    "delete",
    "merge",
    "truncate",
    "drop",
    "alter",
    "create",
    "replace",
    "copy",
)


def _install_readonly_guard(engine: Engine) -> None:
    """Fail loudly rather than silently mutating a mirrored source."""

    @event.listens_for(engine, "before_cursor_execute")
    def _guard(
        conn: Connection,
        cursor: Any,
        statement: str,
        parameters: Any,
        context: Any,
        executemany: bool,
    ) -> None:
        head = statement.lstrip().split(None, 1)
        if head and head[0].lower() in _WRITE_PREFIXES:
            raise PermissionError(
                "write statement blocked on a read-only engine: "
                f"{head[0].upper()}...\n"
                "Pass readonly=False if you really mean to write."
            )
