# stele

Point it at a Databricks catalog, get back a SQLAlchemy ORM package that works
unchanged against both that catalog and a SQL Server replica of the same data.

Built for the case where the tables are **federated foreign tables** with
**SCD2 `_history` companions** and **no declared constraints** — which means the
catalog can tell you almost nothing about the model's shape, and everything
interesting has to be inferred, verified, and then written down.

---

## The pipeline

```
stele introspect ──► model.yaml     regenerable, disposable, never hand-edited
stele profile    ──► model.yaml     adds observed string lengths
stele infer      ──► overlay.yaml   proposals + evidence, YOU edit this
stele generate   ──► models/        regenerable, never hand-edited
stele ddl        ──► replica.sql    SQL Server CREATE TABLE
```

The split between `model.yaml` and `overlay.yaml` is the whole design. Upstream
drift shows up as a diff in the first file; everything you know that the catalog
doesn't lives in the second and survives regeneration.

## Install

```bash
uv venv --python 3.14
uv pip install -e '.[all]'
```

Python 3.14 is fine. (The Databricks docs page claiming ≤3.11 is stale — the
actual wheel metadata is `>=3.8,<4.0`, `databricks-sql-connector` is pure
Python, and `pyarrow` has had cp314 wheels since 25.x.) Drop `[all]` to
`[databricks]` if you don't need the pyodbc side yet.

```bash
export DATABRICKS_SERVER_HOSTNAME=adb-1234567890.1.azuredatabricks.net
export DATABRICKS_HTTP_PATH=/sql/1.0/warehouses/abc123
export DATABRICKS_TOKEN=dapi...
```

## Walkthrough

### 1. Introspect

```bash
stele introspect --catalog my_federated_catalog --schemas dbo --out model.yaml
```

Reads `information_schema` in four queries rather than N per-table Inspector
round trips, and falls back to the Inspector if the catalog doesn't expose it.
Pairs `X` with `X_history` and reports any pair where the columns don't line up
the way you expect — that mismatch is where generated code would otherwise
produce quietly wrong results.

Expect `declared FKs 0`. That's not a failure; federation exposes no DDL
surface to hang informational constraints on.

Tune the SCD2 flags here if your history tables differ from the defaults:

```bash
stele introspect --catalog c --schemas dbo \
  --start-column StartDate --end-column EndDate \
  --end-open sentinel --end-sentinel 9999-12-31T00:00:00 \
  --interval half_open
```

### 2. Profile

```bash
stele profile --catalog my_federated_catalog --spec model.yaml --sample 1000000
```

Federation collapses `NVARCHAR(50)`, `CHAR(2)` and `VARCHAR(MAX)` all into
`STRING`. Without this step every string column becomes `NVARCHAR(MAX)` on the
replica, and you lose index eligibility (900/1700-byte key limits), blow the
8060-byte row limit, and hand the optimiser bad cardinality estimates.

Profiling gives an observed max, which is a *lower bound* on the true declared
width, so results are rounded up to stable buckets. Pin anything load-bearing
with `type_override` once you can confirm it.

### 3. Infer

```bash
stele infer --catalog my_federated_catalog --spec model.yaml --validate --out overlay.yaml
```

Name heuristics generate candidates; SQL turns them into evidence:

- **PK check** — total rows, null rows, and duplicate group count. A column that
  *looks* like a key but isn't unique in the mirror gets rejected outright.
- **FK check** — distinct child values vs. how many match the parent
  (containment), plus the child null fraction. Weak containment usually means
  the parent lives outside the mirrored subset.

Proposals above `--min-score` are written live; everything else is written
**commented out with its evidence**, so nothing is silently dropped and nothing
questionable is silently accepted. Review, uncomment, edit, commit.

### 4. Generate

```bash
stele generate --spec model.yaml --overlay overlay.yaml --out models
stele check --package models
```

`check` imports the package and runs `configure_mappers()` with no database
connection — a fast way to catch a broken relationship after editing the
overlay.

### 5. Replica DDL

```bash
stele ddl --package models --dialect mssql --out replica.sql
```

Because the models carry generic types with mssql variants, this emits real
`NVARCHAR(n)` / `DATETIME2(6)` DDL rather than the STRING-everywhere shape the
federated catalog reports.

---

## Using the generated models

```python
from models import Customer, CustomerHistory, LOGICAL_SCHEMAS, metadata
from stele.db import DatabricksConfig, databricks_engine, mssql_engine
from stele.runtime import Binding

lakehouse = Binding(
    engine=databricks_engine(DatabricksConfig.from_env(catalog="my_catalog"), readonly=True),
    schemas={"dbo": "dbo"},
    readonly=True,
)

replica = Binding(
    engine=mssql_engine("mssql+pyodbc://...?driver=ODBC+Driver+18+for+SQL+Server"),
    schemas={"dbo": "dbo"},
)

# Identical query, either backend.
from sqlalchemy import select
stmt = select(Customer).where(Customer.RegionId == 4)

lakehouse.scalars(stmt)
replica.scalars(stmt)
```

### Point-in-time queries

```python
import datetime as dt

CustomerHistory.as_of(dt.datetime(2026, 3, 1))       # everything as it stood then
CustomerHistory.current()                             # the live version of each row
CustomerHistory.changes_between("2026-01-01", "2026-04-01")
CustomerHistory.versions_of(some_customer)            # one entity's full timeline

customer.history          # relationship, ordered by StartDate, read-only
```

## Two design decisions worth knowing about

**Symbolic schemas.** Generated classes carry a token (`stele__dbo`), never a
literal schema name. The token resolves per-engine through SQLAlchemy's
`schema_translate_map`. That's what lets one class hierarchy address
`my_catalog.dbo.Customer` and `ReplicaDb.dbo.Customer` with no conditionals in
the model layer — and it's why you should resist the urge to hardcode a schema
into `__table_args__` when hand-editing.

**Databricks opens read-only by default.** Delta has no session-scoped
transaction in the sense the ORM assumes, so a `Session` that flushes dirty
objects against the lakehouse will do something you did not intend. Write
statements raise `PermissionError` unless you pass `readonly=False`.

## Known sharp edges

- **Composite-key FK targets** aren't proposed by `infer` — declare them in the
  overlay by hand.
- **Self-referencing FKs** are detected but not auto-proposed; they're real
  often enough to want, and wrong often enough to want confirmed.
- **Complex types** (`ARRAY`, `MAP`, `STRUCT`, `VARIANT`) map to `JSON` and are
  flagged as not round-tripping to SQL Server.
- **Tables with no discoverable key** still generate, with a mapper-level
  guessed key and a loud comment. ORM identity is unreliable until you fix it in
  the overlay.
- For real ETL, consider landing the federated tables into managed Delta first
  and pointing a second `Binding` at those — same classes, different schema map.
  Federated pushdown and ORM-generated SQL don't always cooperate.

## Renaming

The package name appears in generated imports (`from stele.runtime import ...`).
If you rename it, regenerate.
