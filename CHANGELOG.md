# Changelog

Notable changes, newest first. The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and this project follows [semantic versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Added

- `stele infer` names the tables whose composite keys keep them out of relationship proposals, so the gap is a line of output rather than an absence you have to notice.
- `binding.as_of(ts)` opens a session where every history table shows only the version valid at that instant, across selects, relationship traversals, eager loads and hand-written joins. Per-entity overrides and opt-outs are available, and `as_of()` with no argument means now.
- History classes carry relationships to their parents, joined on the key columns alone. Which version they find is decided by the session rather than baked into the join, so a parent that versions is reached through its own history and one that does not is reached as itself.
- A documentation site built with Material for MkDocs, covering the pipeline command by command, what the heuristics look for and the score every rule produces, where each kind of correction belongs, and how to use the generated package. `uv run mkdocs serve` renders it.
- Connection settings can come from a `.env` file at the top of the project, and `DATABRICKS_HOST` is read when `DATABRICKS_SERVER_HOSTNAME` is unset, so what `databricks configure` writes is picked up as it stands.
- Workflow auditing with zizmor as a `./check.sh` step, and a Dependabot configuration that proposes `uv` and GitHub Actions updates weekly, holding each release for seven days and grouping them into one pull request per ecosystem.
- A `py.typed` marker and complete annotations across the public surface, so a type checker sees real signatures for `Binding`, the SCD2 query helpers, and the spec dataclasses.

### Changed

- The SCD2 selects name what they return, so `binding.scalars(CustomerHistory.as_of(ts))` is a `list[CustomerHistory]`. `current` stays open, because it queries the primary table when the live row is not duplicated into history.
- `Binding.scalars` and `Binding.rows` carry the element type of the statement through, so `binding.scalars(select(Customer))` is a `list[Customer]` rather than a `list[Any]` and a mistake downstream of the query is caught.

### Fixed

- A composite foreign key generates one `ForeignKeyConstraint` rather than one `ForeignKey` per column. The package now imports, and the replica DDL emits a single clause over the pair instead of two that SQL Server would reject.
- A self-referencing foreign key declared in the overlay generates a package that imports. Both ends of a self-join sit on one table, and the generated relationship now names which end is the parent.
- A key name claimed by tables in several schemas resolves to the one in the child's own schema, or, where there is none, is proposed for each candidate at a reduced score with the competitors named rather than picked between silently.
- Primary key inference recognises a key name with the affix on either side of the table name, so a schema naming keys `IdWidget` gets proposals, and the relationships that depend on them, as one naming `WidgetId` already did.
- `DATABRICKS_CATALOG` and `DATABRICKS_SCHEMA` reach the CLI: `--catalog` is optional and falls back to the variable. Missing settings exit with one line naming what is absent rather than a traceback.
