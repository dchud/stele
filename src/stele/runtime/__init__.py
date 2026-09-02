"""Runtime support imported by generated model packages.

Generated code depends on this module, so it must stay importable without any
database driver installed.
"""

from .asof import PinnedSessionError, history_classes, pin, utcnow
from .base import NAMING_CONVENTION, Base, schema_map, schema_token
from .binding import Binding, replica_ddl
from .history import HistoryMixin, SCD2Config, as_of_all, normalize

__all__ = [
    "NAMING_CONVENTION",
    "Base",
    "Binding",
    "HistoryMixin",
    "PinnedSessionError",
    "SCD2Config",
    "as_of_all",
    "history_classes",
    "normalize",
    "pin",
    "replica_ddl",
    "schema_map",
    "schema_token",
    "utcnow",
]
