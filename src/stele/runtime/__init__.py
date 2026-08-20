"""Runtime support imported by generated model packages.

Generated code depends on this module, so it must stay importable without any
database driver installed.
"""

from .base import Base, NAMING_CONVENTION, schema_map, schema_token
from .binding import Binding, replica_ddl
from .history import HistoryMixin, SCD2Config, as_of_all, normalize, utcnow

__all__ = [
    "Base",
    "Binding",
    "HistoryMixin",
    "NAMING_CONVENTION",
    "SCD2Config",
    "as_of_all",
    "normalize",
    "replica_ddl",
    "schema_map",
    "schema_token",
    "utcnow",
]
