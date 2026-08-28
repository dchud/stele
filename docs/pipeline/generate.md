# generate

```bash
stele generate --spec model.yaml --overlay overlay.yaml --out models
```

Applies the overlay to the spec and renders a Python package. Offline. The
output directory is overwritten.

## What comes out

One module per primary table, named after it, with the table's history class
alongside it. Plus `_schemas.py` holding the schema tokens, and an `__init__.py`
re-exporting every class along with `metadata` and `LOGICAL_SCHEMAS`.

```
models/
├── __init__.py
├── _schemas.py
├── customer.py      Customer and CustomerHistory
├── order.py         Order
├── order_line.py    OrderLine
└── region.py        Region
```

Import from the package, not from the modules: `from models import Customer`.

Generated code is meant to be read. It uses `DeclarativeBase`, `Mapped[...]`
with `mapped_column`, and typed `relationship` — the shapes someone would write
by hand:

```python
class Widget(Base):
    __tablename__ = "Widget"
    __table_args__ = {"schema": SCHEMA_DBO}

    WidgetId: Mapped[int] = mapped_column(
        BigInteger(),
        primary_key=True,
        nullable=False,
    )
    OwnerId: Mapped[int | None] = mapped_column(
        BigInteger(),
        ForeignKey(f"{SCHEMA_DBO}.Owner.OwnerId"),
        nullable=True,
    )

    # inferred relationship (confidence 0.8)
    owner: Mapped["Owner | None"] = relationship(
        "Owner",
        back_populates="widgets",
    )
```

The comment carrying the confidence is deliberate. A relationship that came out
of a name match reads differently from one you declared, and the generated file
is where that distinction is most useful.

## Naming

Table and column names are preserved by default: `WidgetId` stays `WidgetId`.
`--snake-case` renames attributes to `widget_id` while keeping the column names
in the mapping, which is the more Pythonic shape and the more disruptive one if
the replica's consumers already expect the original.

Class names are derived from table names — `widget_type` becomes `WidgetType`,
`ETL_log` becomes `ETLLog` — and can be overridden per table with `class_name`
in the overlay.

!!! warning "Same table name in two schemas"

    Class and module names come from the table name alone, so `dbo.Customer`
    and `sales.Customer` cannot both be `Customer`. The second one reached
    becomes `Customer2` in `customer_2.py`, and which schema that is can only
    be recovered by reading its `__table_args__`.

    Set `class_name` in the overlay for both of them and the ambiguity goes
    away:

    ```yaml
    tables:
      dbo.Customer:
        class_name: Customer
      sales.Customer:
        class_name: SalesCustomer
    ```

## What it warns about

```
wrote 2 module(s) / 6 class(es) to models

  3 table(s) generated without a primary key:
    dbo.AuditTrail
    -> set primary_key in the overlay; ORM identity is unreliable until you do

  2 column(s) will not round-trip to SQL Server:
    dbo.Event.Payload (VARIANT -> JSON)
```

A table with no key still generates, with a mapper-level guess and a loud
comment in the file. SQLAlchemy needs something to build an identity map from,
and refusing to emit the class would be worse than emitting a flagged one — but
the identity map is unreliable until you fix it.

## Schema tokens

Nothing in the generated package names a real schema. Each class is mapped to a
constant like `SCHEMA_DBO`, whose value is a token — `stele__dbo`. A `Binding`
resolves tokens to real names per engine, which is how the same class addresses
two backends. See [Bindings and schemas](../generated/index.md).

Resist hardcoding a schema into `__table_args__` when reading the output. It
would work against one backend and break the other.
