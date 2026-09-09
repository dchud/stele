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

A primary key proposal becomes one query, a foreign key proposal two.

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

## Composite keys

A child carrying **every** column of a parent's composite key, under the
parent's own names, is proposed at 0.70 — below a single-column name match,
above the threshold, so it is written live. One column matching such a key is
not proposed at all: a key name says which table it belongs to, not how many
columns it spans.

A composite key no child carries that way is named in the output, so its
absence from the proposals is something you read rather than something you
notice:

```
1 table(s) have composite keys no child carries by name:
    dbo.District (RegionId, DistrictId)
    -> declare those references in the overlay
```

See [Changing the output](../overlay.md#foreign-keys).

## `--discover`

Four shapes produce nothing from names: an opaquely named column, a table's own
key standing in for a reference, a composite key, and a self-reference. The
composite is reached above. The other three are reached by `--discover`, which
rules candidate pairs out with statistics `profile` already recorded and
proposes whatever survives.

```bash
stele profile --spec model.yaml --distinct
stele infer --spec model.yaml --validate --discover
```

It needs those statistics and says so when the spec carries none. `--distinct`
is worth its time here: a distinct count rules out far more pairs than a range
does, and it is the better of the two for ranking what is left.

A discovery reaches the overlay commented out, marked `DISCOVERED` rather than
`REJECTED`, carrying the numbers behind it:

```
# DISCOVERED score=0.49 containment=0.998 :: no name evidence; range [3, 498] inside [1, 512]; 430 distinct of 512
#   - columns: ["Custodian"]
#     referred_table: dbo.Owner
#     referred_columns: ["OwnerId"]
```

Nothing argued against it; it has no name behind it, which is a judgement a
person makes rather than a threshold.

The run says what it weighed:

```
discovery: 12 pair(s) the statistics could test, 8 not ruled out, 2 proposed
    6 more survived and were left unchecked; raise --max-discoveries to see them
```

Those counts are the answer to whether pruning is selective enough on a given
catalog. Every proposal that goes on to be checked costs warehouse queries,
which is what `--max-discoveries` caps. Survivors are ranked by how much of the
parent's key space the child covers, so the cap keeps the pairs most likely to
be references — see [How it decides](../heuristics.md#candidates-from-statistics).

## Flags worth knowing

| Flag | Effect |
|---|---|
| `--validate` | check proposals against the data; needs a connection |
| `--min-score` | the live/commented-out threshold, default 0.6 |
| `--sample N` | cap distinct values scanned per foreign key check |
| `--discover` | also propose references no name reveals, from profiled statistics |
| `--max-discoveries N` | how many discovered candidates to check, default 50 |
| `--force` | overwrite an existing overlay |
| `--apply` | write accepted proposals into `model.yaml` instead of an overlay |

`--apply` skips human review and writes into the file that gets regenerated.
Prefer the overlay.
