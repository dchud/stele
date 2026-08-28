# What the types say

The generated package is fully annotated and ships a `py.typed` marker, so a
type checker sees real signatures rather than `Any`. Most of that is ordinary
SQLAlchemy. Two things are worth knowing.

## The element type survives

```python
customers = lakehouse.scalars(select(Customer))
# list[Customer]

names = lakehouse.scalars(select(Customer.CustomerName))
# list[str | None]

pairs = lakehouse.rows(select(Customer.CustomerId, Customer.CustomerName))
# Sequence[Row[tuple[int, str | None]]]
```

`Binding.scalars` takes a select of one thing and returns a list of that thing.
`Binding.rows` takes a select of any width and keeps the row's shape.

The point-in-time helpers carry it too, so the two compose:

```python
versions = lakehouse.scalars(CustomerHistory.as_of("2026-03-01"))
# list[CustomerHistory]

for v in versions:
    v.CustomerName    # checked
    v.CustmoerName    # caught
```

`as_of`, `changes_between`, `versions_of` and `timeline` all name what they
return.

## The two that stay open

**`current()`** returns `Select[tuple[Any]]`. It selects the primary table when
the live row is not duplicated into history, so which class its rows belong to
is a property of the model rather than of the call. Naming one of them would be
wrong for half the configurations. Annotate the result yourself where it
matters:

```python
rows: list[Customer] = lakehouse.scalars(CustomerHistory.current())
```

**`as_of_all()`** returns `dict[str, Select[tuple[Any]]]`. It holds a different
element type under every key. A type variable over its argument would name the
element type for a list of one class and reject the mixed list the function
exists to serve.

**`versions_of(entity)`** takes `Any` for its argument, because the business key
is read off whatever you pass by attribute name. Narrowing it would reject
callers that work.

## Why `scalars` rejects a multi-column select

```python
lakehouse.scalars(select(Customer.CustomerId, Customer.CustomerName))
#                 ^ a type error
```

`Session.scalars` would accept that and silently return only the first column.
The signature here takes a one-column select, so the mistake is caught rather
than discovered in the output. Use `rows` when you want several columns.
