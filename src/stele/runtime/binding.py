"""Bind generated models to a backend.

A ``Binding`` is an engine plus the schema translation for that backend.
Swapping between the Databricks source and the SQL Server replica is a matter
of picking a different Binding; nothing in the model layer changes.
"""

from __future__ import annotations

from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from dataclasses import dataclass, field
from typing import Any

from sqlalchemy import Engine, Executable, MetaData, Row
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.schema import CreateTable, SchemaConst

from .base import schema_map


@dataclass
class Binding:
    engine: Engine
    schemas: dict[str, str] = field(default_factory=dict)
    readonly: bool = False

    def __post_init__(self) -> None:
        translate = schema_map(**self.schemas)
        if translate:
            self.engine = self.engine.execution_options(
                schema_translate_map=translate
            )
        self._sessionmaker = sessionmaker(
            bind=self.engine, expire_on_commit=False
        )

    @contextmanager
    def session(self, **kwargs: Any) -> Iterator[Session]:
        s: Session = self._sessionmaker(**kwargs)
        if self.readonly:
            s.autoflush = False
        try:
            yield s
            if not self.readonly:
                s.commit()
        except Exception:
            s.rollback()
            raise
        finally:
            s.close()

    def scalars(self, stmt: Executable) -> list[Any]:
        with self.session() as s:
            return list(s.scalars(stmt))

    def rows(self, stmt: Executable) -> Sequence[Row[Any]]:
        with self.session() as s:
            return s.execute(stmt).all()


def replica_ddl(
    metadata: MetaData,
    *,
    dialect_name: str = "mssql",
    schemas: dict[str, str] | None = None,
) -> str:
    """Emit CREATE TABLE statements for the failover replica.

    Because the models carry generic types with mssql variants, this produces
    real NVARCHAR/DATETIME2 DDL rather than the STRING-everywhere shape the
    federated catalog reports.

    Note that ``schema_translate_map`` is applied by the *connection* at
    execution time, so it cannot resolve tokens during a standalone compile.
    Tables are therefore cloned into a fresh MetaData with real schema names
    before compiling.
    """
    from sqlalchemy import MetaData
    from sqlalchemy.dialects import mssql, postgresql, sqlite

    from .base import SCHEMA_TOKEN_PREFIX

    dialects = {"mssql": mssql, "postgresql": postgresql, "sqlite": sqlite}
    if dialect_name not in dialects:
        raise ValueError(f"unsupported dialect for DDL: {dialect_name}")
    dialect = dialects[dialect_name].dialect()

    mapping = schemas or {}

    def resolve(token: str | None) -> str | None:
        if token is None:
            return None
        if token.startswith(SCHEMA_TOKEN_PREFIX):
            logical = token[len(SCHEMA_TOKEN_PREFIX) :]
            return mapping.get(logical, logical)
        return mapping.get(token, token)

    target = MetaData(naming_convention=metadata.naming_convention)
    for table in metadata.sorted_tables:
        # A table with no schema keeps not having one, which is what
        # RETAIN_SCHEMA means; only a resolved token is worth overriding.
        resolved = resolve(table.schema)
        table.to_metadata(
            target,
            schema=resolved if resolved else SchemaConst.RETAIN_SCHEMA,
            referred_schema_fn=lambda tbl, to_schema, fk, ref: resolve(ref),
        )

    out: list[str] = []
    for table in target.sorted_tables:
        sql = str(CreateTable(table).compile(dialect=dialect))
        out.append(sql.strip().rstrip(";") + ";")
    return "\n\n".join(out)
