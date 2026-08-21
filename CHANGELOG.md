# Changelog

Notable changes, newest first. The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and this project follows [semantic versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Added

- Workflow auditing with zizmor as a `./check.sh` step, and a Dependabot configuration that proposes `uv` and GitHub Actions updates weekly, holding each release for seven days and grouping them into one pull request per ecosystem.
- A `py.typed` marker and complete annotations across the public surface, so a type checker sees real signatures for `Binding`, the SCD2 query helpers, and the spec dataclasses.
