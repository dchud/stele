# How it fits together

Six commands. Three of them talk to Databricks; three work entirely offline.

| Command | Reads | Writes | Needs a connection |
|---|---|---|---|
| `introspect` | the catalog | `model.yaml` | yes |
| `profile` | the catalog, `model.yaml` | `model.yaml` | yes |
| `infer` | `model.yaml`, `overlay.yaml` with `--overlay` | `overlay.yaml` | only with `--validate` |
| `generate` | `model.yaml`, `overlay.yaml` | `models/` | no |
| `ddl` | `models/` | `replica.sql` | no |
| `check` | `models/` | nothing | no |

To set up a repository around these files, see
[Your own repository](../repository.md).

## What is regenerable

`model.yaml`, `models/` and `replica.sql` are outputs. Delete any of them and
run the command again. Nothing you care about should live in them, and
`generate` overwrites the package without asking.

`overlay.yaml` is an input you write. `introspect` never touches it, and `infer`
refuses to overwrite one that exists unless you pass `--force`.

## The order that matters

`introspect` before everything. `profile` before `generate` if the replica DDL
matters, because an unprofiled string column becomes `NVARCHAR(MAX)`.

`infer` reads `model.yaml`. Pass `--overlay` and it applies that first, so keys
already declared become targets for relationship proposals, and with
`--validate` the references the overlay declares are checked against the data —
see [Gotchas](../gotchas.md#infer-reads-the-overlay-only-when-asked).

`generate` is the only command that applies the overlay.

## Scope

`--schemas` on `introspect` takes a list and is the only place scope is chosen.
Two regex filters narrow it further:

```bash
stele introspect --schemas dbo sales ops \
  --include '^(Customer|Order|Widget)' \
  --exclude '_staging$'
```

Both match against the bare table name. `--include` keeps only what matches;
`--exclude` drops what matches. Filtering here rather than later is what keeps
`profile` and `infer` cheap, because neither of them has a scope filter of its
own.
