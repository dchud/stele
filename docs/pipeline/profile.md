# profile

```bash
stele profile --spec model.yaml --sample 1000000
```

Recovers the type information federation threw away. Updates `model.yaml` in
place.

## Why it matters

Lakehouse Federation reports every character column as `STRING`, whether the
source declared `NVARCHAR(2)`, `CHAR(2)` or `NVARCHAR(MAX)`. Generate the
replica straight from that and every string column becomes `NVARCHAR(MAX)`,
which costs you three things at once:

- **Index eligibility.** SQL Server caps index keys at 900 bytes for clustered
  and 1700 for nonclustered. A `MAX` column cannot be a key column at all.
- **The row limit.** In-row data is capped at 8060 bytes, and a wide table of
  `MAX` columns will not create.
- **Cardinality estimates.** The optimiser sizes its guesses from declared
  widths, so a table of `MAX` columns produces bad plans.

## What it does

For every table with at least one character column, one query per batch of 40
columns:

```sql
SELECT COUNT(*) AS _total,
       MAX(LENGTH(col)) AS _len_0,
       SUM(CASE WHEN col IS NULL THEN 1 ELSE 0 END) AS _null_0,
       ...
FROM catalog.schema.table t
```

Columns are batched because a very wide table can hit expression-count limits.
Each column comes back with an observed maximum length and a null fraction,
recorded on the column in `model.yaml`.

`--sample N` wraps the source in `SELECT * FROM ... LIMIT N` first. On a large
table that is the difference between minutes and hours, at the cost of a
narrower observation. `--distinct` adds a `COUNT(DISTINCT ...)` per column,
which is considerably slower and rarely worth it.

A table whose profile query fails is logged and skipped rather than aborting the
run.

## The observed maximum is a lower bound

Nothing in the data tells you the declared width. A column of `NVARCHAR(50)`
holding nothing longer than 12 characters profiles as 12. Generating
`NVARCHAR(12)` from that would truncate the next insert.

So observed lengths are rounded **up** to a stable bucket:

```
10, 20, 50, 100, 200, 255, 500, 1000, 2000, 4000
```

Above 4000, the column becomes `NVARCHAR(MAX)`. Buckets are stable so that a
slightly different sample does not churn the generated DDL.

This is a guess that errs toward safety, not a fact. Pin anything load-bearing
with `type_override` in the overlay once you can confirm the real width — see
[Changing the output](../overlay.md#column-level-corrections).

## Warnings

After profiling, `stele profile` reports what will bite on the SQL Server side:

- string columns with no observed length and no override, which will become
  `NVARCHAR(MAX)`
- tables whose estimated in-row byte total approaches the 8060-byte limit

## Scope

`profile` has no schema filter. It profiles every enabled table in the spec.
Narrowing the spec at `introspect` time with `--include` and `--exclude` is the
only way to profile less.
