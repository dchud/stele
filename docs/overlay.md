# Changing the output

Corrections belong in different places depending on what kind they are. Putting
one in the wrong place means losing it on the next regeneration.

| What you want to change | Where it belongs |
|---|---|
| which schemas and tables exist at all | `introspect` flags |
| how history tables are shaped | `introspect` flags |
| how much data profiling reads | `profile` flags |
| what counts as an accepted proposal | `infer --min-score` |
| keys, relationships, names, types | `overlay.yaml` |
| attribute naming style | `generate --snake-case` |
| schema names in the replica DDL | `ddl --schema` |

`model.yaml` and `models/` are outputs. Editing either one works until the next
run of the command that writes it, which is the worst possible failure mode.

## Scope, at introspect time

```bash
stele introspect --schemas dbo sales ops \
  --include '^(Customer|Order|Widget)' \
  --exclude '_staging$'
```

Both regexes match the bare table name. This is the only scope control in the
pipeline: `profile` and `infer` process whatever is in the spec.

## History shape, at introspect time

The four SCD2 flags — `--end-open`, `--end-sentinel`, `--interval`, and
`--current-not-in-history` — are described on
[introspect](pipeline/introspect.md#scd2-shape). They are recorded in the spec
and end up in the generated classes, so changing one means re-running
`introspect` and `generate`.

## The overlay

`overlay.yaml` is the one file in the pipeline you write by hand and the one
that survives everything. `infer` writes a starting point; after that it is
yours.

```yaml
tables:
  dbo.Widget:
    class_name: Widget
    primary_key: [WidgetId]
    comment: "the thing customers actually buy"

    columns:
      WidgetName:
        type_override: "NVARCHAR(120)"
      Notes:
        nullable: true

    foreign_keys_mode: replace
    foreign_keys:
      - columns: [OwnerId]
        referred_table: dbo.Owner
        referred_columns: [OwnerId]
```

### Table-level keys

| Key | Effect |
|---|---|
| `primary_key` | list of column names; marks the origin as manual |
| `class_name` | override the derived class name |
| `enabled` | set false to drop the table from generation |
| `comment` | becomes the class docstring |
| `history_table`, `history_of` | correct a pairing the suffix rule got wrong |

### Column-level corrections

| Key | Effect |
|---|---|
| `type_override` | a literal type expression, bypassing inference entirely |
| `nullable` | correct nullability the catalog reported wrong |
| `observed_max_length` | set a length without re-profiling |
| `comment` | becomes a comment above the column |

`type_override` is the escape hatch for anything load-bearing. Profiling gives a
lower bound on a string's width; if you know the real one, pin it here and
stop guessing.

### Foreign keys

`foreign_keys_mode` decides what happens to what is already in the spec:

- **`replace`** (the default) discards the inferred foreign keys for that table
  and uses exactly what you list. This is what `infer` writes, so an edited
  overlay is a complete statement rather than a patch.
- **`merge`** keeps the inferred ones and adds yours, skipping duplicates by
  columns and target.

Composite keys go here. `infer` only proposes single-column relationships, and
a table keyed on a pair is not a proposal target at all, so a two-column
reference is always hand-written:

```yaml
    foreign_keys:
      - columns: [RegionId, DistrictId]
        referred_table: dbo.District
        referred_columns: [RegionId, DistrictId]
```

That becomes one table-level constraint rather than one per column, because it
is a single claim about the pair:

```python
class Ledger(Base):
    __tablename__ = "Ledger"
    __table_args__ = (
        ForeignKeyConstraint(
            ["RegionId", "DistrictId"],
            [f"{SCHEMA_DBO}.District.RegionId",
             f"{SCHEMA_DBO}.District.DistrictId"],
        ),
        {"schema": SCHEMA_DBO},
    )
```

### Tables the catalog never reported

`add_tables` creates a table introspection did not see — a reference pointing
outside the mirrored subset, where you would rather have the class than a
dangling column. It takes the same keys as `tables`, and because there is no
introspected column to correct, it also declares the columns:

```yaml
add_tables:
  dbo.Region:
    primary_key: [RegionId]
    columns:
      RegionId:
        source_type: bigint
        nullable: false
      RegionName:
        source_type: string
      Rate:
        type_override: "Numeric(5, 2)"
```

A column needs to say what it holds, through `source_type` or `type_override`;
one that says neither is skipped with a warning, as is a table left with no
columns at all. Ordinals follow the order you write them in unless you set
them.

### History settings

```yaml
history:
  interval: closed
  end_open: sentinel
```

Anything under `history` overrides the spec-level SCD2 configuration, which is a
faster loop than re-running `introspect` with different flags.

## Checking an edit

```bash
stele generate --spec model.yaml --overlay overlay.yaml --out models
stele check --package models
```

`check` resolves every mapper and every relationship without a database. A
typo in `referred_table`, a column count that does not match, a relationship
pointing at a table you disabled — all surface here in under a second.

Unknown keys are warned about rather than ignored silently, so a misspelled
overlay key shows up in the `generate` output.
