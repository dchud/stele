# introspect

```bash
stele introspect --schemas dbo sales ops --out model.yaml
```

Reads the catalog and writes a transcript of it. Regenerate freely; the file is
never hand-edited.

## What it reads

Four queries against `<catalog>.information_schema`, regardless of how many
schemas you name, each filtered with `table_schema IN (...)`:

| Query | Table | For |
|---|---|---|
| tables | `tables` | names, table type, comments |
| columns | `columns` | names, ordinal, `full_data_type`, nullability, comments |
| constraints | `table_constraints` joined to `key_column_usage` | declared keys |
| referential | `referential_constraints` joined to `constraint_column_usage` | declared foreign keys |

Four queries rather than N per-table Inspector round trips is the reason a
hundred-table schema introspects in seconds. If the first query fails —
`information_schema` is not exposed, or the catalog is empty — it falls back to
the SQLAlchemy Inspector, which walks schema by schema and is slower.

Expect the last two to come back with nothing. Federation exposes no DDL surface
to hang informational constraints on, so `declared FKs 0` is the normal result
and not a failure.

## History pairing

Any table whose name ends in the history suffix is matched to a table of the
same name without it, **in the same schema**. The default suffix is `_history`.

```
dbo.Widget          ← primary
dbo.Widget_history  ← paired to it
```

The pairing is recorded both ways, and the history table's key is derived: the
primary table's key columns plus the interval start column. A history table with
no partner is reported as a warning and left alone.

`introspect` also reports any pair whose columns do not line up — a column in one
and not the other, or a missing interval column. That mismatch is where
generated code would otherwise produce quietly wrong answers, so it is worth
reading.

## SCD2 shape

Four decisions about history tables cannot be discovered and have to be
declared. Each one produces wrong answers rather than errors if guessed:

```bash
stele introspect --schemas dbo \
  --history-suffix _history \
  --start-column StartDate --end-column EndDate \
  --end-open sentinel --end-sentinel 9999-12-31T00:00:00 \
  --interval half_open \
  --current-not-in-history
```

| Flag | Question it answers |
|---|---|
| `--end-open` | is an open interval's end `NULL`, or a sentinel date? |
| `--end-sentinel` | if a sentinel, which one |
| `--interval` | is the interval `[start, end)` or `[start, end]`? |
| `--current-not-in-history` | is the live row absent from the history table? |

These are recorded in `model.yaml` and end up in each generated history class,
where the point-in-time helpers read them. Get `--interval` wrong and every
`as_of` query on a boundary date returns the wrong version, silently.

## What comes out

```
wrote model.yaml
  tables            214  (97 history)
  declared PKs      0
  declared FKs      0

  No FK constraints found. Expected for federated foreign catalogs -
  run `stele infer --validate` next to propose them from data.
```
