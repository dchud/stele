# Changelog

Notable changes, newest first. The format follows [Keep a
Changelog](https://keepachangelog.com/en/1.1.0/), and this project follows
[semantic versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Added

- A prior-art page relating stele to existing tools and research: model
  generators, unique column combination and inclusion dependency discovery,
  SCD2 support in ORMs and in SQL, and the regenerate-plus-overlay shape.
- `stele introspect --tz-aware` keeps timestamps timezone-aware instead of
  normalising them to UTC-naive.
- `stele introspect` names the tables whose `_history` companion an `--include`
  or `--exclude` pattern removed, and the reverse.
- The guide covers `Base.to_dict()` and `__repr__`: what they return, and that
  both read and key by attribute name, shown on a class whose attributes differ
  from its column names.
- An overlay's `add_tables` entry declares its own columns, so a table
  introspection never saw generates a class that maps. A column says what it
  holds through `source_type` or `type_override`.
- `stele infer` names the tables whose composite keys keep them out of
  relationship proposals.
- `binding.as_of(ts)` opens a session where every history table shows only the
  version valid at that instant, across selects, relationship traversals, eager
  loads and hand-written joins. Per-entity overrides and opt-outs are
  available, and `as_of()` with no argument means now.
- History classes carry relationships to their parents, joined on the key
  columns alone. Which version they find is decided by the session rather than
  baked into the join, so a parent that versions is reached through its own
  history and one that does not is reached as itself.
- A documentation site built with Material for MkDocs, covering the pipeline
  command by command, what the heuristics look for and the score every rule
  produces, where each kind of correction belongs, and how to use the generated
  package. `uv run mkdocs serve` renders it.
- Connection settings can come from a `.env` file at the top of the project,
  and `DATABRICKS_HOST` is read when `DATABRICKS_SERVER_HOSTNAME` is unset, so
  what `databricks configure` writes is picked up as it stands.
- Workflow auditing with zizmor as a `./check.sh` step, and a Dependabot
  configuration that proposes `uv` and GitHub Actions updates weekly, holding
  each release for seven days and grouping them into one pull request per
  ecosystem.
- A `py.typed` marker and complete annotations across the public surface, so a
  type checker sees real signatures for `Binding`, the SCD2 query helpers, and
  the spec dataclasses.

### Changed

- The minimum supported Python is 3.14, raised from 3.11.
- A session pinned with `binding.as_of()` refuses anything that is not a
  select.
- `Base.to_dict()` and `__repr__` read and key by attribute name, so a column
  whose attribute differs reports its real value rather than `None`. The dict
  is usable as constructor arguments.
- The SCD2 selects name what they return, so
  `binding.scalars(CustomerHistory.as_of(ts))` is a `list[CustomerHistory]`.
  `current` stays open.
- `Binding.scalars` and `Binding.rows` carry the element type of the statement
  through, so `binding.scalars(select(Customer))` is a `list[Customer]` rather
  than a `list[Any]`.

### Fixed

- Generated modules import only what they use, separate classes by two blank
  lines, and write a single-column business key as a tuple.
- `stele generate` removes modules left by a previous run that this one does
  not write, and names them. Only files carrying the generated header, in a
  directory that already holds a generated package.
- `stele.runtime.utcnow()` takes no arguments. The exported name took a config
  and the one in use did not.
- `pin()` on an already-pinned session replaces the pin rather than adding a
  second set of criteria that narrowed every select to nothing.
- `stele ddl --schema` reports a mapping without `=`, and `ddl` and `check`
  report a package directory whose name is not a usable module name, instead of
  raising.
- `infer()` leaves the caller's spec unchanged, so `stele infer --apply` counts
  every key and reference it applied.
- A `type_override` written in lower case, such as `nvarchar(50)`, imports and
  calls the type by the name the library exports.
- An overlay's unrecognised `foreign_keys_mode` is reported instead of silently
  merging, and a model file declaring a newer `spec_version` is refused instead
  of silently losing keys.
- A string column profiled as entirely empty, or longer than `NVARCHAR` allows,
  says which instead of advising a profile run that has already happened.
- The Inspector fallback collects views, which the `information_schema` path
  already did.
- `min_score` has one default across `infer()`, `apply_to_spec`, the overlay
  writer and the CLI, so a proposal written into the overlay is one that gets
  applied.
- `stele infer --validate` falls back to the next primary key candidate when
  the highest-scoring one is rejected for duplicates or nulls.
- `stele generate` reports a history table that generated nothing because its
  primary table is missing or disabled.
- A column whose name is not a valid Python identifier — `Unit Price`,
  `my-col`, `2fast` — generates a module that imports, and `generate` prints
  the renames it made.
- A row whose interval end is `NULL` is open whichever marker `end_open` names,
  and `overlaps()` reads `interval` the way `valid_at` does.
- The overlay warns about an unknown table-level key instead of discarding it,
  and applies its settings in a fixed order.
- `stele infer` quotes the column lists it writes, so a name containing a comma
  or a colon survives into the overlay the operator edits.
- Primary key columns generate with `autoincrement=False`, so the replica DDL
  no longer declares a single integer key `IDENTITY`.
- Two references from one table to the same parent generate relationships that
  resolve, named for the columns that distinguish them.
- `generate --snake-case` produces a package that imports and runs.
- A relationship whose name matches a column no longer removes that column from
  the mapping and the replica DDL. The column is kept and the collision is
  reported.
- A composite foreign key generates one `ForeignKeyConstraint` rather than one
  `ForeignKey` per column. The package now imports, and the replica DDL emits a
  single clause over the pair instead of two that SQL Server would reject.
- A self-referencing foreign key declared in the overlay generates a package
  that imports. Both ends of a self-join sit on one table, and the generated
  relationship now names which end is the parent.
- A key name claimed by tables in several schemas resolves to the one in the
  child's own schema, or, where there is none, is proposed for each candidate
  at a reduced score with the competitors named rather than picked between
  silently.
- Primary key inference recognises a key name with the affix on either side of
  the table name, so a schema naming keys `IdWidget` gets proposals, and the
  relationships that depend on them, as one naming `WidgetId` already did.
- `DATABRICKS_CATALOG` and `DATABRICKS_SCHEMA` reach the CLI: `--catalog` is
  optional and falls back to the variable. Missing settings exit with one line
  naming what is absent rather than a traceback.
