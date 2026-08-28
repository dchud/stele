# infer

```bash
stele infer --spec model.yaml --validate --out overlay.yaml
```

Proposes the keys and relationships the catalog never declared, and writes them
as an overlay you edit.

Name heuristics generate the candidates; SQL turns them into evidence. The rules
and the score each one produces are on [How it decides](../heuristics.md). This
page is about running it.

## Order of operations

1. Primary keys are proposed for every table without one.
2. With `--validate`, each is checked against the data.
3. Accepted keys are applied **in memory**, so foreign key matching has targets.
4. Foreign keys are proposed.
5. With `--validate`, each is checked against the data.

Step 3 is why a table with no discoverable key also gets no relationships
pointing at it. A parent needs a single-column key before anything can reference
it.

## Without a connection

Leave off `--validate` and `infer` touches no database at all. It reads
`model.yaml`, applies the name heuristics, and prints what it found. On a large
catalog this is the cheap first pass: read the proposals, fix the spec's scope,
then spend the query budget once.

```bash
stele infer --spec model.yaml --min-score 0.4
```

Lowering `--min-score` below the default of 0.6 surfaces the proposals that were
made and rejected. A real relationship whose columns have mismatched types
scores 0.4 and is otherwise invisible.

## With `--validate`

Every proposal becomes two queries.

**Primary keys** get total rows, null rows, and duplicate group count:

```sql
SELECT (SELECT COUNT(*) FROM t) AS total_rows,
       (SELECT COUNT(*) FROM t WHERE k IS NULL) AS null_rows,
       (SELECT COUNT(*) FROM (
          SELECT k FROM t GROUP BY k HAVING COUNT(*) > 1) d) AS duplicate_groups
```

A column that looks like a key but is not unique in the mirror is rejected
outright, not merely marked down.

**Foreign keys** get containment — how many distinct child values exist in the
parent — and the child's null fraction:

```sql
WITH c AS (SELECT DISTINCT child_col FROM child WHERE child_col IS NOT NULL),
     p AS (SELECT DISTINCT parent_col FROM parent)
SELECT (SELECT COUNT(*) FROM c) AS distinct_values,
       (SELECT COUNT(*) FROM c JOIN p ON c.child_col = p.parent_col) AS matched
```

`--sample N` caps the distinct child values scanned. A validation query that
fails is logged and the proposal keeps its name-only score.

## What it writes

Proposals at or above `--min-score` are written live. Everything else is written
commented out **with its evidence**, so nothing is silently dropped and nothing
questionable is silently accepted:

```yaml
tables:
  dbo.Order:
    # score=0.99 rows=182034 dups=0 nulls=0 :: name matches Order plus a key affix; unique and non-null in data
    primary_key: [OrderId]
    foreign_keys_mode: replace
    foreign_keys:
      # score=0.99 containment=1.000 :: name matches Customer key, types agree; containment 1.000
      - columns: [CustomerId]
        referred_table: dbo.Customer
        referred_columns: [CustomerId]
        origin: inferred
        confidence: 0.99
    # REJECTED score=0.40 containment=n/a :: name match but type differs (string vs bigint)
    #   - columns: [RegionCode]
    #     referred_table: dbo.Region
    #     referred_columns: [RegionId]
```

Read it, uncomment what you agree with, correct what you do not, and commit it.
That file is the one artifact in the pipeline worth keeping.

## What it declines to propose

A table whose key spans several columns is not a proposal target, so no
reference to it is ever suggested. `infer` names those tables rather than
leaving you to notice an absence:

```
2 table(s) have composite keys; references to them are not proposed:
    dbo.District (RegionId, DistrictId)
    dbo.OrderLine (OrderId, LineNo)
    -> declare those references in the overlay
```

Matching a pair of columns by name across a catalog goes wrong far more often
than matching one, which is why those references are yours to write. See
[Changing the output](../overlay.md#foreign-keys).

## Flags worth knowing

| Flag | Effect |
|---|---|
| `--validate` | check proposals against the data; needs a connection |
| `--min-score` | the live/commented-out threshold, default 0.6 |
| `--sample N` | cap distinct values scanned per foreign key check |
| `--force` | overwrite an existing overlay |
| `--apply` | write accepted proposals into `model.yaml` instead of an overlay |

`--apply` skips human review and writes into the file that gets regenerated.
Prefer the overlay.
