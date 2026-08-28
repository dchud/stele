# Changelog

Notable changes, newest first. The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and this project follows [semantic versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Added

- Connection settings can come from a `.env` file at the top of the project, and `DATABRICKS_HOST` is read when `DATABRICKS_SERVER_HOSTNAME` is unset, so what `databricks configure` writes is picked up as it stands.
- Workflow auditing with zizmor as a `./check.sh` step, and a Dependabot configuration that proposes `uv` and GitHub Actions updates weekly, holding each release for seven days and grouping them into one pull request per ecosystem.
- A `py.typed` marker and complete annotations across the public surface, so a type checker sees real signatures for `Binding`, the SCD2 query helpers, and the spec dataclasses.

### Changed

- `Binding.scalars` and `Binding.rows` carry the element type of the statement through, so `binding.scalars(select(Customer))` is a `list[Customer]` rather than a `list[Any]` and a mistake downstream of the query is caught.

### Fixed

- A key name claimed by tables in several schemas resolves to the one in the child's own schema, or, where there is none, is proposed for each candidate at a reduced score with the competitors named rather than picked between silently.
- Primary key inference recognises a key name with the affix on either side of the table name, so a schema naming keys `IdWidget` gets proposals, and the relationships that depend on them, as one naming `WidgetId` already did.
- `DATABRICKS_CATALOG` and `DATABRICKS_SCHEMA` reach the CLI: `--catalog` is optional and falls back to the variable. Missing settings exit with one line naming what is absent rather than a traceback.
