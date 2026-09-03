# History

A table paired with an SCD2 companion gets a second class, a set of selects that
build the right interval predicate, and a way to ask a whole query about one
moment.

## What generation produces

```python
class Customer(Base):
    __tablename__ = "Customer"
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
    ...
    StartDate: Mapped[datetime.datetime] = mapped_column(..., primary_key=True)
    EndDate: Mapped[datetime.datetime | None]

    # the version valid at the session's instant; unpinned this matches
    # every version
    region: Mapped["RegionHistory | None"] = relationship(
        "RegionHistory",
        primaryjoin="CustomerHistory.RegionId == foreign(RegionHistory.RegionId)",
        viewonly=True,
    )
    # Tier does not version; this is its only state
    tier: Mapped["Tier | None"] = relationship("Tier", viewonly=True)

    __history_of__ = Customer
    __scd2__ = SCD2Config(
        start_attr="StartDate", end_attr="EndDate",
        end_open="null", interval="half_open",
        current_in_history=True, naive_utc=True,
        business_key=("CustomerId",),
    )
```

`Customer.history` is the version list, ordered and read-only. `__history_of__`
and `__scd2__` are what the helpers read.

Look at `CustomerHistory.region`. It joins on the key column and **nothing
else** — no interval predicate. Which version of the region it finds is the
session's business, not the join's, which is what the rest of this page is
about.

## The rule

**Inside a pinned session, every history table shows only the version valid at
that instant.**

That covers the select you wrote, the relationship you traversed, the eager
load you asked for, and the join you wrote by hand. One sentence, no exceptions
inside it.

## Which call do I want

| You want | Use |
|---|---|
| a whole query about one moment | `binding.as_of(ts)` |
| the same, about right now | `binding.as_of()` |
| every version of one entity | `versions_of(entity)` or `timeline(entity)` |
| one select about a moment, in an ordinary session | `as_of(ts)` |
| versions that came into effect in a window | `changes_between(a, b)` |
| the live version | `current()`, in an unpinned session |
| the predicate, for a query you are building | `valid_at(ts)`, `overlaps(a, b)` |

## Pinning a session

Region 3 was renamed from `North` to `Northern` in September 2025. Customer 42
was `Acme Ltd` until February 2026.

```python
with binding.as_of("2025-03-01") as s:
    for c in s.scalars(select(CustomerHistory)):
        print(c.CustomerName, c.region.RegionName)
```

```
Acme Ltd  North
```

Both halves are as of March 2025. The customer version is the one valid then,
and so is the region name — not the name the region has today.

With no argument, the instant is now:

```python
with binding.as_of() as s:
    ...
```

```
Acme Corp  Northern
```

Nothing is filtered unless you ask. An ordinary `binding.session()` shows every
version of everything, which is what a table of versions contains.

## Saying you mean something else

Three ways, in increasing bluntness.

**One entity at a different moment.** Useful when you want a historical fact
labelled with a current name:

```python
with binding.as_of("2025-03-01", {RegionHistory: "2026-06-01"}) as s:
    c = s.scalars(select(CustomerHistory)).one()
```

```
Acme Ltd  Northern
```

**One entity unfiltered:**

```python
with binding.as_of("2025-03-01", {RegionHistory: None}) as s:
    ...
```

```
Acme Ltd  regions unfiltered
  North from 2024-01-01
  Northern from 2025-09-01
```

**A statement that names its own moment wins.** `versions_of` and `timeline`
mean every version, and say so, so they are unaffected by the pin:

```python
with binding.as_of("2025-03-01") as s:
    s.scalars(CustomerHistory.versions_of((42,)))   # every version
    s.scalars(select(CustomerHistory))              # the pinned one
```

```
versions_of  -> ['Acme Ltd', 'Acme Corp']
plain select -> ['Acme Ltd']
```

`as_of` and `changes_between` carry their own instant, so a sub-question about
another moment works and leaves the session alone:

```python
with binding.as_of("2025-03-01") as s:
    s.scalars(CustomerHistory.as_of("2026-06-01"))
    s.scalars(select(CustomerHistory))
```

```
as_of(2026-06-01)    -> ['Acme Corp']
session still pinned -> ['Acme Ltd']
```

Without that, the two predicates would combine and the sub-question would
return nothing.

## What a pinned session refuses

`current()` means now, and a pinned session is not about now:

```python
with binding.as_of("2025-03-01") as s:
    s.scalars(CustomerHistory.current())
```

```
PinnedSessionError: current() means now, but this session is pinned to 2025-03-01.
For the version at that instant, select the class directly.
For today, use an unpinned session.
```

It raises rather than guessing because both readings are plausible and both
were wrong before. Unpinned it works as usual:

```
['Acme Corp']
```

A statement that is not a select refuses too. The criteria attach to a select,
so anything else would run against every version rather than the pinned one:

```python
with binding.as_of("2025-03-01") as s:
    s.execute(update(CustomerHistory).values(CustomerName="Acme"))
```

```
PinnedSessionError: a session pinned to 2025-03-01 executes selects only, and this is an update.
The criteria cannot be applied to it, so it would run against every version rather than the pinned one.
Use an unpinned session for writes and for textual SQL.
```

Inserts and deletes raise the same way, and so does `text()` in either
direction: SQL the ORM cannot read is SQL the pin cannot narrow, so a textual
select inside a pinned session would report every version.

## Point-in-time selects

Every helper returns a `Select`. Nothing runs until you execute it, so they
compose:

```python
stmt = (
    CustomerHistory.as_of("2025-03-01")
    .where(CustomerHistory.RegionId == 3)
    .order_by(CustomerHistory.CustomerName)
)
```

They accept a `datetime`, a `date`, or an ISO 8601 string.

The reason they exist is that the predicate for "valid at this instant" depends
on three modelling decisions that produce **wrong answers rather than errors**
if guessed: whether an open interval ends `NULL` or at a sentinel, whether the
interval is `[start, end)` or `[start, end]`, and whether timestamps are naive
UTC. `valid_at` reads all three off `__scd2__`. Writing the predicate by hand is
correct for one configuration and quietly wrong for the others, most visibly on
boundary dates.

`versions_of` reads the business key off whatever you pass, by name — an
instance of either class, a tuple in key order, a list, or a dict:

```python
CustomerHistory.versions_of(customer)
CustomerHistory.versions_of((42,))
CustomerHistory.versions_of({"CustomerId": 42})
```

## Limits worth knowing

**A traversal outside a pinned session is not meaningful.** The relationship
matches every version of the parent, so a scalar attribute returns whichever
came first and SQLAlchemy warns:

```
Acme Ltd  North
SAWarning: Multiple rows returned with uselist=False for lazily-loaded
attribute 'CustomerHistory.region'
```

The warning only fires where the parent actually has more than one version, so
it is a help and not a guarantee. Traverse from a history class inside a pinned
session. `-W error::SAWarning` in a test suite turns the cases it does catch
into failures.

**`customer.history` narrows too.** It is a relationship to a history class, so
the rule applies to it like everything else:

```
unpinned  -> ['Acme Ltd', 'Acme Corp']
pinned    -> ['Acme Ltd']
```

Use `versions_of` or `timeline` for the whole timeline; they mean every version
wherever they are called.

**A pinned session pins history tables only.** A current table has no interval,
so nothing can move it, and a query touching both returns two moments at once:

```
history: Acme Ltd  region=North
current: Northern
```

The two rows describe different moments, which is what the two tables hold.
Notice it when you mix them.

**A parent that does not version is reached as itself:**

```python
c.tier.TierName
```

```
Gold
```

`Tier` has one state, so its current row is not a guess about the past. The
attribute is named the same way whether the parent versions or not, so nothing
at the call site has to change.

## Eager loading

A pinned session adds one criterion per history class to every statement, which
on a catalog of a few hundred history tables costs a millisecond or two per
query. Against a Databricks round trip that is noise. In a lazy-load loop it is
paid once per load, so ask for what you need up front:

```python
from sqlalchemy.orm import selectinload

with binding.as_of("2025-03-01") as s:
    stmt = select(CustomerHistory).options(selectinload(CustomerHistory.region))
    rows = s.scalars(stmt).all()
```

`selectinload` and `joinedload` are both narrowed correctly.

## Writing

A pinned session executes selects and nothing else. An instant is a claim about
what was true, and a statement that writes would escape the pin rather than
honour it. Use `binding.session()` when you mean to change something.

That refusal sees the statements you pass to the session, which is not every
way to write. Two paths go around it:

**A flush of dirty objects.** The unit of work emits its SQL on the connection
rather than through the session's execute path, so editing an instance you
loaded and then calling `flush()` or `commit()` writes. Nothing flushes on its
own — a pinned session has `autoflush` off and does not commit on exit — so
this takes an explicit call:

```python
with binding.as_of("2025-03-01") as s:
    c = s.scalars(select(CustomerHistory)).first()
    c.CustomerName = "Acme"
    s.commit()          # writes
```

**Anything executed on the engine or on a connection**, including
`session.connection().execute(...)`.

A Databricks engine opened read-only raises `PermissionError` on any write
statement at the point the cursor executes it, which covers both paths. The
replica is an ordinary SQL Server database and has no such guard.
