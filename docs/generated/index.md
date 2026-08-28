# Bindings and schemas

The generated package is ordinary SQLAlchemy. What is unusual is that the same
classes address two backends, and that is what a `Binding` is for.

## A binding is an engine plus a schema map

```python
from models import Customer, Order
from stele.db import DatabricksConfig, databricks_engine, mssql_engine
from stele.runtime import Binding

lakehouse = Binding(
    engine=databricks_engine(
        DatabricksConfig.from_env(catalog="mycat"), readonly=True
    ),
    schemas={"dbo": "dbo", "sales": "sales"},
    readonly=True,
)

replica = Binding(
    engine=mssql_engine(
        "mssql+pyodbc://...?driver=ODBC+Driver+18+for+SQL+Server"
    ),
    schemas={"dbo": "dbo", "sales": "Sales"},
)
```

Note the schema maps differ. The lakehouse calls it `sales`; the replica calls
it `Sales`. Nothing in the model layer knows or cares.

```python
from sqlalchemy import select

stmt = select(Order).where(Order.RegionId == 4)

lakehouse.scalars(stmt)   # runs against mycat.sales.Order
replica.scalars(stmt)     # runs against ReplicaDb.Sales.Order
```

## How the schema token works

Generated classes carry a token, never a schema name:

```python
class Order(Base):
    __tablename__ = "Order"
    __table_args__ = {"schema": SCHEMA_SALES}   # "stele__sales"
```

A `Binding` turns its `schemas` dict into SQLAlchemy's `schema_translate_map`
and attaches it to the engine as an execution option. SQLAlchemy substitutes the
real name when it compiles a statement for that connection.

Two consequences worth holding on to:

- **Do not hardcode a schema** into `__table_args__` when reading or extending
  the generated code. It would work against one backend and break the other.
- **The translation happens at execution time**, so a standalone compile — say,
  `str(select(Order))` for debugging — still shows the token. `stele ddl` gets
  around this by cloning the tables into a fresh `MetaData` with real names
  first.

## Sessions

`Binding.session()` is a context manager over a `sessionmaker` with
`expire_on_commit=False`. It commits on a clean exit, rolls back on an
exception, and always closes:

```python
with lakehouse.session() as s:
    rows = s.scalars(select(Customer).where(Customer.RegionId == 4)).all()
```

A read-only binding sets `autoflush = False` and does not commit.

Two shortcuts sit on top for the common cases:

```python
lakehouse.scalars(select(Customer))                     # list[Customer]
lakehouse.rows(select(Customer.CustomerId, Customer.CustomerName))
```

Each opens and closes its own session. Use `session()` when you want several
statements in one.

## Read-only by default

`databricks_engine` installs a guard that raises `PermissionError` on any
statement beginning with `insert`, `update`, `delete`, `merge`, `truncate`,
`drop`, `alter`, `create`, `replace` or `copy`.

This is not caution for its own sake. Delta has no session-scoped transaction in
the sense the ORM assumes, so a `Session` that flushes dirty objects against the
lakehouse does something you did not intend and cannot roll back. Pass
`readonly=False` when you actually mean to write.

The replica has no such guard. It is an ordinary SQL Server database.
