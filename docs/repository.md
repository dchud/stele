# Using the output in your own repository

The pipeline leaves five outputs, and tbls renders a sixth. This page sets
up a repository around them that other people can clone, that stays current
as the catalog changes, and that keeps the generated code generated.

You need `stele introspect` to have run at least once, so `model.yaml`
exists.

## What you have

| File | Where it comes from | Do you edit it |
|---|---|---|
| `model.yaml` | `introspect`, extended by `profile` | no |
| `overlay.yaml` | you, starting from what `infer` proposes | yes |
| the generated package | `generate` | no |
| `replica.sql` | `ddl` | no |
| `dictionary.json` | `dictionary` | no |
| `dbdoc/` | `tbls doc`, from `dictionary.json` | no |

Commit all of them. Only `overlay.yaml` is written by hand; the rest are
rebuilt from it and from `model.yaml`, and committing them means a
contributor can clone the repository, import the models and read the data
dictionary without any Databricks credentials. The dictionary is the one
whose readers are mostly people who never run the pipeline, so a rendered
tree they can browse in the repository is most of its value.

stele's own repository ignores `dictionary.json`. That is the opposite
advice for the opposite reason: there the file would describe one customer's
catalog rather than anything about the tool. Here it describes your model,
so it belongs in git.

## 1. Lay out the repository

```
pyproject.toml          depends on stele, plus a driver extra
model.yaml
overlay.yaml
replica.sql
dictionary.json
dbdoc/                  rendered from dictionary.json by tbls
src/acme_models/        the generated package
src/acme/               your code
tests/
Makefile                or justfile
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

=== "Makefile"

    ```make
    regen:
    	stele generate --spec model.yaml --overlay overlay.yaml --out src/acme_models
    	stele ddl --package src/acme_models --schema dbo=dbo --out replica.sql
    	stele check --package src/acme_models
    	stele dictionary --spec model.yaml --overlay overlay.yaml --out dictionary.json
    	tbls doc --rm-dist json://dictionary.json dbdoc
    ```

=== "justfile"

    ```just
    regen:
        stele generate --spec model.yaml --overlay overlay.yaml --out src/acme_models
        stele ddl --package src/acme_models --schema dbo=dbo --out replica.sql
        stele check --package src/acme_models
        stele dictionary --spec model.yaml --overlay overlay.yaml --out dictionary.json
        tbls doc --rm-dist json://dictionary.json dbdoc
    ```

Run `make regen` after every overlay edit. Having the flags in one place
means your CI files stay free of them, and there is a single thing to update
when the pipeline changes.

The last line needs [tbls](https://github.com/k1LoW/tbls) on the path; the
rest is Python. `--rm-dist` clears the output directory first, which is what
makes the recipe repeatable and what stops a table dropped upstream leaving
its page behind - a stale page is tracked and unchanged, so nothing below
would catch it.

The rest of this page writes `make regen`; substitute `just regen` throughout
if you picked that one.

## 3. Check the committed output on every push

This job needs no database credentials, so it can run on every pull request.
It does need tbls, for the last line of the recipe:

```yaml
- uses: k1LoW/setup-tbls@f25e3d013a596865b2db90dac7ee19e9f15b5780 # v1.4.0
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

To see what a change would do to the dictionary without regenerating it,
`tbls diff json://dictionary.json dbdoc` prints the difference and exits
non-zero when the two disagree.

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
from sqlalchemy import select
from stele.runtime import Binding

from acme_models import Customer, Order


def orders_in_region(binding: Binding, region: int) -> list[Order]:
    """Orders whose customer sits in `region`, newest first."""
    return binding.scalars(
        select(Order)
        .join(Order.customer)
        .where(Customer.RegionId == region)
        .order_by(Order.OrderId.desc())
    )
```

Reading that outward:

- The query is ordinary SQLAlchemy. `Customer` and `Order` are normal mapped
  classes, and every query form SQLAlchemy offers works on them.
- `Order.customer` is a relationship `generate` wrote, from the reference
  between the two tables. Joining through it means the join condition comes
  from the model rather than from you restating the key columns. See
  [Relationships](generated/relationships.md) for how the names are chosen.
- The binding says **which database**. Both classes carry a schema token
  rather than a schema name, so they do not name a backend on their own — a
  lakehouse binding and a replica binding resolve the same two classes to
  different schemas. Passing one in is how you choose.
- `binding.scalars(stmt)` is shorthand for opening a session, running the
  statement and returning the objects:

    ```python
    with binding.session() as s:
        return list(s.scalars(stmt))
    ```

    Use `session()` directly when you want several statements in one
    transaction. See [Bindings and schemas](generated/index.md).

There is no ambient default binding. Forgetting to set one would not raise —
it would read the wrong database.

When passing it around gets repetitive, bind it once in your own code:

```python
from dataclasses import dataclass


@dataclass
class Orders:
    binding: Binding

    def in_region(self, region: int) -> list[Order]:
        return self.binding.scalars(
            select(Order)
            .join(Order.customer)
            .where(Customer.RegionId == region)
            .order_by(Order.OrderId.desc())
        )


orders = Orders(lakehouse)
orders.in_region(4)
```

Corrections to the *model* — a key, a relationship, a type, a name, a column
description — go in `overlay.yaml` and take effect on the next `make regen`.
Anything you write into the generated package is deleted the next time it
runs.

## Expect these

**A stele upgrade changes the generated package.** Templates change between
versions, so a dependency bump produces a diff in `src/acme_models/` with no
catalog change behind it. Run `make regen` as part of the upgrade and commit
the result alongside it. A tbls upgrade does the same to `dbdoc/`, which is
why the version belongs in the workflow rather than floating.

**`profile --sample N` reads an unordered sample.** It is a `LIMIT` without
an `ORDER BY`, so two runs can see different rows. Observed lengths round up
to buckets, so this usually changes nothing — but a value crossing a bucket
boundary widens a column for real.

**The scheduled job needs a personal access token.** The connection settings
accept a token and nothing else, so that is what goes in the secret.
