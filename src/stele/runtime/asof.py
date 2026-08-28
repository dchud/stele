"""Pin a session to an instant.

A history table answers questions about moments, and almost every question
about a moment involves more than one table. Writing the instant into every
join by hand is where point-in-time queries go wrong: it is easy to filter the
row you selected and forget the row you traversed to, and the result looks
plausible either way.

So the instant lives on the session instead. Inside a pinned session every
history table shows only the version valid at that instant, whether it is
reached by a select, a lazy load, an eager load, or a join written by hand.
That is one sentence, and it is the whole rule.

Statements that name their own moment override the session's, which is what
``TIME_SCOPE`` on a statement declares. Without it the two predicates would be
combined and a sub-question inside a pinned session would silently return
nothing.

The criteria attach to a select, so a pinned session executes selects and
nothing else. An insert, an update, a delete, or textual SQL the ORM cannot
read would run against every version rather than the pinned one, so it raises.
That check sees statements passed to the session, which is not every way to
write: a flush of dirty objects, and anything executed on the engine or on the
connection, go around it.
"""

from __future__ import annotations

import datetime as dt

from sqlalchemy import event
from sqlalchemy.orm import ORMExecuteState, Session, with_loader_criteria

from .base import Base
from .history import (
    CURRENT,
    TIME_SCOPE,
    HistoryMixin,
    TimestampLike,
)


class PinnedSessionError(RuntimeError):
    """A statement that cannot mean anything at the session's instant."""


def history_classes() -> list[type[HistoryMixin]]:
    """Every mapped class carrying an SCD2 configuration.

    Read from the registry rather than passed in, so a binding does not have
    to be told which classes a generated package happens to contain.
    """
    out: list[type[HistoryMixin]] = []
    for mapper in Base.registry.mappers:
        cls = mapper.class_
        if issubclass(cls, HistoryMixin) and hasattr(cls, "__scd2__"):
            out.append(cls)
    return out


def _describe(state: ORMExecuteState) -> str:
    """Name the statement kind, for the refusal message."""
    if state.is_insert:
        return "an insert"
    if state.is_update:
        return "an update"
    if state.is_delete:
        return "a delete"
    return "not one"


def pin(
    session: Session,
    at: TimestampLike,
    overrides: dict[type[HistoryMixin], TimestampLike | None] | None = None,
) -> None:
    """Constrain every history class in `session` to the version valid at `at`.

    An entry in `overrides` names a different instant for one class, or
    ``None`` to leave that class unfiltered.

    A statement the criteria cannot narrow raises ``PinnedSessionError``
    rather than executing unnarrowed.

    The criteria are built once here rather than per statement. The instant is
    fixed for the life of the session, and building them per execution costs
    three times as much on a catalog with a few hundred history tables.
    """
    chosen = dict(overrides or {})
    options = []
    for cls in history_classes():
        when = chosen.get(cls, at)
        if when is None:
            continue
        options.append(
            with_loader_criteria(cls, cls.valid_at(when), include_aliases=True)
        )

    @event.listens_for(session, "do_orm_execute")
    def _narrow(state: ORMExecuteState) -> None:
        if not state.is_select:
            raise PinnedSessionError(
                f"a session pinned to {at} executes selects only, and this "
                f"is {_describe(state)}.\n"
                "The criteria cannot be applied to it, so it would run "
                "against every version rather than the pinned one.\n"
                "Use an unpinned session for writes and for textual SQL."
            )
        scope = state.execution_options.get(TIME_SCOPE)
        if scope == CURRENT:
            raise PinnedSessionError(
                f"current() means now, but this session is pinned to {at}.\n"
                "For the version at that instant, select the class directly.\n"
                "For today, use an unpinned session."
            )
        if scope is not None:
            # The statement names its own moment; it wins.
            return
        state.statement = state.statement.options(*options)


def utcnow() -> dt.datetime:
    """The instant a pinned session means when none is given."""
    return dt.datetime.now(dt.UTC)
