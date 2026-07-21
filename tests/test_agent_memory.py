import json
import os
import sqlite3
from dataclasses import replace
from pathlib import Path

import pytest

import agent_memory


def write_action(
    root: Path,
    *,
    memory_uuid: str,
    timestamp: str = "2026-07-21T12:00:00Z",
    agent_id: str = "pr.command.zordon",
    session_id: str = "ses_test",
    event_type: str = "message",
    title: str = "Example title",
    summary: str = "Example summary",
    message: str = "Example full message",
) -> Path:
    path = root / "actions" / "2026" / "07" / f"event-{memory_uuid}.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "\n".join(
            [
                "---",
                f"memory_uuid: {memory_uuid}",
                f"timestamp: {timestamp}",
                f"session_id: {session_id}",
                f"agent_id: {agent_id}",
                "agent_name: Zordon",
                "model: test",
                "provider: test",
                "variant: test",
                f"event_type: {event_type}",
                "phase: actions",
                "redaction: false",
                "---",
                f"# {title}",
                "",
                "## Links",
                "- master: [[memory/master]]",
                "",
                "## Summary",
                summary,
                "",
                "## Full Message",
                message,
                "",
                "## Tool Outputs (summarized)",
                "- none",
                "",
                "## Raw Output Links",
                "- none",
                "",
            ]
        ),
        encoding="utf-8",
    )
    return path


@pytest.mark.parametrize(
    "value",
    ["", ".", "..", "../escape", "nested/name", "nested\\name", "/absolute", "bad\nvalue"],
)
def test_validate_identifier_rejects_unsafe_values(value: str) -> None:
    with pytest.raises(ValueError):
        agent_memory.validate_identifier(value, "session id")


def test_validate_identifier_preserves_namespaced_ids() -> None:
    assert agent_memory.validate_identifier("pr.command.zordon", "agent id") == "pr.command.zordon"


@pytest.mark.parametrize("link", ["[[../secret]]", "[[/etc/passwd]]", "[[actions/../../secret]]"])
def test_link_to_path_rejects_paths_outside_root(tmp_path: Path, link: str) -> None:
    with pytest.raises(ValueError):
        agent_memory.link_to_path(link, tmp_path)


def test_search_matches_canonical_fields_and_excludes_raw_files(tmp_path: Path) -> None:
    title_path = write_action(tmp_path, memory_uuid="title-id", title="Unique Quasar title")
    write_action(tmp_path, memory_uuid="summary-id", summary="A quasar summary")
    write_action(tmp_path, memory_uuid="message-id", message="The message mentions quasar twice: quasar")
    raw = tmp_path / "actions" / "2026" / "07" / "raw" / "secret.raw"
    raw.parent.mkdir(parents=True)
    raw.write_text("quasar", encoding="utf-8")

    results = agent_memory.search_action_documents(tmp_path, "quasar", limit=10)

    assert [result.memory_uuid for result in results] == ["message-id", "title-id", "summary-id"]
    assert results[1].relative_path == title_path.relative_to(tmp_path).as_posix()
    assert {result.matched_field for result in results} == {"title", "summary", "message"}
    assert all("secret.raw" not in result.relative_path for result in results)


def test_search_filters_and_json_contract(tmp_path: Path) -> None:
    write_action(
        tmp_path,
        memory_uuid="wanted-id",
        agent_id="pr.command.zordon",
        session_id="ses_wanted",
        event_type="checkpoint",
        message="portable semantic memory",
    )
    write_action(
        tmp_path,
        memory_uuid="other-id",
        agent_id="pr.rangers.billy",
        session_id="ses_other",
        event_type="message",
        message="portable semantic memory",
    )

    results = agent_memory.search_action_documents(
        tmp_path,
        "semantic memory",
        agent_id="pr.command.zordon",
        session_id="ses_wanted",
        event_type="checkpoint",
        since="2026-07-21T00:00:00Z",
        until="2026-07-22T00:00:00Z",
    )

    assert len(results) == 1
    payload = json.loads(json.dumps(results[0].as_dict()))
    assert payload == {
        "timestamp": "2026-07-21T12:00:00Z",
        "memory_uuid": "wanted-id",
        "agent": "pr.command.zordon",
        "session": "ses_wanted",
        "event_type": "checkpoint",
        "relative_path": "actions/2026/07/event-wanted-id.md",
        "matched_field": "message",
        "snippet": "portable semantic memory",
        "score": 2.0,
    }


def test_search_prefers_newer_results_when_relevance_is_equal(tmp_path: Path) -> None:
    write_action(
        tmp_path,
        memory_uuid="older-id",
        timestamp="2026-07-20T12:00:00Z",
        message="portable memory",
    )
    write_action(
        tmp_path,
        memory_uuid="newer-id",
        timestamp="2026-07-21T12:00:00Z",
        message="portable memory",
    )

    results = agent_memory.search_action_documents(tmp_path, "portable memory")

    assert [result.memory_uuid for result in results] == ["newer-id", "older-id"]


def test_search_refuses_symlinked_action_files(tmp_path: Path) -> None:
    outside = tmp_path.parent / "outside-memory.md"
    outside.write_text("quasar", encoding="utf-8")
    action = tmp_path / "actions" / "2026" / "07" / "linked.md"
    action.parent.mkdir(parents=True)
    action.symlink_to(outside)

    assert agent_memory.search_action_documents(tmp_path, "quasar") == []


def test_search_cli_outputs_json(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    write_action(tmp_path, memory_uuid="cli-id", message="durable cross project recall")
    parser = agent_memory.build_parser()
    args = parser.parse_args(
        [
            "search",
            "cross project",
            "--root",
            str(tmp_path),
            "--format",
            "json",
        ]
    )

    assert args.func(args) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload[0]["memory_uuid"] == "cli-id"
    assert payload[0]["matched_field"] == "message"


def test_log_rejects_traversal_identifier(tmp_path: Path) -> None:
    parser = agent_memory.build_parser()
    args = parser.parse_args(
        [
            "log",
            "--root",
            str(tmp_path),
            "--agent-id",
            "pr.command.zordon",
            "--session-id",
            "..",
            "--message",
            "I will test traversal handling",
        ]
    )

    with pytest.raises(ValueError):
        args.func(args)


def test_search_rejects_invalid_filter_identifier(tmp_path: Path) -> None:
    with pytest.raises(ValueError):
        agent_memory.search_action_documents(tmp_path, "memory", session_id="../escape")


def test_log_rejects_traversal_memory_uuid(tmp_path: Path) -> None:
    parser = agent_memory.build_parser()
    args = parser.parse_args(
        [
            "log",
            "--root",
            str(tmp_path),
            "--agent-id",
            "pr.command.zordon",
            "--session-id",
            "ses_test",
            "--memory-uuid",
            "../../escape",
            "--message",
            "I will test UUID traversal handling",
        ]
    )

    with pytest.raises(ValueError):
        args.func(args)


def test_agent_index_is_keyed_by_stable_agent_id(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    parser = agent_memory.build_parser()
    log_args = parser.parse_args(
        [
            "log",
            "--root",
            str(tmp_path),
            "--agent-id",
            "pr.command.zordon",
            "--agent-name",
            "Zordon",
            "--session-id",
            "ses_test",
            "--message",
            "I will preserve stable agent identity",
        ]
    )
    assert log_args.func(log_args) == 0
    capsys.readouterr()

    load_args = parser.parse_args(
        [
            "load",
            "--root",
            str(tmp_path),
            "--source",
            "agent",
            "--agent-id",
            "pr.command.zordon",
            "--format",
            "json",
        ]
    )
    assert load_args.func(load_args) == 0
    payload = json.loads(capsys.readouterr().out)

    assert payload[0]["agent"] == "pr.command.zordon"
    assert (tmp_path / "memory" / "agents" / "pr.command.zordon.md").is_file()


def test_agent_load_uses_master_index_for_legacy_history(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    legacy = tmp_path / "memory" / "agents" / "Zordon.md"
    legacy.parent.mkdir(parents=True)
    legacy.write_text(
        "# Agent Memory: Zordon\n\n"
        "- 2026-07-21T12:00:00Z | memory_uuid: `legacy-id` | "
        "agent: `pr.command.zordon` | session: `ses_legacy` | type: `message` | "
        "[[actions/2026/07/legacy-id]] | legacy memory\n",
        encoding="utf-8",
    )
    master = tmp_path / "memory" / "master.md"
    master.write_text(legacy.read_text(encoding="utf-8").replace("# Agent Memory: Zordon", "# Master Memory"), encoding="utf-8")
    parser = agent_memory.build_parser()
    args = parser.parse_args(
        [
            "load",
            "--root",
            str(tmp_path),
            "--source",
            "agent",
            "--agent-id",
            "pr.command.zordon",
            "--format",
            "json",
        ]
    )

    assert args.func(args) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload[0]["memory_uuid"] == "legacy-id"


def test_search_preserves_markdown_headings_inside_message(tmp_path: Path) -> None:
    write_action(
        tmp_path,
        memory_uuid="heading-id",
        message="Before\n\n## User heading\n\nneedle after heading",
    )

    results = agent_memory.search_action_documents(tmp_path, "needle")

    assert results[0].memory_uuid == "heading-id"


def test_search_compares_timezone_aware_timestamps(tmp_path: Path) -> None:
    write_action(
        tmp_path,
        memory_uuid="offset-id",
        timestamp="2026-07-21T12:00:00+02:00",
        message="timezone memory",
    )

    results = agent_memory.search_action_documents(
        tmp_path,
        "timezone",
        since="2026-07-21T09:59:59Z",
        until="2026-07-21T10:00:01Z",
    )

    assert results[0].memory_uuid == "offset-id"


def test_search_redacts_historical_secret_fields(tmp_path: Path) -> None:
    write_action(
        tmp_path,
        memory_uuid="secret-id",
        title="api_key: LEAKME123",
        summary="password: NeverPrintThis",
        message="safe searchable marker",
    )

    results = agent_memory.search_action_documents(tmp_path, "safe searchable")

    rendered = json.dumps(results[0].as_dict())
    assert "LEAKME123" not in rendered
    assert "NeverPrintThis" not in rendered


def test_search_normalizes_terminal_controls_in_snippets(tmp_path: Path) -> None:
    write_action(
        tmp_path,
        memory_uuid="control-id",
        message="needle\nforged row\x1b[31m",
    )

    result = agent_memory.search_action_documents(tmp_path, "needle")[0]

    assert "\n" not in result.snippet
    assert "\x1b" not in result.snippet


def test_search_bounds_unique_query_terms(tmp_path: Path) -> None:
    query = " ".join(f"term{index}" for index in range(33))
    with pytest.raises(ValueError, match="too many unique terms"):
        agent_memory.search_action_documents(tmp_path, query)


def test_validate_reports_escaping_links_instead_of_crashing(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    action = write_action(tmp_path, memory_uuid="unsafe-link-id")
    action.write_text(action.read_text(encoding="utf-8") + "[[../outside]]\n", encoding="utf-8")
    parser = agent_memory.build_parser()
    args = parser.parse_args(["validate", "--root", str(tmp_path), "--strict"])

    assert args.func(args) == 1
    assert "unsafe_memory_link" in capsys.readouterr().out


def test_log_allows_safe_display_name_with_spaces(tmp_path: Path) -> None:
    parser = agent_memory.build_parser()
    args = parser.parse_args(
        [
            "log",
            "--root",
            str(tmp_path),
            "--agent-id",
            "pr.command.zordon",
            "--agent-name",
            "Zordon Command Agent",
            "--session-id",
            "ses_test",
            "--message",
            "I will preserve display name compatibility",
        ]
    )

    assert args.func(args) == 0


def test_log_redacts_explicit_title_and_summary(tmp_path: Path) -> None:
    parser = agent_memory.build_parser()
    args = parser.parse_args(
        [
            "log",
            "--root",
            str(tmp_path),
            "--agent-id",
            "pr.command.zordon",
            "--session-id",
            "ses_test",
            "--title",
            "api_key: LEAKME123",
            "--summary",
            "password: NeverPrintThis",
            "--message",
            "I will test explicit metadata redaction",
        ]
    )

    assert args.func(args) == 0
    action = next((tmp_path / "actions").glob("*/*/*.md"))
    persisted = action.read_text(encoding="utf-8")
    assert "LEAKME123" not in persisted
    assert "NeverPrintThis" not in persisted
    assert "[REDACTED]" in persisted


def test_search_rejects_invalid_time_bound(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="invalid since timestamp"):
        agent_memory.search_action_documents(tmp_path, "memory", since="not-a-time")


def test_search_skips_document_with_invalid_timestamp(tmp_path: Path) -> None:
    write_action(tmp_path, memory_uuid="invalid-time-id", timestamp="not-a-time", message="needle")
    assert agent_memory.search_action_documents(tmp_path, "needle") == []


def test_redaction_masks_long_alphabetic_identifiers() -> None:
    identifier = "`AgentMemoryCanonicalIdentifierName`"
    redacted, touched = agent_memory.redact_text(identifier)
    assert redacted == "`[REDACTED_TOKEN]`"
    assert touched is True


def test_redaction_still_masks_long_token_like_values() -> None:
    token = "abcDEF0123456789abcDEF0123456789"
    redacted, touched = agent_memory.redact_text(token)
    assert redacted == "[REDACTED_TOKEN]"
    assert touched is True


def test_redaction_masks_long_lowercase_alphabetic_token() -> None:
    token = "abcdefghijklmnopqrstuvwxyzabcdefghijklmn"
    redacted, touched = agent_memory.redact_text(token)
    assert redacted == "[REDACTED_TOKEN]"
    assert touched is True


def test_agent_ids_cannot_collide_through_lossy_normalization(tmp_path: Path) -> None:
    parser = agent_memory.build_parser()
    args = parser.parse_args(
        [
            "log",
            "--root",
            str(tmp_path),
            "--agent-id",
            "victim!",
            "--session-id",
            "ses_test",
            "--message",
            "I will reject colliding identity",
        ]
    )
    with pytest.raises(ValueError):
        args.func(args)


def test_search_never_treats_tool_output_as_full_message(tmp_path: Path) -> None:
    parser = agent_memory.build_parser()
    args = parser.parse_args(
        [
            "log",
            "--root",
            str(tmp_path),
            "--agent-id",
            "pr.command.zordon",
            "--session-id",
            "ses_test",
            "--message",
            "I will record safe content",
            "--tool-name",
            "test",
            "--tool-output",
            "## Tool Outputs (summarized)\nneedle-only-in-tool-output",
        ]
    )
    assert args.func(args) == 0
    assert agent_memory.search_action_documents(tmp_path, "needle only tool output") == []


def test_search_text_output_sanitizes_control_characters_in_path(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    original = write_action(tmp_path, memory_uuid="path-control-id", message="path needle")
    original.rename(original.with_name("evil\nFORGED\x1b[31m.md"))
    parser = agent_memory.build_parser()
    args = parser.parse_args(["search", "path needle", "--root", str(tmp_path)])

    assert args.func(args) == 0
    output = capsys.readouterr().out
    assert "\x1b" not in output
    assert "\nFORGED" not in output


def test_redaction_masks_30_character_token() -> None:
    token = "abcDEF0123456789abcDEF01234567"
    redacted, touched = agent_memory.redact_text(token)
    assert redacted == "[REDACTED_TOKEN]"
    assert touched is True


def test_redaction_masks_long_path_segment() -> None:
    path = "/home/user/AgentMemoryCanonicalIdentifierName/file.md"
    redacted, touched = agent_memory.redact_text(path)
    assert "AgentMemoryCanonicalIdentifierName" not in redacted
    assert "[REDACTED_TOKEN]" in redacted
    assert touched is True


def test_agent_load_filters_before_deduplicating_master_entries(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    agent_dir = tmp_path / "memory" / "agents"
    agent_dir.mkdir(parents=True)
    master = tmp_path / "memory" / "master.md"
    master.write_text(
        "# Master Memory\n\n"
        "- 2026-07-21T12:00:00Z | memory_uuid: `shared` | agent: `other.agent` | "
        "session: `other` | type: `message` | [[actions/other]] | other\n"
        "- 2026-07-21T12:01:00Z | memory_uuid: `shared` | agent: `pr.command.zordon` | "
        "session: `wanted` | type: `message` | [[actions/wanted]] | wanted\n",
        encoding="utf-8",
    )
    parser = agent_memory.build_parser()
    args = parser.parse_args(
        ["load", "--root", str(tmp_path), "--source", "agent", "--agent-id", "pr.command.zordon", "--format", "json"]
    )

    assert args.func(args) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload[0]["session"] == "wanted"


def test_agent_load_never_previews_raw_artifacts(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    raw = tmp_path / "actions" / "2026" / "07" / "raw" / "secret.raw"
    raw.parent.mkdir(parents=True)
    raw.write_text("RAW_SECRET_VALUE", encoding="utf-8")
    agent_dir = tmp_path / "memory" / "agents"
    agent_dir.mkdir(parents=True)
    (tmp_path / "memory" / "master.md").write_text(
        "# Master Memory\n\n"
        "- 2026-07-21T12:00:00Z | memory_uuid: `raw-link` | agent: `pr.command.zordon` | "
        "session: `ses_test` | type: `message` | [[actions/2026/07/raw/secret.raw]] | unsafe\n",
        encoding="utf-8",
    )
    parser = agent_memory.build_parser()
    args = parser.parse_args(
        [
            "load", "--root", str(tmp_path), "--source", "agent", "--agent-id", "pr.command.zordon",
            "--with-action", "--format", "json",
        ]
    )

    assert args.func(args) == 0
    payload = json.loads(capsys.readouterr().out)
    rendered = json.dumps(payload)
    assert "RAW_SECRET_VALUE" not in rendered
    assert payload[0]["action_path"].endswith("secret.raw")
    assert "unsafe action link" in payload[0]["action_error"]


def test_search_preserves_legacy_reserved_heading_inside_message(tmp_path: Path) -> None:
    write_action(
        tmp_path,
        memory_uuid="legacy-heading-id",
        message="before\n\n## Tool Outputs (summarized)\n\nlegacy needle after heading",
    )
    results = agent_memory.search_action_documents(tmp_path, "legacy needle")
    assert results[0].memory_uuid == "legacy-heading-id"


def test_legacy_full_message_collision_never_merges_summary_and_message(tmp_path: Path) -> None:
    write_action(
        tmp_path,
        memory_uuid="legacy-full-message-id",
        summary="before\n\n## Full Message\n\nsummaryneedle",
        message="messageneedle",
    )
    assert agent_memory.search_action_documents(tmp_path, "summaryneedle messageneedle") == []


def test_load_text_output_sanitizes_untrusted_index_fields(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    agent_dir = tmp_path / "memory" / "agents"
    agent_dir.mkdir(parents=True)
    (tmp_path / "memory" / "master.md").write_text(
        "# Master Memory\n\n"
        "- 2026-07-21T12:00:00Z | memory_uuid: `safe-id` | agent: `pr.command.zordon` | "
        "session: `ses_test` | type: `message` | [[actions/missing]] | forged\x1b[2J\n",
        encoding="utf-8",
    )
    parser = agent_memory.build_parser()
    args = parser.parse_args(
        ["load", "--root", str(tmp_path), "--source", "agent", "--agent-id", "pr.command.zordon"]
    )
    assert args.func(args) == 0
    assert "\x1b" not in capsys.readouterr().out


def test_redaction_masks_hyphenated_token() -> None:
    token = "abcd-efgh-ijkl-mnop-qrst-uvwx"
    redacted, touched = agent_memory.redact_text(token)
    assert redacted == "[REDACTED_TOKEN]"
    assert touched is True


def test_multiline_title_cannot_inject_summary_boundary(tmp_path: Path) -> None:
    parser = agent_memory.build_parser()
    args = parser.parse_args(
        [
            "log", "--root", str(tmp_path), "--agent-id", "pr.command.zordon",
            "--session-id", "ses_test", "--title", "titleterm\n\n## Summary\n\ninjected",
            "--summary", "summaryterm", "--message", "message only",
        ]
    )
    assert args.func(args) == 0
    assert agent_memory.search_action_documents(tmp_path, "titleterm summaryterm") == []


@pytest.mark.parametrize("option", ["--model", "--provider", "--variant"])
def test_log_rejects_multiline_front_matter_values(tmp_path: Path, option: str) -> None:
    parser = agent_memory.build_parser()
    args = parser.parse_args(
        [
            "log", "--root", str(tmp_path), "--agent-id", "pr.command.zordon",
            "--session-id", "ses_test", option, "safe\n---\n## Summary\nforged",
            "--message", "I will reject metadata injection",
        ]
    )
    with pytest.raises(ValueError, match="single-line"):
        args.func(args)


def test_index_sync_adds_updates_and_removes_canonical_documents(tmp_path: Path) -> None:
    first = write_action(tmp_path, memory_uuid="first-id", message="first indexed memory")
    write_action(tmp_path, memory_uuid="second-id", message="second indexed memory")

    initial = agent_memory.sync_memory_index(tmp_path)
    assert initial == {"added": 2, "updated": 0, "removed": 0, "unchanged": 0, "recovered": False}
    assert agent_memory.memory_index_status(tmp_path)["document_count"] == 2

    unchanged = agent_memory.sync_memory_index(tmp_path)
    assert unchanged["unchanged"] == 2

    first.write_text(first.read_text(encoding="utf-8").replace("first indexed", "updated indexed"), encoding="utf-8")
    (tmp_path / "actions" / "2026" / "07" / "event-second-id.md").unlink()
    changed = agent_memory.sync_memory_index(tmp_path)
    assert changed["updated"] == 1
    assert changed["removed"] == 1
    assert agent_memory.memory_index_status(tmp_path)["document_count"] == 1


def test_indexed_search_matches_filters_and_excludes_raw(tmp_path: Path) -> None:
    write_action(
        tmp_path,
        memory_uuid="indexed-id",
        agent_id="pr.command.zordon",
        session_id="ses_indexed",
        event_type="checkpoint",
        message="durable sqlite retrieval",
    )
    raw = tmp_path / "actions" / "2026" / "07" / "raw" / "secret.raw"
    raw.parent.mkdir(parents=True)
    raw.write_text("sqlite secret", encoding="utf-8")
    agent_memory.sync_memory_index(tmp_path)

    results = agent_memory.search_indexed_documents(
        tmp_path,
        "sqlite retrieval",
        agent_id="pr.command.zordon",
        session_id="ses_indexed",
        event_type="checkpoint",
    )

    assert [result.memory_uuid for result in results] == ["indexed-id"]
    assert all("secret.raw" not in result.relative_path for result in results)


def test_auto_search_falls_back_when_index_is_missing_or_corrupt(tmp_path: Path) -> None:
    write_action(tmp_path, memory_uuid="fallback-id", message="fallback memory")
    assert agent_memory.search_memories(tmp_path, "fallback", mode="auto")[0].memory_uuid == "fallback-id"

    index_path = agent_memory.memory_index_path(tmp_path)
    index_path.parent.mkdir(parents=True, exist_ok=True)
    index_path.write_bytes(b"not sqlite")
    assert agent_memory.search_memories(tmp_path, "fallback", mode="auto")[0].memory_uuid == "fallback-id"


def test_index_sync_recovers_from_corruption(tmp_path: Path) -> None:
    write_action(tmp_path, memory_uuid="recover-id", message="recoverable memory")
    index_path = agent_memory.memory_index_path(tmp_path)
    index_path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.chmod(tmp_path / "memory", 0o700)
    os.chmod(index_path.parent, 0o700)
    index_path = agent_memory.memory_index_path(tmp_path)
    index_path.write_bytes(b"not sqlite")

    result = agent_memory.sync_memory_index(tmp_path)

    assert result["recovered"] is True
    assert agent_memory.memory_index_status(tmp_path)["ok"] is True
    assert agent_memory.search_indexed_documents(tmp_path, "recoverable")[0].memory_uuid == "recover-id"


def test_index_cli_sync_status_and_rebuild(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    write_action(tmp_path, memory_uuid="cli-index-id", message="cli indexed memory")
    parser = agent_memory.build_parser()

    sync_args = parser.parse_args(["index", "sync", "--root", str(tmp_path), "--format", "json"])
    assert sync_args.func(sync_args) == 0
    assert json.loads(capsys.readouterr().out)["added"] == 1

    status_args = parser.parse_args(["index", "status", "--root", str(tmp_path), "--format", "json"])
    assert status_args.func(status_args) == 0
    assert json.loads(capsys.readouterr().out)["document_count"] == 1

    rebuild_args = parser.parse_args(["index", "rebuild", "--root", str(tmp_path), "--format", "json"])
    assert rebuild_args.func(rebuild_args) == 0
    assert json.loads(capsys.readouterr().out)["added"] == 1


def test_auto_search_synchronizes_a_healthy_stale_index(tmp_path: Path) -> None:
    write_action(tmp_path, memory_uuid="existing-id", message="existing memory")
    agent_memory.sync_memory_index(tmp_path)
    write_action(tmp_path, memory_uuid="new-id", message="newly logged memory")
    assert agent_memory.search_memories(tmp_path, "newly logged", mode="auto")[0].memory_uuid == "new-id"


def test_indexed_search_matches_unicode_casefold_semantics(tmp_path: Path) -> None:
    write_action(tmp_path, memory_uuid="unicode-id", message="Straße memory")
    agent_memory.sync_memory_index(tmp_path)
    assert agent_memory.search_indexed_documents(tmp_path, "strasse")[0].memory_uuid == "unicode-id"


def test_index_status_and_sync_detect_missing_fts_rows(tmp_path: Path) -> None:
    write_action(tmp_path, memory_uuid="drift-id", message="drift memory")
    agent_memory.sync_memory_index(tmp_path)
    with sqlite3.connect(agent_memory.memory_index_path(tmp_path)) as connection:
        connection.execute("DELETE FROM documents_fts WHERE memory_uuid = 'drift-id'")
        connection.commit()

    assert agent_memory.memory_index_status(tmp_path)["ok"] is False
    repaired = agent_memory.sync_memory_index(tmp_path)
    assert repaired["updated"] == 1
    assert agent_memory.search_indexed_documents(tmp_path, "drift")[0].memory_uuid == "drift-id"


def test_indexed_search_rejects_reversed_time_range(tmp_path: Path) -> None:
    write_action(tmp_path, memory_uuid="time-id", message="time memory")
    agent_memory.sync_memory_index(tmp_path)
    with pytest.raises(ValueError, match="since timestamp must not be after"):
        agent_memory.search_indexed_documents(
            tmp_path,
            "time",
            since="2026-07-22T00:00:00Z",
            until="2026-07-21T00:00:00Z",
        )


def test_index_rejects_symlinked_state_directory(tmp_path: Path) -> None:
    outside = tmp_path.parent / "outside-index-state"
    outside.mkdir()
    memory = tmp_path / "memory"
    memory.mkdir()
    (memory / ".state").symlink_to(outside, target_is_directory=True)
    with pytest.raises(ValueError, match="symlink"):
        agent_memory.sync_memory_index(tmp_path)
    assert list(outside.iterdir()) == []


def test_index_rejects_hardlinked_database(tmp_path: Path) -> None:
    write_action(tmp_path, memory_uuid="hardlink-id", message="hardlink memory")
    agent_memory.sync_memory_index(tmp_path)
    index_path = agent_memory.memory_index_path(tmp_path)
    other = tmp_path / "other.sqlite3"
    os.link(index_path, other)
    with pytest.raises(ValueError, match="hardlink"):
        agent_memory.sync_memory_index(tmp_path)


def test_index_paths_with_uri_characters_work(tmp_path: Path) -> None:
    root = tmp_path / "project?#name"
    write_action(root, memory_uuid="uri-id", message="uri path memory")
    agent_memory.sync_memory_index(root)
    assert agent_memory.memory_index_status(root)["ok"] is True
    assert agent_memory.search_indexed_documents(root, "uri path")[0].memory_uuid == "uri-id"


def test_lock_contention_does_not_delete_healthy_index(tmp_path: Path) -> None:
    write_action(tmp_path, memory_uuid="locked-id", message="locked memory")
    agent_memory.sync_memory_index(tmp_path)
    index_path = agent_memory.memory_index_path(tmp_path)
    connection = sqlite3.connect(index_path)
    connection.execute("BEGIN EXCLUSIVE")
    try:
        with pytest.raises(sqlite3.OperationalError):
            agent_memory.sync_memory_index(tmp_path)
        with pytest.raises(sqlite3.OperationalError):
            agent_memory.rebuild_memory_index(tmp_path)
        assert index_path.exists()
    finally:
        connection.rollback()
        connection.close()


def test_index_filters_before_result_truncation(tmp_path: Path) -> None:
    agent_memory.sync_memory_index(tmp_path)
    with sqlite3.connect(agent_memory.memory_index_path(tmp_path)) as connection:
        ordinary = []
        for index in range(5001):
            row = (
                f"ordinary-{index}", f"actions/{index}.md", f"hash-{index}",
                "2026-07-21T12:00:00Z", "other.agent", "ses_other", "message",
                "", "", "needle",
            )
            row_hash = agent_memory._indexed_row_hash(
                (row[2], row[1], *row[3:])
            )
            ordinary.append((*row, row_hash))
        connection.executemany(
            "INSERT INTO documents VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)", ordinary
        )
        connection.executemany(
            "INSERT INTO documents_fts(memory_uuid, title, summary, message) VALUES (?, '', '', 'needle')",
            [(row[0],) for row in ordinary],
        )
        target_base = (
            "target-id", "actions/target.md", "target-hash", "2026-07-21T12:01:00Z",
            "pr.command.zordon", "ses_target", "message", "", "", "needle",
        )
        target = (
            *target_base,
            agent_memory._indexed_row_hash((target_base[2], target_base[1], *target_base[3:])),
        )
        connection.execute("INSERT INTO documents VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)", target)
        connection.execute(
            "INSERT INTO documents_fts(memory_uuid, title, summary, message) VALUES (?, '', '', 'needle')",
            (target[0],),
        )
        connection.commit()

    results = agent_memory.search_indexed_documents(
        tmp_path, "needle", agent_id="pr.command.zordon"
    )
    assert [result.memory_uuid for result in results] == ["target-id"]


def test_index_file_is_private(tmp_path: Path) -> None:
    agent_memory.sync_memory_index(tmp_path)
    mode = agent_memory.memory_index_path(tmp_path).stat().st_mode & 0o777
    assert mode == 0o600


def test_sync_repairs_modified_and_orphaned_fts_payloads(tmp_path: Path) -> None:
    write_action(tmp_path, memory_uuid="payload-id", message="canonical payload memory")
    agent_memory.sync_memory_index(tmp_path)
    with sqlite3.connect(agent_memory.memory_index_path(tmp_path)) as connection:
        connection.execute(
            "UPDATE documents_fts SET message = 'tampered' WHERE memory_uuid = 'payload-id'"
        )
        connection.execute(
            "INSERT INTO documents_fts(memory_uuid, title, summary, message) VALUES ('orphan', '', '', 'orphan')"
        )
        connection.commit()

    assert agent_memory.memory_index_status(tmp_path)["ok"] is False
    repaired = agent_memory.sync_memory_index(tmp_path)
    assert repaired["updated"] == 1
    assert agent_memory.memory_index_status(tmp_path)["ok"] is True
    assert agent_memory.search_indexed_documents(tmp_path, "canonical payload")[0].memory_uuid == "payload-id"


def test_indexed_search_handles_underscore_tokens_like_scan(tmp_path: Path) -> None:
    write_action(tmp_path, memory_uuid="underscore-id", message="literal _ marker")
    agent_memory.sync_memory_index(tmp_path)
    indexed = agent_memory.search_indexed_documents(tmp_path, "_")
    scanned = agent_memory.search_action_documents(tmp_path, "_")
    assert [item.memory_uuid for item in indexed] == [item.memory_uuid for item in scanned]


def test_explicit_index_search_rejects_symlinked_state_directory(tmp_path: Path) -> None:
    external_root = tmp_path / "external"
    write_action(external_root, memory_uuid="external-id", message="external secret memory")
    agent_memory.sync_memory_index(external_root)
    local_root = tmp_path / "local"
    (local_root / "memory").mkdir(parents=True)
    (local_root / "memory" / ".state").symlink_to(
        external_root / "memory" / ".state", target_is_directory=True
    )
    with pytest.raises(ValueError, match="symlink"):
        agent_memory.search_indexed_documents(local_root, "external secret")


def test_sync_repairs_relational_document_drift(tmp_path: Path) -> None:
    write_action(
        tmp_path,
        memory_uuid="row-drift-id",
        agent_id="pr.command.zordon",
        message="relational drift memory",
    )
    agent_memory.sync_memory_index(tmp_path)
    with sqlite3.connect(agent_memory.memory_index_path(tmp_path)) as connection:
        connection.execute(
            "UPDATE documents SET agent = 'tampered.agent', title = 'tampered' "
            "WHERE memory_uuid = 'row-drift-id'"
        )
        connection.commit()
    assert agent_memory.memory_index_status(tmp_path)["ok"] is False
    repaired = agent_memory.sync_memory_index(tmp_path)
    assert repaired["updated"] == 1
    assert agent_memory.memory_index_status(tmp_path)["ok"] is True
    assert agent_memory.search_indexed_documents(
        tmp_path, "relational drift", agent_id="pr.command.zordon"
    )[0].memory_uuid == "row-drift-id"


def test_indexed_search_falls_back_for_non_fts_word_characters(tmp_path: Path) -> None:
    write_action(tmp_path, memory_uuid="unicode-word-id", message="marker ᦰ memory")
    agent_memory.sync_memory_index(tmp_path)
    indexed = agent_memory.search_indexed_documents(tmp_path, "ᦰ")
    scanned = agent_memory.search_action_documents(tmp_path, "ᦰ")
    assert [item.memory_uuid for item in indexed] == [item.memory_uuid for item in scanned]


def test_fts_uses_python_word_boundaries_for_ascii_queries(tmp_path: Path) -> None:
    write_action(tmp_path, memory_uuid="boundary-id", message="needle\uE000 marker")
    agent_memory.sync_memory_index(tmp_path)
    indexed = agent_memory.search_indexed_documents(tmp_path, "needle")
    scanned = agent_memory.search_action_documents(tmp_path, "needle")
    assert [item.memory_uuid for item in indexed] == [item.memory_uuid for item in scanned]


def test_status_and_sync_recover_fts_internal_corruption(tmp_path: Path) -> None:
    write_action(tmp_path, memory_uuid="fts-corrupt-id", message="fts corruption memory")
    agent_memory.sync_memory_index(tmp_path)
    with sqlite3.connect(agent_memory.memory_index_path(tmp_path)) as connection:
        connection.execute(
            "DELETE FROM documents_fts_data WHERE id = (SELECT MAX(id) FROM documents_fts_data)"
        )
        connection.commit()
    assert agent_memory.memory_index_status(tmp_path)["ok"] is False
    repaired = agent_memory.sync_memory_index(tmp_path)
    assert repaired["recovered"] is True
    assert agent_memory.search_indexed_documents(tmp_path, "fts corruption")[0].memory_uuid == "fts-corrupt-id"


def test_canonical_wal_sidecar_forces_safe_scan_fallback(tmp_path: Path) -> None:
    write_action(tmp_path, memory_uuid="wal-id", message="canonical wal memory")
    agent_memory.sync_memory_index(tmp_path)
    connection = sqlite3.connect(agent_memory.memory_index_path(tmp_path))
    connection.execute("PRAGMA journal_mode = WAL")
    connection.execute(
        "UPDATE documents SET message = 'stale wal payload' WHERE memory_uuid = 'wal-id'"
    )
    connection.commit()
    try:
        assert agent_memory.memory_index_status(tmp_path)["ok"] is False
        with pytest.raises(ValueError, match="sidecar"):
            agent_memory.search_indexed_documents(tmp_path, "canonical wal")
        assert agent_memory.search_memories(tmp_path, "canonical wal", mode="auto")[0].memory_uuid == "wal-id"
    finally:
        connection.close()


def test_auto_falls_back_if_sidecar_appears_after_status(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    write_action(tmp_path, memory_uuid="wal-race-id", message="wal race canonical")
    agent_memory.sync_memory_index(tmp_path)
    connection = sqlite3.connect(agent_memory.memory_index_path(tmp_path))
    connection.execute("PRAGMA journal_mode = WAL")
    connection.execute("UPDATE documents SET message = 'stale' WHERE memory_uuid = 'wal-race-id'")
    connection.commit()
    monkeypatch.setattr(agent_memory, "memory_index_status", lambda root: {"ok": True})
    try:
        result = agent_memory.search_memories(tmp_path, "wal race canonical", mode="auto")
        assert result[0].memory_uuid == "wal-race-id"
    finally:
        connection.close()


def test_failed_post_replace_step_restores_corrupt_original(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    write_action(tmp_path, memory_uuid="restore-id", message="restore memory")
    index_path = agent_memory.memory_index_path(tmp_path)
    index_path.parent.mkdir(parents=True)
    original = b"corrupt original bytes"
    index_path.write_bytes(original)
    real_fchmod = os.fchmod

    def fail_regular_file(descriptor: int, mode: int) -> None:
        if os.path.isfile(f"/proc/self/fd/{descriptor}"):
            raise OSError("injected chmod failure")
        real_fchmod(descriptor, mode)

    monkeypatch.setattr(agent_memory.os, "fchmod", fail_regular_file)
    with pytest.raises(OSError, match="injected chmod failure"):
        agent_memory.sync_memory_index(tmp_path)
    assert index_path.read_bytes() == original


def test_shared_memory_root_uses_private_per_user_index(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "shared-project"
    write_action(root, memory_uuid="shared-id", message="shared root memory")
    user_state = tmp_path / "user-state"
    monkeypatch.setenv("XDG_STATE_HOME", str(user_state))
    monkeypatch.setattr(agent_memory, "_directory_owned_by_current_user", lambda path: False)

    result = agent_memory.sync_memory_index(root)
    index_path = agent_memory.memory_index_path(root)

    assert result["added"] == 1
    assert index_path.is_relative_to(user_state)
    assert index_path.stat().st_mode & 0o777 == 0o600


def test_alternate_fts_corruption_message_is_recoverable() -> None:
    error = sqlite3.DatabaseError("fts5: corruption found reading blob")
    assert agent_memory._is_sqlite_corruption(error) is True


def test_relative_xdg_state_home_is_ignored_for_shared_root(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "shared"
    root.mkdir()
    monkeypatch.setattr(agent_memory, "_directory_owned_by_current_user", lambda path: False)
    monkeypatch.setenv("XDG_STATE_HOME", "relative-state")
    assert agent_memory.memory_index_path(root).is_absolute()
    assert "relative-state" not in agent_memory.memory_index_path(root).parts


def test_symlinked_xdg_state_home_is_rejected(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "shared"
    root.mkdir()
    target = tmp_path / "target-state"
    target.mkdir()
    link = tmp_path / "state-link"
    link.symlink_to(target, target_is_directory=True)
    monkeypatch.setattr(agent_memory, "_directory_owned_by_current_user", lambda path: False)
    monkeypatch.setenv("XDG_STATE_HOME", str(link))
    with pytest.raises(ValueError, match="unsafe memory index directory component"):
        agent_memory.sync_memory_index(root)
    assert list(target.iterdir()) == []


def test_external_index_is_bound_to_project_inode(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "project"
    write_action(root, memory_uuid="old-project-id", message="old project memory")
    user_state = tmp_path / "state"
    monkeypatch.setenv("XDG_STATE_HOME", str(user_state))
    monkeypatch.setattr(agent_memory, "_directory_owned_by_current_user", lambda path: False)
    agent_memory.sync_memory_index(root)

    old_root = tmp_path / "old-project"
    root.rename(old_root)
    write_action(root, memory_uuid="new-project-id", message="new project memory")
    with pytest.raises(ValueError, match="project identity mismatch"):
        agent_memory.search_indexed_documents(root, "old project")
    recovered = agent_memory.sync_memory_index(root)
    assert recovered["recovered"] is True
    assert agent_memory.search_indexed_documents(root, "new project")[0].memory_uuid == "new-project-id"


def test_sync_rejects_root_replacement_during_discovery(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "project"
    write_action(root, memory_uuid="old-id", message="old memory")
    real_discover = agent_memory._discover_index_documents

    def replace_after_discovery(stable_root: Path):
        documents = real_discover(stable_root)
        root.rename(tmp_path / "old-project")
        write_action(root, memory_uuid="new-id", message="new memory")
        return documents

    monkeypatch.setattr(agent_memory, "_discover_index_documents", replace_after_discovery)
    with pytest.raises(ValueError, match="project root changed"):
        agent_memory.sync_memory_index(root)


@pytest.mark.parametrize(
    ("endpoint", "expected"),
    [
        ("http://127.0.0.1:11434", "http://127.0.0.1:11434"),
        ("http://[::1]:11434/", "http://[::1]:11434"),
    ],
)
def test_ollama_endpoint_accepts_canonical_loopback_literals(
    endpoint: str, expected: str
) -> None:
    assert agent_memory.validate_ollama_endpoint(endpoint) == expected


@pytest.mark.parametrize(
    "endpoint",
    [
        "https://127.0.0.1:11434",
        "http://localhost:11434",
        "http://192.168.1.2:11434",
        "http://127.0.0.1",
        "http://user@127.0.0.1:11434",
        "http://127.0.0.1:11434/api",
        "http://127.0.0.1:11434?query=yes",
        "http://127.0.0.1:11434#fragment",
        "http://2130706433:11434",
    ],
)
def test_ollama_endpoint_rejects_unsafe_or_ambiguous_urls(endpoint: str) -> None:
    with pytest.raises(ValueError, match="loopback"):
        agent_memory.validate_ollama_endpoint(endpoint)


def test_provider_fingerprint_is_stable_and_identity_complete() -> None:
    identity = agent_memory.ProviderFingerprint(
        provider="ollama",
        endpoint="http://127.0.0.1:11434",
        model="nomic-embed-text:v1.5",
        model_digest="sha256:abc123",
        server_version="0.11.4",
        document_prefix="search_document: ",
        query_prefix="search_query: ",
        chunker_version="canonical-fields-v1",
    )

    assert identity.digest() == replace(identity).digest()
    for field, changed in (
        ("provider", "other"),
        ("endpoint", "http://127.0.0.1:11435"),
        ("model", "other-model"),
        ("model_digest", "sha256:def456"),
        ("server_version", "0.11.5"),
        ("document_prefix", "doc: "),
        ("query_prefix", "query: "),
        ("chunker_version", "canonical-fields-v2"),
    ):
        assert replace(identity, **{field: changed}).digest() != identity.digest()


@pytest.mark.parametrize("field", ["model_digest", "server_version"])
def test_provider_fingerprint_rejects_missing_reported_identity(field: str) -> None:
    values = {
        "provider": "ollama",
        "endpoint": "http://127.0.0.1:11434",
        "model": "nomic-embed-text:v1.5",
        "model_digest": "sha256:abc123",
        "server_version": "0.11.4",
        "document_prefix": "search_document: ",
        "query_prefix": "search_query: ",
        "chunker_version": "canonical-fields-v1",
    }
    values[field] = ""
    with pytest.raises(ValueError, match=field.replace("_", " ")):
        agent_memory.ProviderFingerprint(**values)


def test_ollama_config_requires_explicit_loopback_endpoint_and_model() -> None:
    config = agent_memory.OllamaConfig(
        endpoint="http://127.0.0.1:11434/",
        model="nomic-embed-text:v1.5",
    )
    assert config.endpoint == "http://127.0.0.1:11434"
    assert config.model == "nomic-embed-text:v1.5"

    with pytest.raises(ValueError, match="model"):
        agent_memory.OllamaConfig(endpoint="http://127.0.0.1:11434", model="")
    with pytest.raises(ValueError, match="loopback"):
        agent_memory.OllamaConfig(endpoint="http://ollama.local:11434", model="model")


def test_discover_ollama_fingerprint_uses_reported_version_and_model_digest() -> None:
    calls = []

    def request_json(method: str, url: str, payload: dict | None, **limits):
        calls.append((method, url, payload, limits))
        if url.endswith("/api/version"):
            return {"version": "0.11.4"}
        return {
            "models": [
                {"name": "other:latest", "digest": "sha256:other"},
                {"name": "nomic-embed-text:v1.5", "digest": "sha256:expected"},
            ]
        }

    config = agent_memory.OllamaConfig(
        endpoint="http://127.0.0.1:11434",
        model="nomic-embed-text:v1.5",
    )
    fingerprint = agent_memory.discover_ollama_fingerprint(config, request_json=request_json)

    assert fingerprint.server_version == "0.11.4"
    assert fingerprint.model_digest == "sha256:expected"
    assert [(call[0], call[1], call[2]) for call in calls] == [
        ("GET", "http://127.0.0.1:11434/api/version", None),
        ("GET", "http://127.0.0.1:11434/api/tags", None),
    ]
    assert all(call[3] == {"timeout": 10.0, "max_bytes": 8_000_000} for call in calls)


def test_ollama_http_opener_disables_proxies_and_redirects() -> None:
    opener = agent_memory._build_ollama_opener()
    redirect_handler = next(
        handler
        for handler in opener.handlers
        if isinstance(handler, agent_memory._RejectRedirects)
    )

    assert not any(isinstance(handler, agent_memory.ProxyHandler) for handler in opener.handlers)
    with pytest.raises(agent_memory.HTTPError):
        redirect_handler.redirect_request(
            None,
            None,
            302,
            "redirect",
            {},
            "http://127.0.0.1:11434/elsewhere",
        )
