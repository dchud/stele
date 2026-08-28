# Gotchas

The surprises worth knowing before a large run.

## Every schema has to be introspected together

Relationships are found by matching names across everything in the spec. A table
that is not in `model.yaml` cannot be a target, so a reference from `sales` into
`dbo` is only found if both schemas were introspected into the same file.

`introspect` overwrites its output completely; there is no merge. Run it once
with every schema you care about:

```bash
stele introspect --schemas dbo sales ops finance --out model.yaml
```

Cost is not a reason to hesitate. The four `information_schema` queries filter
with `table_schema IN (...)`, so five schemas cost the same four queries as one.

## infer never reads the overlay

`infer` reads `model.yaml` alone. The overlay is applied by `generate` and by
nothing else.

The consequence is that a key you declare in the overlay does not feed back into
inference. It reaches the generated package, but the relationships that would
have been found once that table became a valid target are not proposed. For a
table whose key inference misses, the overlay has to carry the relationships as
well as the key.

## profile and infer have no scope filter

Both process every enabled table in the spec. `--schemas` on those two commands
only sets the connection's default schema; it does not narrow the work.

Scope is chosen once, at `introspect`, with `--schemas`, `--include` and
`--exclude`. On a catalog with hundreds of tables that choice is the difference
between a run measured in minutes and one measured in hours.

## infer will not overwrite your overlay

By design. Once you have edited `overlay.yaml`, a later `stele infer --out
overlay.yaml` stops with a message rather than replacing it. Write the new
proposals somewhere else and merge by hand:

```bash
stele infer --spec model.yaml --validate --out overlay-new.yaml
```

## Composite keys are never proposed

A table needs a single-column key to be a relationship target, and `infer` only
proposes single-column relationships. Composite keys and the references to them
are hand-written in the overlay, always.

## Self-references are found but not proposed

A column pointing at its own table's key is skipped. Parent-child hierarchies
are real often enough to want and wrong often enough to want confirmed, so they
are left for the overlay.

## A pinned session pins history tables only

A current table has no validity interval, so nothing can move it to another
moment. A query touching both a history table and a current table returns two
moments in one result:

```
history: Acme Ltd  region=North      as of March 2025
current: Northern                    as things stand
```

That is what the two tables are rather than a bug to route around, but it is
worth noticing when you mix them. `current()` raises inside a pinned session for
the same reason — see [History](generated/history.md#what-a-pinned-session-refuses).

## Traversing from a history class without pinning

`CustomerHistory.region` joins on the key alone and matches every version of the
parent, so outside a pinned session it returns whichever came first and
SQLAlchemy warns:

```
SAWarning: Multiple rows returned with uselist=False for lazily-loaded
attribute 'CustomerHistory.region'
```

The warning only fires where that parent happens to have more than one version,
so a fixture with single-version parents never sees it. Treat it as a help
rather than a guarantee: traversal from a history class belongs inside
`binding.as_of(...)`. `-W error::SAWarning` in a test suite turns the cases it
does catch into failures.

## Composite and self-referencing foreign keys

`infer` proposes neither. Composite keys and self-references are declared by
hand in the overlay, which is the documented route and the one with the fewest
guardrails — run `stele check` after either edit.

## Complex types do not round-trip

`ARRAY`, `MAP`, `STRUCT` and `VARIANT` map to `JSON` and are reported as lossy
by `generate`. They work against Databricks and they will not reproduce
faithfully in the replica.

## An unprofiled string column becomes NVARCHAR(MAX)

Skipping `profile` is fine if you only ever query Databricks. It is not fine if
you generate replica DDL: every character column becomes `NVARCHAR(MAX)`, which
costs index eligibility, risks the 8060-byte row limit, and gives the optimiser
bad estimates.

## Tables with no discoverable key still generate

With a mapper-level guessed key and a loud comment in the file. SQLAlchemy needs
something to build an identity map from. The class works for reading; the
identity map is unreliable until you set `primary_key` in the overlay.

## Federated pushdown and ORM SQL do not always cooperate

For anything resembling ETL, consider landing the federated tables into managed
Delta first and pointing a second `Binding` at those. Same classes, different
schema map, considerably more predictable plans.
