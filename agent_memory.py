#!/usr/bin/env python3

import argparse
import datetime as dt
import json
import os
import re
import uuid
from dataclasses import dataclass
from pathlib import Path

try:
    import fcntl
except ImportError as exc:  # pragma: no cover
    raise RuntimeError("This tool currently requires POSIX file locking") from exc


DEFAULT_ROOT = Path.home() / "opencode"
SENSITIVE_PATTERNS = [
    re.compile(r"(?i)(authorization:\s*bearer\s+)[A-Za-z0-9._\-]+"),
    re.compile(r"(?i)(api[_-]?key\s*[:=]\s*)[A-Za-z0-9._\-]+"),
    re.compile(r"(?i)(token\s*[:=]\s*)[A-Za-z0-9._\-]+"),
    re.compile(r"(?i)(secret\s*[:=]\s*)[A-Za-z0-9._\-]+"),
    re.compile(r"(?i)(password\s*[:=]\s*)\S+"),
    re.compile(r"-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----[\s\S]*?-----END (?:RSA |EC |OPENSSH )?PRIVATE KEY-----"),
]

ACTION_TRIGGERS = [
    "i will",
    "i ran",
    "i updated",
    "i changed",
    "implement",
    "fix",
    "run test",
    "executed",
    "applied patch",
]


@dataclass
class IndexEntry:
    timestamp: str
    memory_uuid: str
    agent: str
    session: str
    event_type: str
    action_link: str
    summary: str


@dataclass
class ValidationIssue:
    kind: str
    path: str
    detail: str


def utc_now() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc)


def iso_ts(ts: dt.datetime) -> str:
    return ts.strftime("%Y-%m-%dT%H:%M:%SZ")


def compact_ts(ts: dt.datetime) -> str:
    return ts.strftime("%Y%m%dT%H%M%S") + f".{int(ts.microsecond / 1000):03d}"


def safe_name(value: str) -> str:
    cleaned = re.sub(r"[^A-Za-z0-9._-]", "-", value.strip())
    return re.sub(r"-+", "-", cleaned).strip("-") or "unknown"


def wiki_link(path: Path, root: Path) -> str:
    rel = path.relative_to(root).as_posix()
    if rel.endswith(".md"):
        rel = rel[:-3]
    return f"[[{rel}]]"


def link_to_path(link: str, root: Path) -> Path:
    stripped = link.strip()
    if stripped.startswith("[[") and stripped.endswith("]]"):
        stripped = stripped[2:-2]
    candidate = root / stripped
    if stripped.endswith(".md") or stripped.endswith(".raw"):
        return candidate
    md = Path(str(candidate) + ".md")
    if md.exists():
        return md
    raw = Path(str(candidate) + ".raw")
    if raw.exists():
        return raw
    return md


def extract_wiki_links(content: str) -> list[str]:
    return re.findall(r"\[\[([^\]]+)\]\]", content)


def ensure_layout(root: Path) -> None:
    (root / "memory" / "agents").mkdir(parents=True, exist_ok=True)
    (root / "memory" / "sessions").mkdir(parents=True, exist_ok=True)
    (root / "memory" / ".state").mkdir(parents=True, exist_ok=True)
    (root / "actions").mkdir(parents=True, exist_ok=True)
    master = root / "memory" / "master.md"
    if not master.exists():
        master.write_text("# Master Memory\n\n", encoding="utf-8")


def lock_file_for_update(path: Path):
    lock_path = path.with_suffix(path.suffix + ".lock")
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    lock_handle = lock_path.open("w", encoding="utf-8")
    fcntl.flock(lock_handle.fileno(), fcntl.LOCK_EX)
    return lock_handle


def append_line_locked(path: Path, line: str) -> None:
    lock_handle = lock_file_for_update(path)
    try:
        with path.open("a", encoding="utf-8") as handle:
            handle.write(line)
            handle.flush()
            os.fsync(handle.fileno())
    finally:
        fcntl.flock(lock_handle.fileno(), fcntl.LOCK_UN)
        lock_handle.close()


def write_text_locked(path: Path, content: str) -> None:
    lock_handle = lock_file_for_update(path)
    try:
        with path.open("w", encoding="utf-8") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
    finally:
        fcntl.flock(lock_handle.fileno(), fcntl.LOCK_UN)
        lock_handle.close()


def state_path(root: Path, session_id: str) -> Path:
    return root / "memory" / ".state" / f"{safe_name(session_id)}.json"


def load_state(root: Path, session_id: str) -> dict:
    path = state_path(root, session_id)
    if not path.exists():
        return {"phase": "context_building", "last_event": None}
    with path.open("r", encoding="utf-8") as handle:
        return json.load(handle)


def save_state(root: Path, session_id: str, state: dict) -> None:
    path = state_path(root, session_id)
    write_text_locked(path, json.dumps(state, indent=2, sort_keys=True) + "\n")


def redact_text(content: str) -> tuple[str, bool]:
    redacted = content
    touched = False
    for pattern in SENSITIVE_PATTERNS:
        replacement = r"\1[REDACTED]" if pattern.groups >= 1 else "[REDACTED]"
        redacted, count = pattern.subn(replacement, redacted)
        if count > 0:
            touched = True

    entropy_like = re.compile(r"\b[A-Za-z0-9+/=_-]{24,}\b")
    matches = set(entropy_like.findall(redacted))
    for token in matches:
        if token.count("-") >= 4:
            continue
        redacted = redacted.replace(token, "[REDACTED_TOKEN]")
        touched = True

    return redacted, touched


def summarize(text: str, max_lines: int = 8, max_chars: int = 800) -> str:
    lines = text.strip().splitlines()
    clipped = "\n".join(lines[:max_lines])
    if len(clipped) > max_chars:
        clipped = clipped[: max_chars - 3] + "..."
    return clipped or "(empty)"


def parse_message(args: argparse.Namespace) -> str:
    if args.message_file:
        return Path(args.message_file).read_text(encoding="utf-8")
    return args.message or ""


def parse_tool_output(args: argparse.Namespace) -> str:
    if args.tool_output_file:
        return Path(args.tool_output_file).read_text(encoding="utf-8")
    return args.tool_output or ""


def should_start_actions(message: str, tool_used: bool) -> bool:
    if tool_used:
        return True
    lower = message.lower()
    return any(trigger in lower for trigger in ACTION_TRIGGERS)


def build_index_line(
    ts: str,
    memory_uuid: str,
    agent_id: str,
    session_id: str,
    event_type: str,
    summary_text: str,
    action_link: str,
) -> str:
    brief = summarize(summary_text, max_lines=1, max_chars=140).replace("\n", " ")
    return (
        f"- {ts} | memory_uuid: `{memory_uuid}` | agent: `{agent_id}` | "
        f"session: `{session_id}` | type: `{event_type}` | {action_link} | {brief}\n"
    )


def parse_index_entry(line: str) -> IndexEntry | None:
    pattern = re.compile(
        r"^-\s+([^|]+)\|\s+memory_uuid:\s+`([^`]+)`\s+\|\s+agent:\s+`([^`]+)`\s+\|\s+"
        r"session:\s+`([^`]+)`\s+\|\s+type:\s+`([^`]+)`\s+\|\s+(\[\[[^\]]+\]\])\s+\|\s+(.*)$"
    )
    match = pattern.match(line.strip())
    if not match:
        return None
    return IndexEntry(
        timestamp=match.group(1).strip(),
        memory_uuid=match.group(2).strip(),
        agent=match.group(3).strip(),
        session=match.group(4).strip(),
        event_type=match.group(5).strip(),
        action_link=match.group(6).strip(),
        summary=match.group(7).strip(),
    )


def load_index_entries(path: Path) -> list[IndexEntry]:
    if not path.exists():
        return []
    entries: list[IndexEntry] = []
    with path.open("r", encoding="utf-8") as handle:
        for line in handle:
            entry = parse_index_entry(line)
            if entry:
                entries.append(entry)
    return entries


def command_init(args: argparse.Namespace) -> int:
    root = Path(args.root).expanduser().resolve()
    ensure_layout(root)
    print(f"Initialized memory layout under {root}")
    return 0


def command_log(args: argparse.Namespace) -> int:
    root = Path(args.root).expanduser().resolve()
    ensure_layout(root)

    agent_id = safe_name(args.agent_id)
    agent_name = safe_name(args.agent_name or agent_id)
    session_id = safe_name(args.session_id)
    event_type = safe_name(args.event_type)
    model = args.model or "unknown"
    provider = args.provider or "unknown"
    variant = args.variant or "unknown"

    message = parse_message(args)
    tool_output = parse_tool_output(args)
    tool_used = bool(args.tool_name or tool_output)

    message_redacted, message_masked = redact_text(message)
    tool_redacted, tool_masked = redact_text(tool_output)
    any_redacted = message_masked or tool_masked

    state = load_state(root, session_id)
    if state.get("phase") == "context_building" and should_start_actions(message_redacted, tool_used):
        state["phase"] = "actions"

    if state.get("phase") == "context_building":
        print("Skipped log event: still in context_building phase")
        return 0

    now = utc_now()
    ts_iso = iso_ts(now)
    ts_compact = compact_ts(now)
    year = now.strftime("%Y")
    month = now.strftime("%m")
    memory_uuid = args.memory_uuid or str(uuid.uuid4())

    action_dir = root / "actions" / year / month
    raw_dir = action_dir / "raw"
    action_dir.mkdir(parents=True, exist_ok=True)
    raw_dir.mkdir(parents=True, exist_ok=True)

    action_file = action_dir / f"{ts_compact}-{memory_uuid}.md"
    raw_link = ""
    raw_summary = ""
    if tool_redacted:
        raw_summary = summarize(tool_redacted)
        should_write_raw = args.raw_mode == "always" or len(tool_redacted) > args.raw_threshold
        if should_write_raw:
            raw_file = raw_dir / f"{ts_compact}-{memory_uuid}.raw"
            if raw_file.exists():
                raw_file = raw_dir / f"{ts_compact}-{memory_uuid}-{uuid.uuid4().hex[:8]}.raw"
            write_text_locked(raw_file, tool_redacted)
            raw_link = wiki_link(raw_file, root)

    master_file = root / "memory" / "master.md"
    agent_file = root / "memory" / "agents" / f"{agent_name}.md"
    session_file = root / "memory" / "sessions" / f"{session_id}.md"

    if not agent_file.exists():
        write_text_locked(agent_file, f"# Agent Memory: {agent_name}\n\n")
    if not session_file.exists():
        write_text_locked(session_file, f"# Session Memory: {session_id}\n\n")

    action_link = wiki_link(action_file, root)
    master_link = wiki_link(master_file, root)
    agent_link = wiki_link(agent_file, root)
    session_link = wiki_link(session_file, root)
    previous_link = ""
    if state.get("last_event"):
        previous_link = wiki_link(root / state["last_event"], root)

    title = args.title or f"{event_type} from {agent_name}"
    summary_text = args.summary or summarize(message_redacted, max_lines=3, max_chars=280)

    tool_lines = "- none"
    if tool_redacted:
        tool_lines = f"- tool: `{args.tool_name or 'unknown'}`\n- summary: {raw_summary}"
        if raw_link:
            tool_lines += f"\n- raw: {raw_link}"

    lines = [
        "---",
        f"memory_uuid: {memory_uuid}",
        f"timestamp: {ts_iso}",
        f"session_id: {session_id}",
        f"agent_id: {agent_id}",
        f"agent_name: {agent_name}",
        f"model: {model}",
        f"provider: {provider}",
        f"variant: {variant}",
        f"event_type: {event_type}",
        "phase: actions",
        f"redaction: {str(any_redacted).lower()}",
        "---",
        f"# {title}",
        "",
        "## Links",
        f"- master: {master_link}",
        f"- agent: {agent_link}",
        f"- session: {session_link}",
    ]
    if previous_link:
        lines.append(f"- previous: {previous_link}")
    lines.extend(
        [
            "",
            "## Summary",
            summary_text,
            "",
            "## Full Message",
            message_redacted or "(empty)",
            "",
            "## Tool Outputs (summarized)",
            tool_lines,
            "",
            "## Raw Output Links",
            raw_link or "- none",
            "",
        ]
    )

    write_text_locked(action_file, "\n".join(lines))

    line = build_index_line(ts_iso, memory_uuid, agent_id, session_id, event_type, summary_text, action_link)
    append_line_locked(master_file, line)
    append_line_locked(agent_file, line)
    append_line_locked(session_file, line)

    state["last_event"] = action_file.relative_to(root).as_posix()
    save_state(root, session_id, state)

    print(f"Logged {action_link}")
    return 0


def command_load(args: argparse.Namespace) -> int:
    root = Path(args.root).expanduser().resolve()
    ensure_layout(root)

    source = args.source
    if source == "master":
        path = root / "memory" / "master.md"
    elif source == "agent":
        if not args.agent_id:
            raise SystemExit("--agent-id is required when --source agent")
        path = root / "memory" / "agents" / f"{safe_name(args.agent_id)}.md"
    else:
        if not args.session_id:
            raise SystemExit("--session-id is required when --source session")
        path = root / "memory" / "sessions" / f"{safe_name(args.session_id)}.md"

    entries = load_index_entries(path)

    if args.agent_id:
        entries = [entry for entry in entries if entry.agent == safe_name(args.agent_id)]
    if args.session_id:
        entries = [entry for entry in entries if entry.session == safe_name(args.session_id)]
    if args.event_type:
        entries = [entry for entry in entries if entry.event_type == safe_name(args.event_type)]

    if args.since:
        entries = [entry for entry in entries if entry.timestamp >= args.since]
    if args.until:
        entries = [entry for entry in entries if entry.timestamp <= args.until]

    entries.sort(key=lambda item: item.timestamp, reverse=args.reverse)
    if args.limit and args.limit > 0:
        entries = entries[: args.limit]

    if args.format == "json":
        payload = []
        for entry in entries:
            row = {
                "timestamp": entry.timestamp,
                "memory_uuid": entry.memory_uuid,
                "agent": entry.agent,
                "session": entry.session,
                "event_type": entry.event_type,
                "action_link": entry.action_link,
                "summary": entry.summary,
            }
            if args.with_action:
                action_path = link_to_path(entry.action_link, root)
                row["action_path"] = action_path.as_posix()
                if action_path.exists():
                    row["action_preview"] = summarize(action_path.read_text(encoding="utf-8"), max_lines=12, max_chars=1600)
            payload.append(row)
        print(json.dumps(payload, indent=2))
        return 0

    for entry in entries:
        print(
            f"{entry.timestamp} | {entry.memory_uuid} | {entry.agent} | "
            f"{entry.session} | {entry.event_type} | {entry.action_link} | {entry.summary}"
        )
        if args.with_action:
            action_path = link_to_path(entry.action_link, root)
            if action_path.exists():
                preview = summarize(action_path.read_text(encoding='utf-8'), max_lines=12, max_chars=1200)
                print(f"  action: {action_path}")
                print(f"  preview: {preview}")
            else:
                print(f"  action: missing ({action_path})")

    return 0


def command_validate(args: argparse.Namespace) -> int:
    root = Path(args.root).expanduser().resolve()
    ensure_layout(root)

    issues: list[ValidationIssue] = []
    referenced_actions: set[Path] = set()
    referenced_raw: set[Path] = set()

    index_paths = [root / "memory" / "master.md"]
    index_paths.extend(sorted((root / "memory" / "agents").glob("*.md")))
    index_paths.extend(sorted((root / "memory" / "sessions").glob("*.md")))

    for index_path in index_paths:
        entries = load_index_entries(index_path)
        for entry in entries:
            action_path = link_to_path(entry.action_link, root)
            referenced_actions.add(action_path)
            if not action_path.exists():
                issues.append(
                    ValidationIssue(
                        kind="missing_action_link",
                        path=index_path.as_posix(),
                        detail=f"{entry.memory_uuid} -> {entry.action_link}",
                    )
                )

    action_files = sorted((root / "actions").glob("**/*.md"))
    for action_file in action_files:
        text = action_file.read_text(encoding="utf-8")
        for raw_link in extract_wiki_links(text):
            if not raw_link.startswith("actions/"):
                continue
            linked = link_to_path(f"[[{raw_link}]]", root)
            if linked.suffix == ".raw":
                referenced_raw.add(linked)
                if not linked.exists():
                    issues.append(
                        ValidationIssue(
                            kind="missing_raw_link",
                            path=action_file.as_posix(),
                            detail=raw_link,
                        )
                    )

    for action_path in referenced_actions:
        if not action_path.exists():
            continue
        text = action_path.read_text(encoding="utf-8")
        if "memory_uuid:" not in text:
            issues.append(
                ValidationIssue(
                    kind="invalid_action_format",
                    path=action_path.as_posix(),
                    detail="missing memory_uuid front-matter",
                )
            )

    all_raw_files = sorted((root / "actions").glob("**/raw/*.raw"))
    for raw_file in all_raw_files:
        if raw_file not in referenced_raw:
            issues.append(
                ValidationIssue(
                    kind="orphan_raw",
                    path=raw_file.as_posix(),
                    detail="not referenced by any action markdown",
                )
            )

    if args.format == "json":
        payload = {
            "ok": len(issues) == 0,
            "issue_count": len(issues),
            "issues": [issue.__dict__ for issue in issues],
        }
        print(json.dumps(payload, indent=2))
    else:
        if issues:
            print(f"Validation failed with {len(issues)} issue(s):")
            for issue in issues:
                print(f"- {issue.kind} | {issue.path} | {issue.detail}")
        else:
            print("Validation passed: no missing links or orphan raw artifacts.")

    if issues and args.strict:
        return 1
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="AgentMemory markdown continuity logger")
    subparsers = parser.add_subparsers(dest="command", required=True)

    init_parser = subparsers.add_parser("init", help="initialize memory and action layout")
    init_parser.add_argument("--root", default=str(DEFAULT_ROOT), help="workspace root")
    init_parser.set_defaults(func=command_init)

    log_parser = subparsers.add_parser("log", help="log an agent event")
    log_parser.add_argument("--root", default=str(DEFAULT_ROOT), help="workspace root")
    log_parser.add_argument("--agent-id", required=True)
    log_parser.add_argument("--agent-name")
    log_parser.add_argument("--session-id", required=True)
    log_parser.add_argument("--event-type", default="message")
    log_parser.add_argument("--title")
    log_parser.add_argument("--summary")
    log_parser.add_argument("--message")
    log_parser.add_argument("--message-file")
    log_parser.add_argument("--tool-name")
    log_parser.add_argument("--tool-output")
    log_parser.add_argument("--tool-output-file")
    log_parser.add_argument("--raw-mode", choices=["always", "threshold"], default="always")
    log_parser.add_argument("--raw-threshold", type=int, default=2000)
    log_parser.add_argument("--model")
    log_parser.add_argument("--provider")
    log_parser.add_argument("--variant")
    log_parser.add_argument("--memory-uuid")
    log_parser.set_defaults(func=command_log)

    load_parser = subparsers.add_parser("load", help="load memory entries")
    load_parser.add_argument("--root", default=str(DEFAULT_ROOT), help="workspace root")
    load_parser.add_argument("--source", choices=["master", "agent", "session"], default="master")
    load_parser.add_argument("--agent-id")
    load_parser.add_argument("--session-id")
    load_parser.add_argument("--event-type")
    load_parser.add_argument("--since", help="ISO timestamp lower bound")
    load_parser.add_argument("--until", help="ISO timestamp upper bound")
    load_parser.add_argument("--limit", type=int, default=50)
    load_parser.add_argument("--reverse", action="store_true", help="newest first")
    load_parser.add_argument("--with-action", action="store_true", help="include action preview")
    load_parser.add_argument("--format", choices=["text", "json"], default="text")
    load_parser.set_defaults(func=command_load)

    validate_parser = subparsers.add_parser("validate", help="validate links and raw artifacts")
    validate_parser.add_argument("--root", default=str(DEFAULT_ROOT), help="workspace root")
    validate_parser.add_argument("--strict", action="store_true", help="exit non-zero when issues are found")
    validate_parser.add_argument("--format", choices=["text", "json"], default="text")
    validate_parser.set_defaults(func=command_validate)

    return parser


def main() -> int:
    parser = build_parser()
    args = parser.parse_args()
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
