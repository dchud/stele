"""Declarative base for generated models.

Generated classes never carry a literal schema name. They carry a *token*
(``stele__sales``) which is resolved to a real schema at execution time via
SQLAlchemy's ``schema_translate_map``. One set of classes therefore addresses
``main.sales.Customer`` on Databricks and ``ReplicaDb.sales.Customer`` on SQL
Server with no conditional logic in the models themselves.
"""

from __future__ import annotations

from typing import Any

from sqlalchemy import MetaData
from sqlalchemy.orm import DeclarativeBase

# Deterministic constraint names, so DDL emitted for the SQL Server replica is
# stable across regenerations and diffable.
NAMING_CONVENTION = {
    "ix": "ix_%(table_name)s_%(column_0_N_name)s",
    "uq": "uq_%(table_name)s_%(column_0_N_name)s",
    "ck": "ck_%(table_name)s_%(constraint_name)s",
    "fk": "fk_%(table_name)s_%(column_0_N_name)s_%(referred_table_name)s",
    "pk": "pk_%(table_name)s",
}

SCHEMA_TOKEN_PREFIX = "stele__"


def schema_token(logical_schema: str) -> str:
    return f"{SCHEMA_TOKEN_PREFIX}{logical_schema}"


def schema_map(**mapping: str) -> dict[str, str]:
    """Build a ``schema_translate_map`` from logical schema -> real schema.

    >>> schema_map(sales="sales", ref="reference_data")
    {'stele__sales': 'sales', 'stele__ref': 'reference_data'}
    """
    return {schema_token(k): v for k, v in mapping.items()}


class Base(DeclarativeBase):
    metadata = MetaData(naming_convention=NAMING_CONVENTION)

    def __repr__(self) -> str:  # pragma: no cover - convenience only
        pk = self.__mapper__.primary_key
        vals = ", ".join(
            f"{c.name}={getattr(self, c.name, None)!r}" for c in pk
        )
        return f"<{type(self).__name__} {vals}>"

    def to_dict(self, *, include_none: bool = True) -> dict[str, Any]:
        out = {}
        for col in self.__table__.columns:
            val = getattr(self, col.name, None)
            if val is None and not include_none:
                continue
            out[col.name] = val
        return out
