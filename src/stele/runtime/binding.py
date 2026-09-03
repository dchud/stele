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
from sqlalchemy.schema import CreateTable
from sqlalchemy.sql.compiler import Compiled
from sqlalchemy.sql.elements import CompilerElement

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
        self._translate = schema_map(**self.schemas)
        if self._translate:
            self.engine = self.engine.execution_options(
                schema_translate_map=self._translate
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

        The session executes selects only. An instant is a claim about what
        was true, and a statement that writes would escape the pin rather
        than honour it, so an insert, an update, a delete, or textual SQL
        raises ``PinnedSessionError``.

        Two paths go around that check, because it sees statements passed to
        the session: a flush of dirty objects, and anything executed on the
        engine or on the connection. Nothing flushes on its own, because the
        session has autoflush off and does not commit on exit, but an
        explicit ``flush`` or ``commit`` writes. Use `session` when you mean
        to change something.
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

    def compile(
        self, stmt: CompilerElement, *, literal_binds: bool = False
    ) -> Compiled:
        """``stmt`` rendered for this binding: its dialect, its schema names.

        Executing resolves the schema tokens on the connection. This resolves
        them without executing, for a statement handed to something that is
        not SQLAlchemy - a warehouse SQL API, another engine, a dataframe
        reader that unwraps to a raw cursor. All of those take SQL text and
        would otherwise receive the token.

        ``str(compiled)`` is the SQL and ``compiled.params`` the values in the
        dialect's paramstyle. ``literal_binds`` renders the values into the
        text instead, for a consumer that accepts a statement and nothing
        alongside it.

        A token this binding's `schemas` does not name renders as itself,
        which is what executing would do with it.

        The statement carries no session, so a pinned session does not narrow
        one compiled here. A point-in-time query carries its own instant:
        ``History.as_of(ts)`` puts the interval predicate in the statement.
        """
        compile_kwargs = {"literal_binds": True} if literal_binds else {}
        if not self._translate:
            # Asking for the render with no map trips an assertion inside the
            # compiler, and a binding is allowed to have no schemas.
            return stmt.compile(
                dialect=self.engine.dialect, compile_kwargs=compile_kwargs
            )
        return stmt.compile(
            dialect=self.engine.dialect,
            schema_translate_map=self._translate,
            render_schema_translate=True,
            compile_kwargs=compile_kwargs,
        )


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

    The schema tokens resolve during the compile: a ``schema_translate_map``
    is a compile-time argument as well as an execution option, so no
    connection is involved and the tables are compiled where they are.
    """
    from sqlalchemy.dialects import mssql, postgresql, sqlite

    from .base import SCHEMA_TOKEN_PREFIX

    dialects = {"mssql": mssql, "postgresql": postgresql, "sqlite": sqlite}
    if dialect_name not in dialects:
        raise ValueError(f"unsupported dialect for DDL: {dialect_name}")
    dialect = dialects[dialect_name].dialect()

    mapping = schemas or {}

    def resolve(token: str) -> str:
        if token.startswith(SCHEMA_TOKEN_PREFIX):
            logical = token[len(SCHEMA_TOKEN_PREFIX) :]
            return mapping.get(logical, logical)
        return mapping.get(token, token)

    # Every schema present is named in the map, so a token the caller did not
    # mention still loses its prefix rather than reaching the DDL.
    translate = {
        t.schema: resolve(t.schema)
        for t in metadata.sorted_tables
        if t.schema is not None
    }

    out: list[str] = []
    for table in metadata.sorted_tables:
        stmt = CreateTable(table)
        ddl = (
            stmt.compile(
                dialect=dialect,
                schema_translate_map=translate,
                render_schema_translate=True,
            )
            if translate
            else stmt.compile(dialect=dialect)
        )
        out.append(str(ddl).strip().rstrip(";") + ";")
    return "\n\n".join(out)
