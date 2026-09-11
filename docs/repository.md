# Using the output in your own repository

The pipeline leaves five outputs, and tbls renders a sixth. Around them go
two repositories that other people can clone, that stay current as the
catalog changes, and that keep the generated code generated.

The **model repository** holds the inputs and the code generated from them.
It is where stele runs, and the only one that needs warehouse credentials.

The **documentation repository** holds the site people read. It needs
neither stele nor a warehouse: what arrives there is Markdown and a MkDocs
project to build it with.

Splitting them suits a site with an audience wider than the people who run
the pipeline. One repository can hold everything instead — see [keeping it
in one repository](#keeping-it-in-one-repository).

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
| the rendered dictionary | `tbls doc`, from `dictionary.json` | no |
| the reference pages | `generate --docs` | no |

The first four live here. The last two are written into the documentation
repository, and `dictionary.json` stays here as the record of what was
published.

Commit all of them. Only `overlay.yaml` is written by hand; the rest are
rebuilt from it and from `model.yaml`, and committing them means a
contributor can clone the repository, import the models and read the data
dictionary without any Databricks credentials. The dictionary is the one
whose readers are mostly people who never run the pipeline, so a rendered
tree they can browse in the repository is most of its value.

The rendered pages are the ones to think about rather than commit
reflexively. They belong in git wherever people read them, which is the
documentation repository — so this one commits `dictionary.json` and not the
Markdown derived from it.

stele's own repository ignores `dictionary.json`. That is the opposite
advice for the opposite reason: there the file would describe one customer's
catalog rather than anything about the tool. Here it describes your model,
so it belongs in git.

## 1. Lay out the model repository

```
pyproject.toml          depends on stele, plus a driver extra
model.yaml
overlay.yaml
replica.sql
dictionary.json         published; the docs repository renders from it
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

    docs:
    	stele generate --spec model.yaml --overlay overlay.yaml \
    	  --out src/acme_models --docs ../docsrepo/docs/api
    	tbls doc --rm-dist json://dictionary.json ../docsrepo/docs/database
    	stele site --document dictionary.json --out ../docsrepo
    ```

=== "justfile"

    ```just
    regen:
        stele generate --spec model.yaml --overlay overlay.yaml --out src/acme_models
        stele ddl --package src/acme_models --schema dbo=dbo --out replica.sql
        stele check --package src/acme_models
        stele dictionary --spec model.yaml --overlay overlay.yaml --out dictionary.json

    docs:
        stele generate --spec model.yaml --overlay overlay.yaml \
          --out src/acme_models --docs ../docsrepo/docs/api
        tbls doc --rm-dist json://dictionary.json ../docsrepo/docs/database
        stele site --document dictionary.json --out ../docsrepo
    ```

Run `make regen` after every overlay edit. Having the flags in one place
means your CI files stay free of them, and there is a single thing to update
when the pipeline changes.

`regen` rebuilds what belongs in this repository, and needs nothing but
Python.

`docs` writes into the documentation repository, taken to be checked out
beside this one, and needs [tbls](https://github.com/k1LoW/tbls) on the
path. Run it after an overlay edit, or whenever the catalog changes.

Both call `generate`: one command writes the package and its reference
pages.

Order matters within `docs`. `--rm-dist` empties the output directory
before writing it, so `stele site` comes after `tbls doc` and not before —
it writes the nav file into that directory. Emptying it is also what stops
a table dropped upstream leaving its page behind.

`make regen` is written throughout; substitute `just regen` if you picked
that one.

## 3. The documentation repository

A MkDocs site with no `nav` of its own builds one from every file it finds.
tbls writes a flat directory — a page per table named `schema.table.md`, a
page per viewpoint, an index — so a few hundred tables become a few hundred
flat navbar entries, and the content sits behind them.

`stele site` writes a site that does not do that. Point it at the
documentation repository:

```bash
stele site --document dictionary.json --out ../docsrepo
```

Run it **after** `tbls doc`, not before. It writes the nav file into the
rendered directory, and `--rm-dist` clears that directory — so the pages come
first and the nav file describes what is there.

Six files, under one rule. **The nav file beside the rendered pages follows
the schemas in the document, so stele owns it and rewrites it. Everything
else describes a site rather than a model, so it is written once and never
overwritten** — edit them freely, and dropping into a site that
already exists leaves it alone.

| File | Contents |
|---|---|
| `docs/database/.nav.yml` | a group per schema, matched by glob — rewritten every run |
| `docs/.nav.yml` | the site's top-level order |
| `docs/stylesheets/wide.css` | widths for a screen a table is read on |
| `mkdocs.yml` | the theme features that matter at this size, and the plugin |
| `pyproject.toml` | `mkdocs-material` and `mkdocs-awesome-nav`, for `uv run` |
| `docs/index.md` | a home page linking to the dictionary |

Nothing there names stele or tbls. Both run here, where the model and the
credentials are; what lands over there is a site that `uv run mkdocs serve`
builds and nothing else.

```make
docs:
	stele generate --spec model.yaml --overlay overlay.yaml \
	  --out src/acme_models --docs ../docsrepo/docs/api
	stele dictionary --spec model.yaml --overlay overlay.yaml --out dictionary.json
	tbls doc --rm-dist json://dictionary.json ../docsrepo/docs/database
	stele site --document dictionary.json --out ../docsrepo
```

Dropping into a site that already exists leaves it alone, which means two
things need adding by hand. `mkdocs.yml` needs `awesome-nav` in its `plugins`
and `navigation.prune` in the theme's `features`, without which the pages are
not grouped. And `docs/.nav.yml` needs the subtrees naming where you want
them — `awesome-nav` appends what it does not name, so they appear either way,
just last until you say otherwise. The run prints both.

### Two documents, side by side

The dictionary describes the **data model** - what `dbo.Order` holds, and what
the data showed about it. `stele generate --docs` writes the other half: what
`Order` is called in Python, what its attributes are named, and which
relationships it carries. A person reading SQL wants the first; a person
writing a query in the generated package wants the second.

The reference pages come from the same pass that writes the package, so a
class cannot be documented as something other than what was emitted, and each
one links to the table it maps.

### Why those pieces

Grouping by schema uses globs — `dbo.*.md` rather than a list of tables — so a
table added upstream appears in the right group with nothing to edit. Globs
work in `awesome-nav`'s `.nav.yml` and are ignored without complaint in
`mkdocs.yml`'s own `nav`, and a glob in a parent directory does not reach into
a child, which rules out keeping the file one level up to survive `--rm-dist`.

`navigation.prune` is the theme feature that matters. Without it MkDocs writes
the entire nav into every page, so the cost of a few hundred sidebar entries
is paid once per table rather than once.

The viewpoint pages are named in the nav rather than left to fall through.
`awesome-nav` appends whatever no glob matched, so they would otherwise
arrive after the last schema with no heading. Their titles come from each
page's own heading, so they read as `dbo` and `Inferred shape` rather than
`viewpoint-0`.

### Width

A table of twenty columns is a different shape from a paragraph, and Material
lays out for the paragraph: its content grid stops at 1464px however wide the
window is, which on an external display leaves the page in a column down the
middle and the table wrapped inside it.

`docs/stylesheets/wide.css` raises that cap to 2000px, and `toc.integrate`
folds the right-hand contents into the left nav to return its column. On a
2560px display that takes the content from 1174px to 1710px, which is enough
for a table of thirteen columns to reach its natural width instead of
wrapping. On a 1440px laptop the cap never binds and the page is unchanged.

Raising it further mostly stretches the prose: a table stops widening once
its columns fit. Where one is wider than that, the header's toggle collapses
the navigation and hands its column to the content — the stylesheet shows
Material's own drawer toggle, which the theme hides above the width where the
navigation becomes a sidebar.

The file is written once, so the widths are yours to change.

### Without the plugin

If you would rather not add one, give MkDocs a single entry and let tbls's own
index do the navigating:

```yaml
nav:
  - Home: index.md
  - Data dictionary: database/README.md
validation:
  nav:
    omitted_files: ignore
```

Every page is still built and reachable — the index lists every table, and the
viewpoint pages group them by schema already. What you lose is browsing from
the sidebar; what you gain is nothing to maintain and no `cp`. Link into the
subtree as `database/README.md`, since a bare `database/` is not recognised as
a link target.

### Keeping it in one repository

Where the dictionary's readers are the people who already work in the model
repository, one repository is enough. Two arrangements cover that.

**Read it on GitHub.** Render into `dbdoc/` at the top of this repository —
tbls's default — commit it, and skip `stele site` entirely. GitHub renders
the Markdown, there is no site to build and nothing to configure. The
navbar problem never arises because there is no navbar.

**A MkDocs site in the model repository.** Point both paths at this
repository rather than a sibling:

```bash
tbls doc --rm-dist json://dictionary.json docs/database
stele site --document dictionary.json --out .
```

The site is built the same way. What differs is that this repository then
carries the MkDocs dependencies alongside stele's, and the pull request
check needs tbls to rebuild the pages it commits.

## 4. Check the committed output on every push

This job needs no database credentials and no second checkout, so it can run
on every pull request:

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

This job leaves `make docs` alone. What that target writes lands in another
repository, where a dirty tree here cannot see it; the documentation
repository's own build is what fails when its pages are wrong.

This job says nothing about `overlay.yaml` itself. That file is an input, so
there is nothing to compare it against.

To see what a change would do to the dictionary without regenerating it,
`tbls diff json://dictionary.json ../docsrepo/docs/database` prints the
difference and exits non-zero when the two disagree.

## 5. Refresh from the catalog on a schedule

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
the result alongside it. A tbls upgrade does the same to the rendered
dictionary, which is why the version belongs in the workflow rather than
floating.

**`profile --sample N` reads an unordered sample.** It is a `LIMIT` without
an `ORDER BY`, so two runs can see different rows. Observed lengths round up
to buckets, so this usually changes nothing — but a value crossing a bucket
boundary widens a column for real.

**The scheduled job needs a personal access token.** The connection settings
accept a token and nothing else, so that is what goes in the secret.
