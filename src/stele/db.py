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
from dataclasses import dataclass

from sqlalchemy import Engine, create_engine, event


@dataclass
class DatabricksConfig:
    host: str
    http_path: str
    token: str
    catalog: str
    schema: str = "default"

    @classmethod
    def from_env(cls, catalog: str | None = None, schema: str | None = None):
        missing = [
            name
            for name in (
                "DATABRICKS_SERVER_HOSTNAME",
                "DATABRICKS_HTTP_PATH",
                "DATABRICKS_TOKEN",
            )
            if not os.environ.get(name)
        ]
        if missing:
            raise RuntimeError(
                "missing environment variables: "
                + ", ".join(missing)
                + "\nSet them, or pass --host/--http-path/--token."
            )
        return cls(
            host=os.environ["DATABRICKS_SERVER_HOSTNAME"].replace("https://", "").strip("/"),
            http_path=os.environ["DATABRICKS_HTTP_PATH"],
            token=os.environ["DATABRICKS_TOKEN"],
            catalog=catalog or os.environ.get("DATABRICKS_CATALOG", "main"),
            schema=schema or os.environ.get("DATABRICKS_SCHEMA", "default"),
        )

    def url(self) -> str:
        q = urllib.parse.urlencode(
            {"http_path": self.http_path, "catalog": self.catalog, "schema": self.schema}
        )
        return f"databricks://token:{urllib.parse.quote(self.token)}@{self.host}?{q}"


def databricks_engine(
    cfg: DatabricksConfig,
    *,
    readonly: bool = True,
    schema_translate_map: dict[str | None, str] | None = None,
    **kwargs,
) -> Engine:
    engine = create_engine(cfg.url(), **kwargs)
    if schema_translate_map:
        engine = engine.execution_options(schema_translate_map=schema_translate_map)
    if readonly:
        _install_readonly_guard(engine)
    return engine


def mssql_engine(
    url: str,
    *,
    schema_translate_map: dict[str | None, str] | None = None,
    **kwargs,
) -> Engine:
    """e.g. mssql+pyodbc://user:pw@host/Db?driver=ODBC+Driver+18+for+SQL+Server

    For Windows integrated auth: append ``&trusted_connection=yes`` and omit
    credentials.
    """
    engine = create_engine(url, **kwargs)
    if schema_translate_map:
        engine = engine.execution_options(schema_translate_map=schema_translate_map)
    return engine


_WRITE_PREFIXES = (
    "insert", "update", "delete", "merge", "truncate",
    "drop", "alter", "create", "replace", "copy",
)


def _install_readonly_guard(engine: Engine) -> None:
    """Fail loudly rather than silently mutating a mirrored source."""

    @event.listens_for(engine, "before_cursor_execute")
    def _guard(conn, cursor, statement, parameters, context, executemany):
        head = statement.lstrip().split(None, 1)
        if head and head[0].lower() in _WRITE_PREFIXES:
            raise PermissionError(
                f"write statement blocked on a read-only engine: {head[0].upper()}...\n"
                "Pass readonly=False if you really mean to write."
            )
