# Query results as dataframes

`Binding.scalars` returns mapped instances and `Binding.rows` returns `Row`
objects. When you want a dataframe, neither is what you are after: the ORM
builds objects you are about to discard.

Write the query against the generated classes, then hand it to the library
that will do the fetching. There are two handles for that.

| Handle | What it carries | For |
|---|---|---|
| `binding.engine` | the schema map, as an execution option | anything that executes through SQLAlchemy |
| `binding.compile(stmt)` | the SQL, with real schema names in it | anything that does not |

Which one a library needs depends on whether it drives SQLAlchemy or opens
its own connection. Both appear below.

## pandas

`read_sql` accepts a statement and a connectable, so the engine does the
work and the schema token resolves the ordinary way:

```python
import pandas as pd
from sqlalchemy import select

from acme_models import Widget

stmt = select(Widget.WidgetId, Widget.WidgetName).where(Widget.OwnerId == 1)

df = pd.read_sql(stmt, lakehouse.engine)
```

`dtype_backend="pyarrow"` gives Arrow-backed columns:

```python
df = pd.read_sql(stmt, lakehouse.engine, dtype_backend="pyarrow")
# WidgetId  -> int64[pyarrow]
# WidgetName -> string[pyarrow]
```

pandas reads Arrow off the wire only through ADBC. With a SQLAlchemy
connectable it fetches rows and converts, on either backend.

## polars

Against the replica, `read_database` takes the statement and the engine and
executes it through SQLAlchemy:

```python
import polars as pl

df = pl.read_database(stmt, replica.engine)
```

Against Databricks it needs the compiled string instead. polars recognises a
Databricks engine, takes its raw cursor and passes the query straight to
`cursor.execute`, so nothing compiles a statement object on the way and the
schema token would reach the connector unresolved:

```python
sql = str(lakehouse.compile(stmt, literal_binds=True))
df = pl.read_database(sql, lakehouse.engine)
```

`literal_binds=True` keeps it to one self-contained string. To send
parameters separately instead, `binding.compile(stmt)` leaves them in
`compiled.params`, and polars takes execution options to pass them through —
check the spelling against the polars version you have.

That path is Arrow end to end: polars fetches through the connector's own
Arrow methods rather than building rows.

## ibis

ibis has no SQLAlchemy entry point. Open its own connection from the same
settings, then give it the compiled SQL:

```python
import ibis

from stele.db import DatabricksConfig

cfg = DatabricksConfig.from_env(catalog="mycat")
con = ibis.databricks.connect(
    server_hostname=cfg.host,
    http_path=cfg.http_path,
    access_token=cfg.token,
    catalog=cfg.catalog,
)

t = con.sql(str(lakehouse.compile(stmt, literal_binds=True)))
```

`literal_binds=True` because `con.sql` takes a statement and nothing
alongside it.

If the query is simple enough to express in ibis directly, `con.table(name,
database=(catalog, schema))` needs nothing from stele at all.

## Spark

`spark.sql` takes a string. The compiled SQL names a schema and a table but
not a catalog, which lives in the connection, so set the current catalog
first:

```python
spark.catalog.setCurrentCatalog(cfg.catalog)
sdf = spark.sql(str(lakehouse.compile(stmt, literal_binds=True)))
```

From Spark 4.0, `createDataFrame` accepts a `pyarrow.Table` and
`DataFrame.toArrow()` goes the other way, so a polars or pandas result can
cross over without a round trip through Python objects.

## DuckDB, ADBC, arrow-odbc

The same hand-off: a compiled string, and a connection you opened yourself.
stele has nothing to do with the connection.

```python
con.execute(str(lakehouse.compile(stmt, literal_binds=True)))
```

## Point-in-time queries carry their own instant

A compiled statement has no session, so pinning one does not narrow it. Put
the instant in the statement:

```python
sql = str(lakehouse.compile(WidgetHistory.as_of(when), literal_binds=True))
```

The interval predicate is rendered into the SQL, so the library running it
reads the right version without reimplementing the comparison. Using
`binding.as_of(when)` around a compile does nothing — that pins a session,
and no session is involved.

## A schema map value is a bare schema name

On Databricks the catalog belongs to the connection, so map a logical schema
to a schema and nothing more. A dotted value does not mean the same thing on
the two backends:

```python
Binding(engine=..., schemas={"dbo": "ReplicaDb.dbo"})
```

```sql
-- mssql splits it
FROM [ReplicaDb].dbo.[Widget]
-- Databricks quotes it as one identifier
FROM `ReplicaDb.dbo`.`Widget`
```

This is true of `Binding` generally, not only of compiled output.

## If you want Arrow

Use the library's own path. polars fetches Arrow from the Databricks
connector when you hand it a string, as above, and pandas has
`dtype_backend="pyarrow"`.

There is no `binding.arrow()`. Arrow from the connector bypasses
SQLAlchemy's result processors, so the types would be the driver's rather
than the `Mapped[...]` types in the generated classes, and pyodbc offers no
Arrow path at all — one method would return a different schema depending on
which binding you called it on.

## How these were checked

The pandas recipe was run against SQLite through a `Binding`, including the
pyarrow dtype backend. The schema rendering above is real output from the
mssql and Databricks dialects.

polars, ibis, Spark and DuckDB are not installed here, so those recipes were
not executed. ibis's connection parameters and `.sql()` come from its own
backend documentation. polars unwrapping a Databricks engine to the raw
cursor, and fetching Arrow from it, comes from reading its database reader;
the exact keyword for passing parameters separately is worth confirming
against the version you install.
