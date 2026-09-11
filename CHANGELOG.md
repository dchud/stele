# Changelog

Notable changes, newest first. The format follows [Keep a
Changelog](https://keepachangelog.com/en/1.1.0/), and this project follows
[semantic versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Added

- `stele generate --docs <dir>` writes reference pages for the package it
  emits: a page per module saying what each class maps to, what its
  attributes are called and which relationships it carries, with each class
  linked to its table in the data dictionary.
- The site `stele site` writes is laid out for the tables it carries: the
  content grid is widened, the right-hand contents folded into the left
  nav, and the navigation made collapsible from the header. Material's
  default stops the content at 1464px however wide the screen.
- `stele site` writes a MkDocs project around a rendered dictionary,
  grouping the pages by schema. Without one, MkDocs builds a navbar from
  every file and the content disappears behind it. Its only input is
  `dictionary.json`, and its output a uv project needing only MkDocs, so the
  site can live in a repository holding neither stele nor the model.
- `stele profile` reports each table as it finishes, with the time that table
  took and an estimate for the rest, and writes the spec as it goes.
  `--resume` skips tables already carrying the observations the run would
  make, so an interrupted pass continues rather than starting again.
- `stele infer --validate` reports the same way while it checks proposals.
- `stele dictionary` writes a [tbls](https://github.com/k1LoW/tbls) document
  from `model.yaml`, which `tbls doc` renders as a browsable site with ER
  diagrams. Every key and reference carries where it came from and whether the
  data confirmed it. Neither half needs a database connection.
- `stele profile` records each table's row count, which the data dictionary
  reports and which was otherwise computed and discarded.
- `stele infer --discover` proposes references no column name reveals - an
  opaque name, a table's own key, a self-reference - by ruling candidate pairs
  out with the value ranges and distinct counts `profile` records. Each reaches
  the overlay commented out, marked `DISCOVERED`.
- A reference to a composite key is proposed when a child carries every column
  of it under the parent's own names, scoring 0.70. Composite keys no child
  carries that way are named in the output.
- A page on getting query results into pandas, polars, ibis or Spark: which
  libraries take a statement and the binding's engine, which need the compiled
  SQL, and why there is no Arrow method.
- A page on setting up your own repository around the output: where the files
  go, one recipe to rebuild them, a check that the committed package matches
  its inputs, and a scheduled refresh from the catalog.
- `Binding.compile(stmt)` renders a statement for that binding's dialect with
  its real schema names, for handing to a consumer that is not SQLAlchemy.
- A foreign key check that leaves values unmatched names a few of them on the
  evidence line, alongside the containment ratio.
- `stele infer --overlay` applies an overlay before inferring. With
  `--validate` it checks every reference the overlay declares against the data,
  which name matching never proposes and so never checked.
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

- Logging shows stele's own records. The level used to be set on the root
  logger, so asking for stele at INFO also asked the Databricks driver to
  narrate authentication, retries and every HTTP 200 it received.
  `--verbose` still opens the libraries back up.
- The Databricks connector is asked for three attempts per request rather
  than its default of thirty across fifteen minutes. Thirty suits a
  warehouse that is busy; on one that is failing it spends a quarter of an
  hour before the error reaches anyone.
- A profiling statement that fails is retried before its table is given up,
  and three tables failing in a row stops the run rather than working
  through hundreds that will fail the same way.
- `stele profile` records the observed minimum and maximum of every integer
  column, in the pass it already makes, and profiles a table that has integer
  columns but no character ones.
- `stele profile` and `stele infer --validate` build their statements with
  SQLAlchemy Core, so the same check compiles for SQL Server as for Databricks.
  Row limits, identifier quoting and string length come from the dialect.
- `stele profile` and `stele infer` read the catalog the spec names, ahead of
  `DATABRICKS_CATALOG`. `--catalog` still wins over both.
- `model.yaml` no longer records when it was introspected, so regenerating it
  against an unchanged catalog produces an identical file. A file written by an
  earlier version does not load; re-run `stele introspect`.
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

- A data dictionary whose proposed keys all verified no longer writes a
  viewpoint selecting on a label nothing carries, which tbls rejects.

- Two bindings over one engine resolve their own schema, which is now asserted
  by a test rather than left to the compiled cache's behaviour.
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
