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

For every table with a character or integer column, one query per batch of 40
columns:

```sql
SELECT COUNT(*) AS _total,
       MAX(LENGTH(Name)) AS _len_0,
       SUM(CASE WHEN Name IS NULL THEN 1 ELSE 0 END) AS _null_0,
       MIN(OwnerId) AS _min_1,
       MAX(OwnerId) AS _max_1,
       ...
FROM schema.table
```

Each column is asked what its type can answer. A character column gives an
observed maximum length and a null fraction, which is what picks its width on
the replica. An integer column gives its observed value range, which is what
`infer --discover` rules candidate references out with. A column that is
neither is not asked about, so a table of timestamps costs no query at all.

Ranges stop at integers on purpose. A string range is a pair of values of
unbounded length written into `model.yaml` verbatim, and lexicographic order
rarely rules a reference out to earn that; a decimal does not round-trip
through YAML as itself, and a rounded bound is not a bound. String and decimal
key candidates are narrowed by `--distinct` instead.

Columns are batched because a very wide table can hit expression-count limits.

`--sample N` wraps the source in `SELECT * FROM ... LIMIT N` first. On a large
table that is the difference between minutes and hours, at the cost of a
narrower observation. `--distinct` adds a `COUNT(DISTINCT ...)` per column,
which is considerably slower. It is worth it before `infer --discover`, where a
distinct count rules out more candidate references than a range does.

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
