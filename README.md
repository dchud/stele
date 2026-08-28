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
stele check                         imports the package, resolves all mappers
```

The split between `model.yaml` and `overlay.yaml` is the whole design. Upstream
drift shows up as a diff in the first file; everything you know that the catalog
doesn't lives in the second and survives regeneration.

## Install

```bash
uv venv --python 3.14
uv pip install -e '.[all]'
```

Python 3.11 or newer. Drop `[all]` to `[databricks]` if you don't need the
pyodbc side yet, or install neither extra if you only want to generate code.

```bash
export DATABRICKS_SERVER_HOSTNAME=adb-1234567890.1.azuredatabricks.net
export DATABRICKS_HTTP_PATH=/sql/1.0/warehouses/abc123
export DATABRICKS_TOKEN=dapi...
export DATABRICKS_CATALOG=my_federated_catalog
```

The same names work in a `.env` file at the top of the project. A flag beats an
exported variable, which beats the file.

## A first run

```bash
stele introspect --schemas dbo --out model.yaml
stele profile --spec model.yaml --sample 1000000
stele infer --spec model.yaml --validate --out overlay.yaml
# read overlay.yaml, uncomment what you agree with, correct what you don't
stele generate --spec model.yaml --overlay overlay.yaml --out models
stele check --package models
```

One schema is a rehearsal. Relationships are found by matching names across
everything in the spec, so a reference from one schema into another is only
found if both were introspected into the same `model.yaml`.

No Databricks connection to hand? `uv run python examples/demo_sqlite.py` runs
the whole pipeline against an in-memory database.

## Documentation

**<https://dchud.github.io/stele/>**

The guide covers the pipeline command by command, what the heuristics look for
and the score every rule produces, where each kind of correction belongs, and
how to use the generated package — bindings, schema translation, relationships,
and point-in-time queries.

The sources are in `docs/`, and `uv run mkdocs serve` renders them at
<http://127.0.0.1:8000> while you edit.

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

## License

MIT. See [LICENSE](LICENSE).

## Contributing

```bash
uv sync --all-extras      # dependencies including the dev group
./check.sh                # everything CI runs
./check.sh --quick        # format, lint, prose, and workflows only
```
