"""stele - introspect a mirrored SCD2 model, emit portable SQLAlchemy code."""

__version__ = "0.1.0"

from .spec import (
    ColumnSpec,
    ForeignKeySpec,
    HistoryConfig,
    ModelSpec,
    TableSpec,
)

__all__ = [
    "ColumnSpec",
    "ForeignKeySpec",
    "HistoryConfig",
    "ModelSpec",
    "TableSpec",
    "__version__",
]
