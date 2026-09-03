# stele

Point stele at a Databricks catalog and it writes a SQLAlchemy ORM package that
runs unchanged against that catalog and against a SQL Server replica of the same
data.

It is built for catalogs that report little more than column names and a
flattened set of types: federated foreign tables with SCD2 `_history`
companions and no declared constraints. Keys, relationships and string widths
are inferred from names, checked against the data, and recorded in files you
can correct.

## The two files

```
stele introspect ──► model.yaml     what the catalog says
stele profile    ──► model.yaml     plus what the data shows
stele infer      ──► overlay.yaml   proposals and evidence, you edit this
stele generate   ──► models/        a Python package
stele ddl        ──► replica.sql    SQL Server CREATE TABLE
stele check                         imports the package, resolves all mappers
```

The split between `model.yaml` and `overlay.yaml` is the whole design.

`model.yaml` is a transcript of the catalog. It is regenerated on every
`introspect` and is never hand-edited, so an upstream change shows up as a diff
in a file nobody has touched. `overlay.yaml` is everything you know that the
catalog does not: which columns are keys, which columns point where, what a
class should be called, how wide a string really is. It is hand-edited and it
survives regeneration.

`models/` and `replica.sql` fall out of the two. Both are disposable.

## What to read

- **[Setting up](setup.md)** — install, connection settings, a first run.
- **[The pipeline](pipeline/index.md)** — one page per command.
- **[How it decides](heuristics.md)** — what the heuristics look for, and the
  score every rule produces. Read this before trusting or distrusting a
  proposal.
- **[Changing the output](overlay.md)** — where each kind of correction belongs.
- **[Gotchas](gotchas.md)** — the surprises worth knowing before a large run.
- **[Your own repository](repository.md)** — setting up a repository around
  the output, and keeping it current as the catalog changes.
- **[Using the package](generated/index.md)** — bindings, relationships,
  point-in-time queries.
- **[Prior art](prior-art.md)** — what the neighbouring tools and the
  literature do, and the published names for stele's mechanisms.

## Two decisions that shape everything else

**Generated classes carry a schema token, never a schema name.** A class is
mapped to `stele__dbo`, and the token resolves per engine through SQLAlchemy's
`schema_translate_map`. That is what lets one class hierarchy address
`my_catalog.dbo.Customer` on Databricks and `ReplicaDb.dbo.Customer` on SQL
Server with no conditionals in the model layer.

**Databricks opens read-only.** Delta has no session-scoped transaction in the
sense the ORM assumes, so a `Session` that flushes dirty objects against the
lakehouse does something you did not intend. Write statements raise
`PermissionError` unless you ask for a writable engine.
