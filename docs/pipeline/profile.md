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

## A long run, and what survives an interruption

One statement per table, each a full aggregate scan, so a catalog of hundreds
of tables takes a while. `profile` reports each table as it finishes:

```
[ 47/312] dbo.OrderLine                3.2s   elapsed 4m12s   eta 18m
```

The per-table duration is the number to watch: it says where the time goes,
and whether `--sample` would change the picture. Output goes to stderr, so
the command's own result stays on stdout. On a terminal the line rewrites
itself; where stderr is redirected, each table gets a line of its own.

The spec is written every ten tables, not only at the end, so an interrupted
run leaves behind what it managed. `--resume` picks it up:

```bash
stele profile --spec model.yaml --distinct
# interrupted
stele profile --spec model.yaml --distinct --resume
```

A table counts as done when it carries a row count, which `profile` assigns
only after every one of its batches has landed — a table interrupted midway
does not carry one and is read again from the top.

What counts as done depends on what you asked for. A spec profiled without
`--distinct` has lengths and null rates but no distinct counts, so
`--distinct --resume` reads those tables again rather than reporting success
over observations it never made. `--resume` is opt-in for the same reason:
without it, every table is read, and a deliberate re-profile is never
silently skipped.

## When the connection drops

A failed statement is retried three times with a widening delay.
Underneath, `databricks-sql-connector` retries each request itself, and
stele sets that to three attempts rather than the connector's own default
of thirty across fifteen minutes — thirty suits a warehouse that is busy,
where waiting is the right answer, not one that is failing.

The two layers multiply: a statement that never succeeds is attempted up
to nine times before its table is given up. They cover different failures,
which is why both are there — the connector retries an HTTP request, stele
retries the whole statement including its connection.

A table that fails every attempt keeps whatever it had, is named in the
summary, and the run continues. Three tables failing in a row stops the run
instead: one table can fail for reasons of its own, but three in a row is the
warehouse, the network or the credential, and the remaining hundreds would
fail the same way. Either way the spec on disk holds the work already done,
and `--resume` continues from it.

## Scope

`profile` has no schema filter. It profiles every enabled table in the spec.
Narrowing the spec at `introspect` time with `--include` and `--exclude` is the
only way to profile less.
