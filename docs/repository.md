# Using the output in your own repository

The pipeline leaves four files. This page sets up a repository around them
that other people can clone, that stays current as the catalog changes, and
that keeps the generated code generated.

You need `stele introspect` to have run at least once, so `model.yaml`
exists.

## What you have

| File | Where it comes from | Do you edit it |
|---|---|---|
| `model.yaml` | `introspect`, extended by `profile` | no |
| `overlay.yaml` | you, starting from what `infer` proposes | yes |
| the generated package | `generate` | no |
| `replica.sql` | `ddl` | no |

Commit all four. Only `overlay.yaml` is written by hand; the rest are
rebuilt from it and from `model.yaml`, and committing them means a
contributor can clone the repository and import the models without any
Databricks credentials.

## 1. Lay out the repository

```
pyproject.toml          depends on stele, plus a driver extra
model.yaml
overlay.yaml
replica.sql
src/acme_models/        the generated package
src/acme/               your code
tests/
Makefile
.env                    ignored
```

Keep `model.yaml` and `overlay.yaml` at the top level, where `stele` looks
for them by default — then no command needs `--spec` or `--overlay`. A
`catalog/` subdirectory is fine too if you pass the paths in step 2.

Name the generated package whatever you like. `stele generate --out` decides
it, and the directory name becomes the import name, so use something that is
a valid Python identifier.

Put your own code in a separate package beside it, not inside it. `generate`
deletes files it wrote that a later run does not, and the package header
says not to edit anything in that tree.

Add `stele` to your dependencies. The generated package imports
`stele.runtime` at run time, so it is a real dependency and not just a build
tool.

## 2. Put the commands in one recipe

```make
regen:
	stele generate --spec model.yaml --overlay overlay.yaml --out src/acme_models
	stele ddl --package src/acme_models --schema dbo=dbo --out replica.sql
	stele check --package src/acme_models
```

Run `make regen` after every overlay edit. Having the flags in one place
means your CI files stay free of them, and there is a single thing to update
when the pipeline changes.

## 3. Check the committed output on every push

This job needs no credentials, so it can run on every pull request:

```yaml
- run: make regen
- run: |
    git status --porcelain
    test -z "$(git status --porcelain)"
```

If someone edits the generated package by hand, or changes `overlay.yaml`
without regenerating, the tree comes back dirty and the job fails. Printing
the status first shows which files.

Use `git status --porcelain` rather than `git diff --exit-code`. A table
added upstream produces a brand new module, and `git diff` does not see
untracked files.

This job says nothing about `overlay.yaml` itself. That file is an input, so
there is nothing to compare it against.

## 4. Refresh from the catalog on a schedule

This is the only job needing credentials:

```yaml
on:
  schedule: [{cron: "0 6 * * 1"}]
  workflow_dispatch:
```

Have it run `stele introspect`, then `make regen`, and open a pull request
if anything changed. Run `stele profile` less often — weekly introspection
and monthly profiling is a reasonable starting point.

Two things to leave out of it. Do not run `infer --force`, which overwrites
the overlay you reviewed. And do not merge the pull request automatically: a
new table arrives with no key and no relationships until someone runs `stele
infer` and accepts what it proposes.

Read the diff in `model.yaml` to see what changed in the catalog, and the
diff in the generated package to see what it did to your models.

## Adding your own code

Query helpers, subclasses and services go in your own package:

```python
from acme_models import Customer, Order
from stele.runtime import Binding

def orders_for(binding: Binding, region: int) -> list[Order]:
    return binding.scalars(select(Order).where(Order.RegionId == region))
```

Corrections to the *model* — a key, a relationship, a type, a name, a column
description — go in `overlay.yaml` and take effect on the next `make regen`.
Anything you write into the generated package is deleted the next time it
runs.

## Expect these

**A stele upgrade changes the generated package.** Templates change between
versions, so a dependency bump produces a diff in `src/acme_models/` with no
catalog change behind it. Run `make regen` as part of the upgrade and commit
the result alongside it.

**`profile --sample N` reads an unordered sample.** It is a `LIMIT` without
an `ORDER BY`, so two runs can see different rows. Observed lengths round up
to buckets, so this usually changes nothing — but a value crossing a bucket
boundary widens a column for real.

**The scheduled job needs a personal access token.** The connection settings
accept a token and nothing else, so that is what goes in the secret.
