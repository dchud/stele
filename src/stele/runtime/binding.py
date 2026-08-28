"""Bind generated models to a backend.

A ``Binding`` is an engine plus the schema translation for that backend.
Swapping between the Databricks source and the SQL Server replica is a matter
of picking a different Binding; nothing in the model layer changes.
"""

from __future__ import annotations

from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from dataclasses import dataclass, field
from typing import Any, TypeVar

from sqlalchemy import Engine, MetaData, Row, Select
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.schema import CreateTable, SchemaConst

from .asof import pin, utcnow
from .base import schema_map
from .history import HistoryMixin, TimestampLike

# ``Select`` carries what it returns as a tuple of column types, so a
# single-entity select is ``Select[tuple[Customer]]``. ``scalars`` takes that
# one-tuple form because it yields the first column and nothing else;
# ``rows`` accepts any width, which is what the tuple bound expresses.
_T = TypeVar("_T")
_Ts = TypeVar("_Ts", bound=tuple[Any, ...])


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
        with self._open(self.readonly, kwargs) as s:
            yield s

    @contextmanager
    def as_of(
        self,
        at: TimestampLike | None = None,
        overrides: dict[type[HistoryMixin], TimestampLike | None]
        | None = None,
        **kwargs: Any,
    ) -> Iterator[Session]:
        """A session where every history table shows one instant.

        `at` defaults to now, so pinning to the present is a call rather than
        a timestamp built at the call site. An entry in `overrides` names a
        different instant for one class, or ``None`` to leave it unfiltered.

        The session is read-only. An instant is a claim about what was true,
        and a statement that writes would escape the pin rather than honour
        it.
        """
        with self._open(True, kwargs) as s:
            pin(s, utcnow() if at is None else at, overrides)
            yield s

    @contextmanager
    def _open(
        self, readonly: bool, kwargs: dict[str, Any]
    ) -> Iterator[Session]:
        s: Session = self._sessionmaker(**kwargs)
        if readonly:
            s.autoflush = False
        try:
            yield s
            if not readonly:
                s.commit()
        except Exception:
            s.rollback()
            raise
        finally:
            s.close()

    def scalars(self, stmt: Select[tuple[_T]]) -> list[_T]:
        with self.session() as s:
            return list(s.scalars(stmt))

    def rows(self, stmt: Select[_Ts]) -> Sequence[Row[_Ts]]:
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
