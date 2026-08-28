# How it decides

A federated catalog declares no keys and no relationships, so stele guesses from
names and then checks the guesses against the data. This page is the whole rule
set and the score each rule produces. Read it before trusting or distrusting a
proposal.

Every score is a number in the overlay next to the proposal that earned it.
Nothing here is hidden.

## Which columns can be keys at all

A column is a key candidate only if its source type starts with one of:

```
int  integer  bigint  smallint  long  short  string  varchar  char  decimal
```

Floats are excluded because equality on them is a bad basis for identity.
Complex types — `ARRAY`, `MAP`, `STRUCT`, `VARIANT` — are excluded because they
cannot be one.

## Key names

Both key-naming conventions occur, sometimes in different schemas of the same
catalog, so a name is built by putting an affix on **either side** of the table
name:

```
id   key   code   no   num   sk
```

For a table `Widget` that gives `WidgetId`, `IdWidget`, `WidgetKey`, `KeyWidget`
and so on, all compared case-insensitively with separators removed. `WIDGET_ID`,
`widget_id`, `WidgetId` and `Widget Key` are the same name to the matcher, and
`id_widget_type` matches a table called `widget_type`.

A naive singular of the table name is tried too — `ies` to `y`, `ses` to `s`,
otherwise a trailing `s` is dropped — so `Categories` yields `CategoryId` and
`Addresses` yields `AddressId`. Both the singular and the original are used, so
a table called `Status` still matches `StatusId`.

## Primary keys

Every table without a key gets one proposal: the highest-scoring candidate.
History tables are skipped, because their key follows the primary table's.

The rules are tried in order and the first that matches wins:

| Rule | Example | Score |
|---|---|---|
| the column is a key name for this table | `Widget.WidgetId`, `Widget.IdWidget` | 0.90 |
| a bare generic name in one of the first three columns | `Widget.Id`, `Widget.RowId`, `Widget.Guid` | 0.75 |
| ends in `id`, and the stem starts the table name | `Boxes.BoxId` | 0.70 |
| starts with `id`, and the rest starts the table name | `Boxes.IdBox` | 0.70 |

The last two exist because the singulariser is naive. `Boxes` reduces to `Boxe`,
so no affix form of it spells `BoxId` — but `Boxes` does start with `Box`. They
catch the `-ches`, `-shes` and `-xes` plurals the three singular rules miss.

Two bonuses, applied to whichever rule matched, capped at 0.99:

- **+0.05** if the column is not nullable
- **+0.05** if it is the first column in the table

So the ordinary case — a non-nullable `WidgetId` in position one of `Widget` —
scores 0.99 before any data is looked at.

### What the data adds

With `--validate`, each proposal gets total rows, null rows, and a count of
duplicate groups.

- **Unique and non-null** adds 0.15, capped at 0.99, and appends
  `unique and non-null in data` to the evidence.
- **Anything else** drops the score to **0.10** and marks it `REJECTED` with the
  duplicate and null counts. A column that looks like a key but is not one in
  the mirror is not marked down, it is thrown out.

## Foreign keys

Every column of every table is matched against an index of key names built from
every other table in the spec. A table enters that index only if it has exactly
one key column; composite keys are left for the overlay.

A table contributes two kinds of name: the key names built from its table name,
and the actual name of its key column. That second one is why declaring an
unusual key in the overlay makes references to it findable.

Columns that are part of their own table's key are skipped, and so are
self-references — real often enough to want, wrong often enough to want
confirmed by hand.

| Situation | Score |
|---|---|
| name matches and the types agree | 0.80 |
| name matches, types differ | 0.40 |

### When several tables claim a name

A key name says nothing about which schema it belongs to, and the same table
name in two schemas generates the same names. So the index maps a name to
**every** table that claims it, and the match resolves in two steps:

**A parent in the child's own schema settles it.** Relationships almost always
stay inside a schema, so this keeps its full score, and the passed-over twin is
named in the evidence:

```
0.80  dbo.Order(CustomerId) -> dbo.Customer
        name matches Customer key, types agree (same schema preferred over ops.Customer)
```

**With no parent there, the name is ambiguous.** Every candidate is proposed at
**half score**, each naming its rivals. That puts them below the default
threshold, so they arrive in the overlay commented out and the choice is yours:

```
0.40  sales.Invoice(CustomerId) -> dbo.Customer
        name matches Customer key, types agree (ambiguous with ops.Customer)
0.40  sales.Invoice(CustomerId) -> ops.Customer
        name matches Customer key, types agree (ambiguous with dbo.Customer)
```

A unique name still crosses schemas freely. If only `dbo` has a `Widget`, then
`sales.Invoice.WidgetId` resolves to it at 0.80.

If two accepted proposals still claim the same column — which needs a threshold
below the halved score — neither is written and a warning names both. Two
parents for one column is a question, not a relationship.

### What the data adds

With `--validate`, containment is the fraction of distinct non-null child values
that exist in the parent:

| Containment | Effect on the score |
|---|---|
| 0.999 or better | add 0.19, capped at 0.99 |
| 0.95 to 0.999 | add 0.05, capped at 0.85, evidence notes `some orphans` |
| below 0.95 | multiply by the containment, evidence notes `WEAK` |
| child column entirely null | drop to 0.20 |

Weak containment usually means the parent lives outside the mirrored subset
rather than that the relationship is wrong. The child's null fraction is
recorded alongside it, and `--sample N` caps the distinct child values scanned.

## String lengths

Profiling records an observed maximum length per character column, which is a
lower bound on the declared width and never the width itself. Lengths are
rounded up to stable buckets:

```
10  20  50  100  200  255  500  1000  2000  4000
```

Above 4000 the column becomes `NVARCHAR(MAX)`. Buckets are stable so a different
sample does not churn the generated DDL, and rounding up rather than down means
the guess errs toward accepting data rather than truncating it.

## The threshold

`--min-score` decides what is written live and what is written commented out. It
defaults to 0.6, which sits above a type-mismatched or ambiguous foreign key
(0.40) and below a plain name match (0.80).

| Threshold | What comes through |
|---|---|
| 0.9 | validated keys and validated relationships only |
| 0.6 | plain name matches too, the default |
| 0.4 | type mismatches and ambiguous names as well |
| 0.1 | everything, including data-rejected keys |

Lowering it is a reading tool. A real relationship whose columns have mismatched
types is invisible at 0.6 and obvious at 0.4.
