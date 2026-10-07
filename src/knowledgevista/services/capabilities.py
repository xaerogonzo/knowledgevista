"""What this installation can do, stated for a program rather than a person: `kv capabilities`.

It needs NO catalog (OpenChem asks before it has any reason to believe a library exists), and it is the one place that says
which commands only read. Four version numbers are reported separately because they change for different reasons:

  protocol_version        the integration contract OpenChem and other callers rely on (`capabilities`, `locate`, the
                          `knowledgevista://` grammar, the error codes). Bumped only by a break in one of those.
  json_schema_version     the shape of the envelope every `--json` command prints.
  catalog_schema_version  the SQLite schema. Internal: reported so a caller can see a catalog is newer than this program,
                          never so that it can read the file.
  mcp.protocol_versions   the Model Context Protocol revisions `kv mcp` speaks.

The command table below is the single source of truth for "read-only or mutating". A test holds it equal to the parser and to the
table in docs/CLI_CONTRACT.md, and another holds every MCP tool to the read-only commands, so adding a command forces the decision.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path
from typing import Any

from knowledgevista import __version__
from knowledgevista.db.migrations import current_version, load_migrations
from knowledgevista.envelope import JSON_SCHEMA_VERSION
from knowledgevista.index.search import QUERY_LANGUAGE_VERSION
from knowledgevista.services.cursor import MAX_LIMIT, MAX_WINDOW

#: Bumped by a change to what callers integrate against (see the module docstring), not by any other change.
PROTOCOL_VERSION = 1

READ, CATALOG, CACHE, CONFIG, FILESYSTEM = "read", "catalog", "cache", "config", "filesystem"

#: command -> kind. `read` writes nothing: not the catalog, not the cache, not the settings, never a source file.
COMMAND_KINDS: dict[str, str] = {
    "root add": CATALOG, "root list": READ, "scan": CATALOG, "stats": READ, "explain": READ, "doctor": READ, "verify": READ,
    "extract": CACHE, "search": READ, "show": READ, "import openchem-index": CACHE,
    "resolve": CATALOG, "review list": READ, "review accept": CATALOG, "review reject": CATALOG,
    "metadata set": CATALOG, "metadata clear": CATALOG, "metadata lock": CATALOG, "metadata unlock": CATALOG,
    "config list": READ, "config get": READ, "config set": CONFIG,
    "relate": CATALOG, "dupes": READ, "related": READ, "relations list": READ, "relations accept": CATALOG, "relations reject": CATALOG,
    "relations add": CATALOG, "relations remove": CATALOG, "document merge": CATALOG, "document split": CATALOG, "document canonical": CATALOG,
    "collection create": CATALOG, "collection add": CATALOG, "collection remove": CATALOG, "collection list": READ, "collection show": READ,
    "collection delete": CATALOG, "tag add": CATALOG, "tag remove": CATALOG, "tag list": READ,
    "saved list": READ, "saved run": READ, "saved delete": CATALOG, "view": READ,
    "capabilities": READ, "locate": READ, "open": READ, "mcp": READ, "gui": CATALOG,
    "root allow-organize": CATALOG, "plan create": CATALOG, "plan show": READ, "apply": FILESYSTEM, "undo": FILESYSTEM, "recover": FILESYSTEM, "history": READ,
}
#: Options that turn an otherwise read-only command into a writing one. Listed so a caller does not have to trust the kind alone.
MUTATING_OPTIONS: dict[str, list[str]] = {"search": ["--save"], "resolve": ["--accept-safe", "--online"]}
#: Commands that start another program (the operating system's viewer) although they change no knowledge-vista state.
LAUNCHES_PROGRAM = frozenset({"open"})

URI_FORMS = [
    "knowledgevista://document/<document id>",
    "knowledgevista://document/<document id>/page/<pdf page>",
    "knowledgevista://document/<document id>/label/<printed label>",
    "knowledgevista://artifact/<sha256>",
    "knowledgevista://artifact/<sha256>/page/<pdf page>",
    "knowledgevista://artifact/<sha256>/label/<printed label>",
]


def catalog_schema_version() -> int:
    return load_migrations()[-1].version


def command_table() -> list[dict[str, Any]]:
    rows = []
    for name in sorted(COMMAND_KINDS):
        kind = COMMAND_KINDS[name]
        row: dict[str, Any] = {"name": name, "kind": kind, "read_only": kind == READ}
        if name in MUTATING_OPTIONS:
            row["mutating_options"] = MUTATING_OPTIONS[name]
        if name in LAUNCHES_PROGRAM:
            row["launches_program"] = True
        rows.append(row)
    return rows


def _catalog_block(catalog_file: Path | None) -> dict[str, Any]:
    """What the catalog at `catalog_file` is, if there is one. Reading it must never create it."""
    if catalog_file is None or not catalog_file.exists():
        return {"exists": False}
    from knowledgevista.db.catalog import open_catalog, revision

    try:
        conn = open_catalog(catalog_file, create=False, read_only=True)
    except Exception as exc:  # noqa: BLE001 - "too new" and "unreadable" are both an answer, not a crash
        return {"exists": True, "readable": False, "reason": f"{type(exc).__name__}: {exc}"}
    try:
        return {"exists": True, "readable": True, "schema_version": current_version(conn), "revision": revision(conn)}
    finally:
        conn.close()


def capabilities(catalog_file: Path | None = None) -> dict[str, Any]:
    from knowledgevista.mcp import tools as mcp_tools
    from knowledgevista.mcp.protocol import SUPPORTED_PROTOCOL_VERSIONS

    return {
        "type": "capabilities",
        "app": "KnowledgeVista",
        "app_version": __version__,
        "protocol_version": PROTOCOL_VERSION,
        "json_schema_version": JSON_SCHEMA_VERSION,
        "catalog_schema_version": catalog_schema_version(),
        "query_language_version": QUERY_LANGUAGE_VERSION,
        "uri_schemes": ["knowledgevista"],
        "uri_forms": URI_FORMS,
        "commands": command_table(),
        "mcp": {"server": "kv mcp", "transport": "stdio", "read_only": True, "protocol_versions": list(SUPPORTED_PROTOCOL_VERSIONS),
                "tools": sorted(mcp_tools.TOOLS)},
        "limits": {"max_limit": MAX_LIMIT, "max_window": MAX_WINDOW},
        "features": {"extract": importlib.util.find_spec("pymupdf") is not None or importlib.util.find_spec("fitz") is not None},
        "catalog": _catalog_block(catalog_file),
    }
