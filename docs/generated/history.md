# History

A table paired with an SCD2 companion gets a second class, and the two are
linked three ways. Point-in-time questions go through helper methods that build
selects; nothing here issues a query until you execute it.

## What generation produces

```python
class Customer(Base):
    __tablename__ = "Customer"
    __table_args__ = {"schema": SCHEMA_DBO}
    ...

    # no FK constraint exists; joined on the business key, read-only
    history: Mapped[list["CustomerHistory"]] = relationship(
        "CustomerHistory",
        primaryjoin="Customer.CustomerId == foreign(CustomerHistory.CustomerId)",
        order_by="CustomerHistory.StartDate",
        viewonly=True,
    )


class CustomerHistory(Base, HistoryMixin):
    __tablename__ = "Customer_history"
    __table_args__ = {"schema": SCHEMA_DBO}

    CustomerId: Mapped[int] = mapped_column(BigInteger(), primary_key=True, nullable=False)
    CustomerName: Mapped[str | None] = mapped_column(...)
    RegionId: Mapped[int | None] = mapped_column(BigInteger(), nullable=True)
    StartDate: Mapped[datetime.datetime] = mapped_column(..., primary_key=True, nullable=False)
    EndDate: Mapped[datetime.datetime | None] = mapped_column(..., nullable=True)

    __history_of__ = Customer
    __scd2__ = SCD2Config(
        start_attr="StartDate",
        end_attr="EndDate",
        end_open="null",
        end_sentinel=None,
        interval="half_open",
        current_in_history=True,
        naive_utc=True,
        business_key=("CustomerId",),
    )
```

The three links:

- **`Customer.history`** is an ordinary SQLAlchemy relationship, ordered by
  `StartDate`. It is `viewonly` and uses `foreign()` in its `primaryjoin`
  because there is no constraint to join on — the annotation tells SQLAlchemy
  which side is the dependent one.
- **`__history_of__`** points back at the primary class.
- **`__scd2__`** carries the interval semantics every helper reads.

The history class's key is the business key plus the interval start, which is
what makes a version row uniquely identifiable.

## The straightforward case

```python
customer = lakehouse.scalars(
    select(Customer).where(Customer.CustomerId == 42)
)[0]

for version in customer.history:
    print(version.StartDate, version.CustomerName)
```

That is the whole story when you already have the entity and want its versions.

## Point-in-time selects

```python
import datetime as dt

CustomerHistory.as_of(dt.datetime(2026, 3, 1))
CustomerHistory.current()
CustomerHistory.changes_between("2026-01-01", "2026-04-01")
CustomerHistory.versions_of(customer)
CustomerHistory.timeline(customer)      # reads better; same as versions_of
```

Each returns a `Select`. Nothing runs until you execute it, so they compose with
ordinary SQLAlchemy:

```python
stmt = (
    CustomerHistory.as_of("2026-03-01")
    .where(CustomerHistory.RegionId == 4)
    .order_by(CustomerHistory.CustomerName)
)
lakehouse.scalars(stmt)
```

They accept a `datetime`, a `date`, or an ISO 8601 string.

### Why they exist

The predicate for "valid at this instant" depends on three modelling decisions
that produce **wrong answers rather than errors** if you guess:

- whether an open interval's end is `NULL` or a sentinel date
- whether the interval is `[start, end)` or `[start, end]`
- whether the timestamps are naive UTC or aware

`as_of` reads all three off `__scd2__` and builds the right predicate. Writing
`StartDate <= ts and EndDate > ts` by hand is correct for exactly one of the
configurations and silently wrong for the others, most visibly on boundary
dates.

### `current` is the odd one

```python
CustomerHistory.current()
```

With `current_in_history=True` this selects from the history table where the
interval is open. With `current_in_history=False` it selects from the **primary
table**, because a history table that does not hold the live row would be
missing the newest version of every entity.

So the rows come back as instances of one class or the other depending on how
the model is configured, which is why this one select does not name its element
type. See [What the types say](typing.md#the-two-that-stay-open).

### `versions_of`

Reads the business key off whatever you hand it, by name. An instance of either
class, a tuple in business-key order, a list, or a dict all work:

```python
CustomerHistory.versions_of(customer)
CustomerHistory.versions_of((42,))
CustomerHistory.versions_of({"CustomerId": 42})
```

It raises `ValueError` if the model has no business key, which happens when the
primary table has no primary key. Set one in the overlay.

### Several models at once

```python
from stele.runtime.history import as_of_all

snapshots = as_of_all([CustomerHistory, OrderHistory], "2026-03-01")
rows = lakehouse.scalars(snapshots["CustomerHistory"])
```

## Joining from a history row

A history class carries **no relationships**. Its foreign key columns are plain
columns: `CustomerHistory.RegionId` exists, `CustomerHistory.region` does not.

That absence is deliberate. The obvious relationship would join a March version
row to the region as it stands today, which is a silently wrong answer of
exactly the kind the helpers above exist to prevent.

Write the join you actually mean. If the parent has no history of its own, and
its current state is what you want, say so:

```python
from sqlalchemy import select

at = dt.datetime(2026, 3, 1)

stmt = (
    select(CustomerHistory, Region)
    .join(Region, Region.RegionId == CustomerHistory.RegionId)
    .where(CustomerHistory.valid_at(at))
)
for customer_version, region in lakehouse.rows(stmt):
    ...
```

`valid_at` is the same predicate `as_of` uses, exposed so you can put it in a
statement you built yourself. `overlaps(start, end)` is the interval version.

If the parent *does* have history, the point-in-time correct join goes to its
history table on the same instant:

```python
stmt = (
    select(CustomerHistory, RegionHistory)
    .join(
        RegionHistory,
        RegionHistory.RegionId == CustomerHistory.RegionId,
    )
    .where(CustomerHistory.valid_at(at))
    .where(RegionHistory.valid_at(at))
)
```

Two `valid_at` predicates, one instant. That is the SCD2 as-of join, and it is
the version that answers "what did this look like in March" rather than "what
did March's row point at, as things stand now".
