# Changelog

## Unreleased

- Milestone 1: the identity substrate. Roots, locations (history, never deleted), artifacts (SHA-256) and documents;
  a scanner that reconciles disk with the catalog (moves and renames keep identity, replaced bytes become a new
  artifact and document, absence is `missing` and never a deletion, an unavailable or half-mounted root changes
  nothing, a killed scan is recoverable); `kv root add/list`, `scan`, `stats`, `explain`, `doctor`, `verify`; a stable
  JSON envelope with documented exit codes and error codes (docs/CLI_CONTRACT.md). 241 tests; 36 planted faults in
  the scanner and services were each caught.
- Deviation recorded: artifact state is not a stored column (see docs/ARCHITECTURE.md).
- A root that contains the catalog file is refused; Access `.mdb` files are recognised as databases (found by
  running against a real 1,862-file library).

- Milestone 0: repository bootstrap. Package skeleton, `kv --version`, a single `paths` module for app-data
  locations, a forward-only migration framework (atomic per migration, backup with SQLite's backup API, refuses a
  newer schema), a generated-fixture PDF builder, a safe-repo check, a lockfile-closure licence inventory, and the
  invariants, safety model and architecture documents.
