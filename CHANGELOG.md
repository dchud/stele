# Changelog

Notable changes, newest first. The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and this project follows [semantic versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Added

- An overlay's `add_tables` entry declares its own columns, so a table introspection never saw generates a class that maps. A column says what it holds through `source_type` or `type_override`.
- `stele infer` names the tables whose composite keys keep them out of relationship proposals, so the gap is a line of output rather than an absence you have to notice.
- `binding.as_of(ts)` opens a session where every history table shows only the version valid at that instant, across selects, relationship traversals, eager loads and hand-written joins. Per-entity overrides and opt-outs are available, and `as_of()` with no argument means now.
- History classes carry relationships to their parents, joined on the key columns alone. Which version they find is decided by the session rather than baked into the join, so a parent that versions is reached through its own history and one that does not is reached as itself.
- A documentation site built with Material for MkDocs, covering the pipeline command by command, what the heuristics look for and the score every rule produces, where each kind of correction belongs, and how to use the generated package. `uv run mkdocs serve` renders it.
- Connection settings can come from a `.env` file at the top of the project, and `DATABRICKS_HOST` is read when `DATABRICKS_SERVER_HOSTNAME` is unset, so what `databricks configure` writes is picked up as it stands.
- Workflow auditing with zizmor as a `./check.sh` step, and a Dependabot configuration that proposes `uv` and GitHub Actions updates weekly, holding each release for seven days and grouping them into one pull request per ecosystem.
- A `py.typed` marker and complete annotations across the public surface, so a type checker sees real signatures for `Binding`, the SCD2 query helpers, and the spec dataclasses.

### Changed

- A session pinned with `binding.as_of()` refuses anything that is not a select. The criteria attach to a select, so an update or textual SQL would run against every version rather than the pinned one.
- `Base.to_dict()` and `__repr__` read and key by attribute name, so a column whose attribute differs reports its real value rather than `None`. The dict is usable as constructor arguments.
- The SCD2 selects name what they return, so `binding.scalars(CustomerHistory.as_of(ts))` is a `list[CustomerHistory]`. `current` stays open, because it queries the primary table when the live row is not duplicated into history.
- `Binding.scalars` and `Binding.rows` carry the element type of the statement through, so `binding.scalars(select(Customer))` is a `list[Customer]` rather than a `list[Any]` and a mistake downstream of the query is caught.

### Fixed

- A column whose name is not a valid Python identifier — `Unit Price`, `my-col`, `2fast` — generates a module that imports. The whole name is checked rather than its first character, every Python keyword is recognised, and `generate` prints the renames it made.
- A row whose interval end is `NULL` is open whichever marker `end_open` names, and `overlaps()` reads `interval` the way `valid_at` does. The two predicates disagreed, so `current()` could return rows `as_of()` never found.
- The overlay warns about an unknown table-level key instead of discarding it, and applies its settings in a fixed order, so an explicitly declared `primary_key_origin` is no longer overwritten depending on the process.
- `stele infer` quotes the column lists it writes, so a name containing a comma or a colon survives into the overlay the operator edits.
- Primary key columns generate with `autoincrement=False`, so the replica DDL no longer declares a single integer key `IDENTITY`. The source owns the key values, and a bulk load without `KEEPIDENTITY` would have renumbered every row and broken the foreign keys pointing at it.
- Two references from one table to the same parent generate relationships that resolve, named for the columns that distinguish them. Previously the package would not import at all, which made the `CreatedBy` and `UpdatedBy` shape ungeneratable.
- `generate --snake-case` produces a package that imports and runs. Four places kept using raw column names where attribute names were required, so the descriptor and the history joins referred to attributes that did not exist.
- A relationship whose name matches a column no longer removes that column from the mapping and the replica DDL. The column is kept and the collision is reported.
- A composite foreign key generates one `ForeignKeyConstraint` rather than one `ForeignKey` per column. The package now imports, and the replica DDL emits a single clause over the pair instead of two that SQL Server would reject.
- A self-referencing foreign key declared in the overlay generates a package that imports. Both ends of a self-join sit on one table, and the generated relationship now names which end is the parent.
- A key name claimed by tables in several schemas resolves to the one in the child's own schema, or, where there is none, is proposed for each candidate at a reduced score with the competitors named rather than picked between silently.
- Primary key inference recognises a key name with the affix on either side of the table name, so a schema naming keys `IdWidget` gets proposals, and the relationships that depend on them, as one naming `WidgetId` already did.
- `DATABRICKS_CATALOG` and `DATABRICKS_SCHEMA` reach the CLI: `--catalog` is optional and falls back to the variable. Missing settings exit with one line naming what is absent rather than a traceback.
