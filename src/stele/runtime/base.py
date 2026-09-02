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

    def __repr__(self) -> str:
        mapper = self.__mapper__
        keys = [
            mapper.get_property_by_column(c).key for c in mapper.primary_key
        ]
        vals = ", ".join(f"{k}={getattr(self, k)!r}" for k in keys)
        return f"<{type(self).__name__} {vals}>"

    def to_dict(self, *, include_none: bool = True) -> dict[str, Any]:
        """The mapped column values, keyed by attribute name.

        A column's attribute name is often not the column's own name: a
        keyword, a name that is not an identifier, and every name under
        `--snake-case` all rename the attribute. The attribute name is the
        one the class answers to, so it is both what the value is read by
        and what the value is keyed by, which makes the result a set of
        keyword arguments the class can be constructed from.

        `include_none=False` drops the entries whose value is None.
        """
        out: dict[str, Any] = {}
        for attr in self.__mapper__.column_attrs:
            val = getattr(self, attr.key)
            if val is None and not include_none:
                continue
            out[attr.key] = val
        return out
