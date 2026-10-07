"""The Model Context Protocol server (`kv mcp`): a READ-ONLY, stdio adapter over the same services the CLI uses.

Nothing here has a write path. The catalog is opened with `mode=ro` and never migrated (db/catalog.py `open_catalog_strict`), the
tools are the read-only commands' services, and a test holds that every tool leaves the catalog's bytes and revision unchanged.
Mutations, when they arrive, are `propose_*` then a plan then `apply_plan` (docs/ARCHITECTURE.md), never a quiet extra tool.
"""
