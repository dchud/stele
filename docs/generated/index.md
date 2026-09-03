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
- **The translation belongs to the binding, not the statement.** `str(select(
  Order))` on its own shows the token, because nothing in that call says which
  backend you meant. `binding.compile(stmt)` says which, and renders the real
  name without a connection.

## Compiling a statement

Everything above happens on a connection. A consumer that is not SQLAlchemy —
a warehouse SQL API, another engine, a dataframe reader that takes a query
string — needs the SQL with real schema names already in it.

```python
from sqlalchemy import select

stmt = select(Order.OrderId).where(Order.RegionId == 4)

str(lakehouse.compile(stmt))
# SELECT sales.`Order`.`OrderId` FROM sales.`Order`
# WHERE sales.`Order`.`RegionId` = :`RegionId_1`

str(replica.compile(stmt))
# SELECT Sales.[Order].[OrderId] FROM Sales.[Order]
# WHERE Sales.[Order].[RegionId] = :RegionId_1
```

One statement, two backends, two schema names and two dialects — what
execution does, without executing.

### Parameters, or values in the text

The values stay out of the SQL by default, where a driver expects them:

```python
compiled = lakehouse.compile(stmt)
str(compiled)     # ... WHERE sales.`Order`.`RegionId` = :`RegionId_1`
compiled.params   # {'RegionId_1': 4}
```

`literal_binds=True` renders them into the text instead, for a consumer that
accepts a statement and nothing alongside it:

```python
str(lakehouse.compile(stmt, literal_binds=True))
# ... WHERE sales.`Order`.`RegionId` = 4
```

### A point-in-time query carries its own instant

A compiled statement has no session, so pinning one does not narrow it. Put
the instant in the statement:

```python
str(lakehouse.compile(OrderHistory.as_of("2026-01-01"), literal_binds=True))
# SELECT ... FROM sales.`Order_history`
# WHERE sales.`Order_history`.`StartDate` <= '2026-01-01 00:00:00.000000'
#   AND (sales.`Order_history`.`EndDate` IS NULL
#        OR sales.`Order_history`.`EndDate` > '2026-01-01 00:00:00.000000')
```

The interval predicate is in the SQL, so whatever runs it reads the right
version without reimplementing the comparison.

### Two edges

A token the binding's `schemas` does not name renders as itself, which is what
executing would do with it. A binding with no `schemas` compiles without
translation, so every token stays one.

### What it wraps

`compile()` is SQLAlchemy's, available on any statement. The method supplies
this binding's dialect and map:

```python
from stele.runtime import schema_map

stmt.compile(
    dialect=binding.engine.dialect,
    schema_translate_map=schema_map(**binding.schemas),
    render_schema_translate=True,
)
```

That works as written. `binding.compile(stmt)` is the same call with two
things taken care of: asking for the render with an empty map raises a bare
`AssertionError`, and nothing else in the documentation points at those three
arguments.

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

Delta has no session-scoped transaction in the sense the ORM assumes, so a
`Session` that flushes dirty objects against the lakehouse does something you
did not intend and cannot roll back. Pass `readonly=False` when you actually
mean to write.

The replica has no such guard. It is an ordinary SQL Server database.

## What every class carries

`Base` adds two methods to SQLAlchemy's declarative base. Both read and key by
*attribute* name rather than column name, which matters on every class where
the two differ: `--snake-case` renames every attribute, and a column name
Python cannot use as an identifier is reached by one it can. The naming rule is
under [generate](../pipeline/generate.md).

A table with a column called `class` and one called `Unit Price` generates
this:

```python
class Part(Base):
    __tablename__ = "Part"
    __table_args__ = {"schema": SCHEMA_DBO}

    PartId: Mapped[int] = mapped_column(
        BigInteger(), primary_key=True, autoincrement=False, nullable=False
    )
    class_: Mapped[str | None] = mapped_column(
        "class", String(20).with_variant(NVARCHAR(20), "mssql"), nullable=True
    )
    Unit_Price: Mapped[decimal.Decimal | None] = mapped_column(
        "Unit Price",
        Numeric(precision=18, scale=4, asdecimal=True),
        nullable=True,
    )
```

`to_dict()` returns the mapped column values, keyed by the names the class
answers to:

```python
>>> part.to_dict()
{'PartId': 7, 'class_': 'fastener', 'Unit_Price': Decimal('1.50')}
```

Relationships are left out; only mapped columns are values. Because the read
and the key both use the attribute name, the result is a set of keyword
arguments the class can be constructed from:

```python
Part(**part.to_dict())
```

`include_none=False` drops the entries whose value is `None`:

```python
>>> Part(PartId=8, class_="washer").to_dict(include_none=False)
{'PartId': 8, 'class_': 'washer'}
```

`__repr__` names the primary key the same way:

```python
>>> part
<Part PartId=7>
```

A class generated without a known primary key gets one guessed at the mapper
level, and the repr names whatever that guess covers. It is longer, and it
carries no promise of identifying the row:

```python
>>> reading
<Reading SensorId=3, TakenAt=datetime.datetime(2026, 1, 1, 0, 0)>
```

`stele generate` names the tables that applies to, and setting `primary_key` in
the overlay is what settles it.

Two smaller things sit on `Base` for the same reason. A column named
`to_dict`, `metadata` or `registry` is renamed, because the base class has
already claimed the name. And `Base.metadata` carries a naming convention for
indexes, constraints and keys, so the DDL `stele ddl` emits for the replica
names them deterministically and two runs diff cleanly.
