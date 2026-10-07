"""`kv capabilities` and the tables behind it: the one statement of what reads and what writes, held equal to the parser, the
contract document and the MCP tools, so adding a command or a tool forces the decision about whether it may write."""

from __future__ import annotations

import json
import re
from pathlib import Path

from support import make_env

from knowledgevista import __version__, cli
from knowledgevista.db.migrations import load_migrations
from knowledgevista.envelope import JSON_SCHEMA_VERSION
from knowledgevista.index.search import QUERY_LANGUAGE_VERSION
from knowledgevista.mcp import tools
from knowledgevista.mcp.protocol import SUPPORTED_PROTOCOL_VERSIONS
from knowledgevista.services import capabilities as cap

ROOT = Path(__file__).resolve().parent.parent
#: The command each MCP tool is the read-only counterpart of. A new tool must be placed here, which means choosing a READ command.
TOOL_COMMANDS = {
    "search_pages": "search", "get_document": "explain", "get_page": "show", "get_page_by_label": "show", "resolve_reference": "locate",
    "get_metadata": "explain", "list_related": "related", "list_duplicates": "dupes", "locate_artifact": "locate",
}


def parser_commands() -> set[str]:
    top = next(a for a in cli.build_parser()._actions if a.dest == "command")
    names = set()
    for name, command in top.choices.items():
        nested = next((a for a in command._actions if a.__class__.__name__ == "_SubParsersAction"), None)
        names |= {f"{name} {action}" for action in nested.choices} if nested else {name}
    return names


def test_the_table_names_exactly_the_commands_the_parser_has():
    assert set(cap.COMMAND_KINDS) == parser_commands()


def test_the_table_agrees_with_the_kind_column_of_the_contract_document():
    contract = (ROOT / "docs" / "CLI_CONTRACT.md").read_text(encoding="utf-8")
    rows = {}
    for line in contract.splitlines():
        match = re.match(r"\| `([a-z][a-z -]*?)(?: [<\[-].*?)?` \| (read|catalog|cache|config|filesystem) \|", line)
        if match:
            rows[match.group(1)] = match.group(2)
    assert rows == cap.COMMAND_KINDS


def test_every_mcp_tool_is_the_counterpart_of_a_read_only_command():
    assert set(tools.TOOLS) == set(TOOL_COMMANDS), "a tool was added or removed without deciding which read command it mirrors"
    for tool, command in TOOL_COMMANDS.items():
        assert cap.COMMAND_KINDS[command] == cap.READ, (tool, command)


def test_a_read_command_with_a_writing_option_says_so():
    table = {row["name"]: row for row in cap.command_table()}
    assert table["search"]["read_only"] is True and table["search"]["mutating_options"] == ["--save"]
    assert "mutating_options" not in table["stats"] and table["scan"]["read_only"] is False and table["extract"]["kind"] == "cache"
    assert table["open"]["launches_program"] is True and "launches_program" not in table["locate"]
    assert {c for c, kind in cap.COMMAND_KINDS.items() if kind == cap.READ} >= {"locate", "capabilities", "mcp", "open", "dupes"}


def test_every_option_named_as_mutating_exists_on_its_command():
    help_text = {"search": "--save", "resolve": "--accept-safe"}
    for command, options in cap.MUTATING_OPTIONS.items():
        parser_text = _help_of(command)
        for option in options:
            assert option in parser_text, (command, option)
    assert all(option in _help_of(command) for command, option in help_text.items())


def _help_of(command: str) -> str:
    parser = cli.build_parser()
    for word in command.split():
        top = next(a for a in parser._actions if a.__class__.__name__ == "_SubParsersAction")
        parser = top.choices[word]
    return parser.format_help()


def test_capabilities_needs_no_catalog_and_creates_nothing(tmp_path, capsys):
    missing = tmp_path / "nowhere" / "c.sqlite"
    code, env, _ = _run(capsys, missing)
    assert code == 0 and env["ok"] is True and env["command"] == "capabilities"
    (record,) = env["records"]
    assert record["catalog"] == {"exists": False} and not missing.parent.exists()


def _run(capsys, catalog):
    code = cli.main(["--json", "--catalog", str(catalog), "capabilities"])
    return code, json.loads(capsys.readouterr().out), None


def test_the_versions_are_separate_numbers_and_come_from_the_things_they_describe(tmp_path, capsys):
    _, env, _ = _run(capsys, tmp_path / "c.sqlite")
    record = env["records"][0]
    assert record["app_version"] == __version__ and record["protocol_version"] == cap.PROTOCOL_VERSION == 1
    assert record["json_schema_version"] == JSON_SCHEMA_VERSION and record["query_language_version"] == QUERY_LANGUAGE_VERSION
    assert record["catalog_schema_version"] == load_migrations()[-1].version
    assert record["uri_schemes"] == ["knowledgevista"] and len(record["uri_forms"]) == 6
    assert record["mcp"] == {"server": "kv mcp", "transport": "stdio", "read_only": True, "protocol_versions": list(SUPPORTED_PROTOCOL_VERSIONS),
                             "tools": sorted(tools.TOOLS)}
    assert record["limits"] == {"max_limit": 1000, "max_window": 5000} and isinstance(record["features"]["extract"], bool)
    assert [c["name"] for c in record["commands"]] == sorted(c["name"] for c in record["commands"])


def test_an_existing_catalog_is_described_not_changed(tmp_path, capsys):
    env = make_env(tmp_path, {"a.txt": "alpha"})
    env.scan()
    before = env.revision()
    _, answer, _ = _run(capsys, env.catalog)
    block = answer["records"][0]["catalog"]
    assert block == {"exists": True, "readable": True, "schema_version": load_migrations()[-1].version, "revision": before} and env.revision() == before


def test_a_catalog_from_a_newer_program_is_reported_as_unreadable_not_crashed_on(tmp_path, capsys):
    env = make_env(tmp_path, {"a.txt": "alpha"})
    env.conn.execute("INSERT INTO schema_migration (version, name, applied_at, app_version) VALUES (99, 'future', 'now', 'x')")
    _, answer, _ = _run(capsys, env.catalog)
    block = answer["records"][0]["catalog"]
    assert block["exists"] is True and block["readable"] is False and block["reason"].startswith("SchemaTooNew")


def test_capabilities_prints_the_same_bytes_every_time(tmp_path, capsys):
    cli.main(["--json", "--catalog", str(tmp_path / "c.sqlite"), "capabilities"])
    first = capsys.readouterr().out
    cli.main(["--json", "--catalog", str(tmp_path / "c.sqlite"), "capabilities"])
    assert capsys.readouterr().out == first and first.count("\n") == 1


def test_the_default_catalog_location_is_not_touched_by_asking(monkeypatch, tmp_path, capsys):
    monkeypatch.setenv("KNOWLEDGEVISTA_HOME", str(tmp_path / "home"))
    cli.main(["--json", "capabilities"])
    answer = json.loads(capsys.readouterr().out)
    assert answer["records"][0]["catalog"] == {"exists": False} and not (tmp_path / "home").exists()
