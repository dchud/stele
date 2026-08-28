# Setting up

## Install

```bash
uv venv --python 3.14
uv pip install -e '.[all]'
```

Python 3.11 or newer. 3.14 is fine: the Databricks documentation page claiming
3.11 or lower is stale, the actual wheel metadata is `>=3.8,<4.0`,
`databricks-sql-connector` is pure Python, and `pyarrow` has had cp314 wheels
since 25.x.

The extras are optional and independent:

| Extra | Pulls in | Needed for |
|---|---|---|
| `databricks` | `databricks-sqlalchemy` | reading the catalog |
| `mssql` | `pyodbc` | talking to the replica |
| `all` | both | both |

Neither is needed to run `generate`, `ddl` or `check`, and neither is needed to
import `stele.runtime`. A generated package works with whichever driver the
machine happens to have.

## Connection settings

```bash
export DATABRICKS_SERVER_HOSTNAME=adb-1234567890.1.azuredatabricks.net
export DATABRICKS_HTTP_PATH=/sql/1.0/warehouses/abc123
export DATABRICKS_TOKEN=dapi...
export DATABRICKS_CATALOG=my_federated_catalog
```

The same four names work in a `.env` file at the top of the project, which the
CLI reads on every run. Two more are optional:

- `DATABRICKS_SCHEMA` sets the connection's default schema.
- `DATABRICKS_HOST` is read when `DATABRICKS_SERVER_HOSTNAME` is unset. It is
  the name `databricks configure` and the Databricks SDK write, and it usually
  carries a full URL, so the scheme and any trailing slash are stripped.

Every setting resolves the same way:

**the command-line flag, then an exported variable, then the `.env` file.**

Nothing in the file displaces a variable you exported. A `.env` is the better
place for a token than a shell profile, and it survives a second terminal.

If something is missing, the command exits with one line naming what it could
not resolve and all three places it looked.

## A first run

Start with one schema. Everything below assumes `DATABRICKS_CATALOG` is set, so
`--catalog` can be left off.

```bash
stele introspect --schemas dbo --out model.yaml
stele profile --spec model.yaml --sample 1000000
stele infer --spec model.yaml --validate --out overlay.yaml
```

Read `overlay.yaml`. Proposals above the confidence threshold are written live;
everything else is written commented out with the evidence that produced it.
Uncomment what you agree with, correct what you do not, then:

```bash
stele generate --spec model.yaml --overlay overlay.yaml --out models
stele check --package models
```

!!! warning "One schema is a rehearsal, not a result"

    Relationships are found by matching names across everything in the spec, so
    a reference from `dbo` into `sales` cannot be found unless both schemas were
    introspected into the same `model.yaml`. See
    [Gotchas](gotchas.md#every-schema-has-to-be-introspected-together).

## No Databricks connection to hand

`examples/demo_sqlite.py` builds a small spec by hand, runs inference and
generation over it, and exercises the result against in-memory SQLite. It is the
fastest way to see the shape of the output.

```bash
uv run python examples/demo_sqlite.py
```
