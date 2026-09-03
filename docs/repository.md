# Keeping it in a repository

A first run leaves four files, and the question is which of them belong in
version control and what stops them drifting apart. One rule answers both:

**Every regenerable file is committed, and CI proves it is regenerable.**

Committing the regenerable ones is what keeps `generate`, `ddl` and `check`
credential-free — see the table in
[How it fits together](pipeline/index.md). Only a scheduled job needs to
reach the catalog. Proving they regenerate is what makes "never hand-edited"
a fact rather than a comment in a file header.

## The layout

```
pyproject.toml          depends on stele, plus a driver extra
model.yaml              committed, regenerable: what the catalog says
overlay.yaml            committed, hand-edited: what you know
replica.sql             committed, regenerable
src/acme_models/        committed, regenerable: the generated package
src/acme/               yours: bindings, subclasses, queries
tests/
Makefile                every stele invocation lives here
.env                    ignored
```

`model.yaml` and `overlay.yaml` sit at the root because that is where the CLI
defaults look for them, so nothing has to pass `--spec` or `--overlay`. A
`catalog/` subdirectory works as well if the recipe below names the paths.

The generated package takes a name of your choosing; `stele generate --out`
decides it, and the directory's basename becomes the import name, so it has
to be a valid Python identifier.

Your own code goes in a sibling package rather than inside the generated one.
`generate` removes files it wrote that a later run does not, so a file you
add there survives only because it lacks the generated header — and the
header says not to edit the tree it is in. Keep the two apart and the rule
stays simple.

## One recipe, called by people and by CI

Put every stele invocation in one place, so no workflow file contains a stele
flag and there is one thing to change when the pipeline changes:

```make
regen:
	stele generate --spec model.yaml --overlay overlay.yaml --out src/acme_models
	stele ddl --package src/acme_models --schema dbo=dbo --out replica.sql
	stele check --package src/acme_models
```

## The offline job

This is the load-bearing one. It runs on every push, needs no credentials,
and fails if the committed output is not what the committed inputs produce:

```yaml
- run: make regen
- run: |
    git status --porcelain
    test -z "$(git status --porcelain)"
```

`git status --porcelain` rather than `git diff --exit-code`: a table added
upstream produces a *new* module, and an untracked file is invisible to
`git diff`. Printing the status before the test is what turns a red build
into a readable one.

A hand-edit to the generated package cannot survive this job. Neither can a
change to `overlay.yaml` that nobody regenerated after.

What it does not catch: an edit to `overlay.yaml` itself. The overlay is an
input, and nothing regenerates it.

## The scheduled job

Catalog drift is a separate question from repository consistency, and it is
the only job needing secrets:

```yaml
on:
  schedule: [{cron: "0 6 * * 1"}]
  workflow_dispatch:
```

It runs `stele introspect`, and `stele profile` on a slower cadence. If
`model.yaml` changed, it runs the recipe and opens a pull request. The diff
in `model.yaml` is the drift; the diff in the generated package is what the
drift did.

Two things it should not do. It should not run `infer --force`, because that
overwrites the overlay you reviewed. And it should not merge itself — a new
table arrives with no key and no references until someone runs `stele infer`
for it, so the pull request wants a person.

## Three things that will surprise you otherwise

**A stele upgrade regenerates the package.** Templates change between
versions, so a dependency bump produces a diff in `src/acme_models/` with no
catalog change at all. That is the upgrade working: the regenerated package
rides in the same pull request as the bump. Without expecting it, the first
one looks like a broken build.

**`profile --sample N` reads an unordered sample.** It is a `LIMIT` with no
`ORDER BY`, so a different run can see different rows. Observed lengths are
rounded up to buckets, so most runs produce no diff — but a value crossing a
bucket boundary widens a column, and that is a real change rather than
noise. Run `profile` less often than `introspect`.

**The scheduled job holds a long-lived token.** The connection settings take
a personal access token and nothing else, so that is what sits in the
secret.

## What to commit, and why

| File | Committed | Because |
|---|---|---|
| `overlay.yaml` | yes | the only thing you cannot regenerate |
| `model.yaml` | yes | its diff is how catalog drift becomes visible, and committing it keeps everything downstream offline |
| `src/acme_models/` | yes | a clone installs and imports with no credentials, and a type change arrives as a reviewable Python diff |
| `replica.sql` | yes | the same argument, and it is what a loader consumes |
| `.env` | no | credentials |

The objection to committing generated code is that it invites hand-edits.
That is what the offline job is for.
