import json
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


def test_agent_load_does_not_trust_unrelated_legacy_indexes(
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
    assert json.loads(capsys.readouterr().out) == []


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


def test_redaction_preserves_long_alphabetic_identifiers() -> None:
    identifier = "AgentMemoryCanonicalIdentifierName"
    redacted, touched = agent_memory.redact_text(identifier)
    assert redacted == identifier
    assert touched is False


def test_redaction_still_masks_long_token_like_values() -> None:
    token = "abcDEF0123456789abcDEF0123456789"
    redacted, touched = agent_memory.redact_text(token)
    assert redacted == "[REDACTED_TOKEN]"
    assert touched is True
