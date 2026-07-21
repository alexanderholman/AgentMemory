#!/usr/bin/env python3

import argparse
import datetime as dt
import json
import os
import re
import shutil
import stat
import subprocess
import sys
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


@dataclass
class DoctorCheck:
    name: str
    ok: bool
    detail: str


@dataclass
class ActionDocument:
    timestamp: str
    memory_uuid: str
    agent: str
    session: str
    event_type: str
    title: str
    summary: str
    message: str
    relative_path: str
    source_bytes: int


@dataclass
class SearchResult:
    timestamp: str
    memory_uuid: str
    agent: str
    session: str
    event_type: str
    relative_path: str
    matched_field: str
    snippet: str
    score: float

    def as_dict(self) -> dict:
        return {
            "timestamp": self.timestamp,
            "memory_uuid": self.memory_uuid,
            "agent": self.agent,
            "session": self.session,
            "event_type": self.event_type,
            "relative_path": self.relative_path,
            "matched_field": self.matched_field,
            "snippet": self.snippet,
            "score": self.score,
        }


def utc_now() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc)


def iso_ts(ts: dt.datetime) -> str:
    return ts.strftime("%Y-%m-%dT%H:%M:%SZ")


def compact_ts(ts: dt.datetime) -> str:
    return ts.strftime("%Y%m%dT%H%M%S") + f".{int(ts.microsecond / 1000):03d}"


def safe_name(value: str) -> str:
    cleaned = re.sub(r"[^A-Za-z0-9._-]", "-", value.strip())
    return re.sub(r"-+", "-", cleaned).strip("-") or "unknown"


def validate_identifier(value: str, label: str) -> str:
    if not value or value in {".", ".."}:
        raise ValueError(f"{label} must not be empty, '.' or '..'")
    if re.search(r"[\\/\x00-\x1f\x7f]", value):
        raise ValueError(f"{label} contains a path separator or control character")
    if not re.fullmatch(r"[A-Za-z0-9._-]+", value):
        raise ValueError(f"{label} contains unsupported characters")
    return value


def normalize_identifier(value: str, label: str) -> str:
    if not value or value in {".", ".."} or re.search(r"[\\/\x00-\x1f\x7f]", value):
        raise ValueError(f"{label} is empty or contains traversal characters")
    normalized = safe_name(value)
    return validate_identifier(normalized, label)


def validate_display_name(value: str) -> str:
    if not value or len(value) > 200 or re.search(r"[\x00-\x1f\x7f]", value):
        raise ValueError("agent name must be a non-empty single-line value")
    return value


def parse_timestamp(value: str, label: str = "timestamp") -> dt.datetime:
    try:
        parsed = dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError(f"invalid {label}: {value}") from exc
    if parsed.tzinfo is None:
        raise ValueError(f"{label} must include a timezone")
    return parsed.astimezone(dt.timezone.utc)


def single_line_text(value: str, max_chars: int = 280) -> str:
    without_controls = re.sub(r"[\x00-\x1f\x7f-\x9f]", " ", value)
    collapsed = re.sub(r"\s+", " ", without_controls).strip()
    if len(collapsed) > max_chars:
        return collapsed[: max_chars - 3] + "..."
    return collapsed or "(empty)"


def wiki_link(path: Path, root: Path) -> str:
    rel = path.relative_to(root).as_posix()
    if rel.endswith(".md"):
        rel = rel[:-3]
    return f"[[{rel}]]"


def link_to_path(link: str, root: Path) -> Path:
    stripped = link.strip()
    if stripped.startswith("[[") and stripped.endswith("]]"):
        stripped = stripped[2:-2]
    root_resolved = root.resolve()
    candidate = (root_resolved / stripped).resolve(strict=False)
    try:
        candidate.relative_to(root_resolved)
    except ValueError as exc:
        raise ValueError(f"memory link escapes root: {link}") from exc
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
    return root / "memory" / ".state" / f"{validate_identifier(session_id, 'session id')}.json"


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

    # Avoid destroying useful memory such as paths and long identifiers. Only
    # mask long values that contain characters commonly present in secrets.
    entropy_like = re.compile(r"\b[A-Za-z0-9+/=_-]{32,}\b")
    matches = set(entropy_like.findall(redacted))
    for token in matches:
        if token.count("-") >= 4:
            continue
        if re.fullmatch(r"[A-Za-z/]+", token):
            continue
        if not re.search(r"[0-9+=_]", token):
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


def _section(text: str, heading: str, end_heading: str) -> str:
    marker = f"## {heading}\n"
    if marker not in text:
        return ""
    content = text.split(marker, 1)[1]
    end_marker = f"\n## {end_heading}\n"
    if end_marker not in content:
        return ""
    return content.rsplit(end_marker, 1)[0].strip()


def read_canonical_file(path: Path, root: Path, max_bytes: int) -> tuple[str, str, int]:
    root_resolved = root.resolve()
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(path, flags)
    except OSError as exc:
        raise ValueError("canonical action cannot be opened safely") from exc
    try:
        details = os.fstat(descriptor)
        if not stat.S_ISREG(details.st_mode):
            raise ValueError("canonical action must be a regular file")
        if details.st_size > max_bytes:
            raise ValueError("canonical action exceeds size limit")
        descriptor_path = Path(f"/proc/self/fd/{descriptor}").resolve()
        try:
            relative_path = descriptor_path.relative_to(root_resolved).as_posix()
        except ValueError as exc:
            raise ValueError("canonical action escapes memory root") from exc
        with os.fdopen(os.dup(descriptor), "r", encoding="utf-8") as handle:
            return handle.read(max_bytes + 1), relative_path, details.st_size
    finally:
        os.close(descriptor)


def parse_action_document(path: Path, root: Path, max_bytes: int = 1_000_000) -> ActionDocument:
    text, relative_path, source_bytes = read_canonical_file(path, root, max_bytes)
    if not text.startswith("---\n") or "\n---\n" not in text[4:]:
        raise ValueError("canonical action has invalid front matter")
    front_matter, body = text[4:].split("\n---\n", 1)
    metadata: dict[str, str] = {}
    for line in front_matter.splitlines():
        if ":" not in line:
            raise ValueError("canonical action has malformed metadata")
        key, value = line.split(":", 1)
        key = key.strip()
        if not key or key in metadata:
            raise ValueError("canonical action has duplicate or empty metadata")
        metadata[key] = value.strip()

    required = ("timestamp", "memory_uuid", "agent_id", "session_id", "event_type")
    if any(not metadata.get(field) for field in required):
        raise ValueError("canonical action is missing required metadata")
    parse_timestamp(metadata["timestamp"])
    validate_identifier(metadata["memory_uuid"], "memory UUID")
    validate_identifier(metadata["agent_id"], "agent id")
    validate_identifier(metadata["session_id"], "session id")
    validate_identifier(metadata["event_type"], "event type")

    title = ""
    for line in body.splitlines():
        if line.startswith("# "):
            title = line[2:].strip()
            break
    return ActionDocument(
        timestamp=metadata["timestamp"],
        memory_uuid=metadata["memory_uuid"],
        agent=metadata["agent_id"],
        session=metadata["session_id"],
        event_type=metadata["event_type"],
        title=title,
        summary=_section(body, "Summary", "Full Message"),
        message=_section(body, "Full Message", "Tool Outputs (summarized)"),
        relative_path=relative_path,
        source_bytes=source_bytes,
    )


def _query_terms(query: str) -> list[str]:
    if not query.strip():
        raise ValueError("search query must not be empty")
    if len(query) > 1000:
        raise ValueError("search query exceeds size limit")
    terms = list(dict.fromkeys(re.findall(r"\w+", query.casefold(), flags=re.UNICODE)))
    if not terms:
        raise ValueError("search query must contain searchable characters")
    if len(terms) > 32:
        raise ValueError("search query contains too many unique terms")
    return terms


def _matching_score(content: str, terms: list[str]) -> float:
    folded = content.casefold()
    counts = [len(re.findall(rf"(?<!\w){re.escape(term)}(?!\w)", folded)) for term in terms]
    if any(count == 0 for count in counts):
        return 0.0
    return float(sum(counts))


def search_action_documents(
    root: Path,
    query: str,
    *,
    agent_id: str | None = None,
    session_id: str | None = None,
    event_type: str | None = None,
    since: str | None = None,
    until: str | None = None,
    limit: int = 50,
) -> list[SearchResult]:
    root = root.expanduser().resolve()
    terms = _query_terms(query)
    if limit < 1 or limit > 1000:
        raise ValueError("limit must be between 1 and 1000")
    if agent_id:
        agent_id = normalize_identifier(agent_id, "agent id")
    if session_id:
        session_id = normalize_identifier(session_id, "session id")
    if event_type:
        event_type = normalize_identifier(event_type, "event type")
    since_value = parse_timestamp(since, "since timestamp") if since else None
    until_value = parse_timestamp(until, "until timestamp") if until else None
    if since_value and until_value and since_value > until_value:
        raise ValueError("since timestamp must not be after until timestamp")

    action_root = root / "actions"
    if not action_root.is_dir() or action_root.is_symlink():
        return []

    results: list[SearchResult] = []
    field_order = {"title": 0, "summary": 1, "message": 2}
    scanned_files = 0
    scanned_bytes = 0
    for current_dir, dirs, files in os.walk(action_root, followlinks=False):
        dirs[:] = sorted(name for name in dirs if name != "raw")
        for filename in sorted(files):
            if not filename.endswith(".md"):
                continue
            path = Path(current_dir) / filename
            try:
                document = parse_action_document(path, root)
            except (OSError, UnicodeError, ValueError):
                continue
            scanned_files += 1
            scanned_bytes += document.source_bytes
            if scanned_files > 10_000 or scanned_bytes > 50_000_000:
                raise ValueError("search scan exceeds configured resource limits")
            if agent_id and document.agent != agent_id:
                continue
            if session_id and document.session != session_id:
                continue
            if event_type and document.event_type != event_type:
                continue
            document_timestamp = parse_timestamp(document.timestamp)
            if since_value and document_timestamp < since_value:
                continue
            if until_value and document_timestamp > until_value:
                continue

            candidates = []
            for field in ("title", "summary", "message"):
                content, _ = redact_text(getattr(document, field))
                score = _matching_score(content, terms)
                if score:
                    candidates.append((score, field_order[field], field, single_line_text(content)))
            if not candidates:
                continue
            score, _, matched_field, snippet = sorted(candidates, key=lambda item: (-item[0], item[1]))[0]
            results.append(
                SearchResult(
                    timestamp=document.timestamp,
                    memory_uuid=document.memory_uuid,
                    agent=document.agent,
                    session=document.session,
                    event_type=document.event_type,
                    relative_path=document.relative_path,
                    matched_field=matched_field,
                    snippet=snippet,
                    score=score,
                )
            )

    results.sort(key=lambda item: (parse_timestamp(item.timestamp), item.memory_uuid), reverse=True)
    results.sort(key=lambda item: (-item.score, field_order[item.matched_field]))
    return results[:limit]


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

    agent_id = normalize_identifier(args.agent_id, "agent id")
    agent_name = validate_display_name(args.agent_name or agent_id)
    session_id = normalize_identifier(args.session_id, "session id")
    event_type = normalize_identifier(args.event_type, "event type")
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
    memory_uuid = validate_identifier(args.memory_uuid or str(uuid.uuid4()), "memory UUID")

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
    agent_file = root / "memory" / "agents" / f"{agent_id}.md"
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

    title, title_masked = redact_text(args.title or f"{event_type} from {agent_name}")
    summary_text, summary_masked = redact_text(
        args.summary or summarize(message_redacted, max_lines=3, max_chars=280)
    )
    any_redacted = any_redacted or title_masked or summary_masked

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
        path = root / "memory" / "agents" / f"{normalize_identifier(args.agent_id, 'agent id')}.md"
    else:
        if not args.session_id:
            raise SystemExit("--session-id is required when --source session")
        path = root / "memory" / "sessions" / f"{normalize_identifier(args.session_id, 'session id')}.md"

    entries = load_index_entries(path)

    if args.agent_id:
        entries = [entry for entry in entries if entry.agent == normalize_identifier(args.agent_id, "agent id")]
    if args.session_id:
        entries = [entry for entry in entries if entry.session == normalize_identifier(args.session_id, "session id")]
    if args.event_type:
        entries = [entry for entry in entries if entry.event_type == normalize_identifier(args.event_type, "event type")]

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


def command_search(args: argparse.Namespace) -> int:
    results = search_action_documents(
        Path(args.root),
        args.query,
        agent_id=args.agent_id,
        session_id=args.session_id,
        event_type=args.event_type,
        since=args.since,
        until=args.until,
        limit=args.limit,
    )
    if args.format == "json":
        print(json.dumps([result.as_dict() for result in results], indent=2))
        return 0
    for result in results:
        print(
            f"{result.score:.1f} | {result.timestamp} | {result.memory_uuid} | "
            f"{result.agent} | {result.session} | {result.event_type} | "
            f"{result.relative_path} | {result.matched_field} | {result.snippet}"
        )
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
            try:
                action_path = link_to_path(entry.action_link, root)
            except ValueError as exc:
                issues.append(
                    ValidationIssue(
                        kind="unsafe_memory_link",
                        path=index_path.as_posix(),
                        detail=str(exc),
                    )
                )
                continue
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
            try:
                linked = link_to_path(f"[[{raw_link}]]", root)
            except ValueError as exc:
                issues.append(
                    ValidationIssue(
                        kind="unsafe_memory_link",
                        path=action_file.as_posix(),
                        detail=str(exc),
                    )
                )
                continue
            if not raw_link.startswith("actions/"):
                continue
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


def command_doctor(args: argparse.Namespace) -> int:
    root = Path(args.root).expanduser().resolve()
    checks: list[DoctorCheck] = []

    py_ok = sys.version_info >= (3, 10)
    checks.append(
        DoctorCheck(
            name="python_version",
            ok=py_ok,
            detail=f"detected {sys.version.split()[0]}, requires >=3.10",
        )
    )

    script_ok = Path(__file__).exists()
    checks.append(
        DoctorCheck(
            name="agent_memory_script",
            ok=script_ok,
            detail=str(Path(__file__).resolve()),
        )
    )

    in_path = any(Path(part).expanduser() == Path.home() / ".local" / "bin" for part in os.environ.get("PATH", "").split(":"))
    checks.append(
        DoctorCheck(
            name="local_bin_on_path",
            ok=in_path,
            detail="~/.local/bin present in PATH" if in_path else "~/.local/bin missing from PATH",
        )
    )

    memlog_path = shutil.which("memlog")
    checks.append(
        DoctorCheck(
            name="memlog_discoverable",
            ok=bool(memlog_path),
            detail=memlog_path or "memlog not found on PATH",
        )
    )

    if memlog_path:
        try:
            result = subprocess.run(
                [memlog_path, "load", "--root", str(root), "--source", "master", "--limit", "1"],
                check=False,
                capture_output=True,
                text=True,
            )
            checks.append(
                DoctorCheck(
                    name="memlog_execution",
                    ok=result.returncode == 0,
                    detail="command executed" if result.returncode == 0 else (result.stderr.strip() or "execution failed"),
                )
            )
        except OSError as exc:
            checks.append(DoctorCheck(name="memlog_execution", ok=False, detail=str(exc)))

    try:
        ensure_layout(root)
        checks.append(
            DoctorCheck(
                name="memory_layout_access",
                ok=True,
                detail=f"layout available at {root}",
            )
        )
    except OSError as exc:
        checks.append(DoctorCheck(name="memory_layout_access", ok=False, detail=str(exc)))

    all_ok = all(check.ok for check in checks)
    if args.format == "json":
        print(
            json.dumps(
                {
                    "ok": all_ok,
                    "checks": [check.__dict__ for check in checks],
                },
                indent=2,
            )
        )
    else:
        status = "passed" if all_ok else "found issues"
        print(f"Doctor {status}:")
        for check in checks:
            marker = "OK" if check.ok else "FAIL"
            print(f"- {marker} {check.name}: {check.detail}")

    if args.strict and not all_ok:
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

    search_parser = subparsers.add_parser("search", help="search canonical action memories")
    search_parser.add_argument("query", help="terms that must occur in one canonical field")
    search_parser.add_argument("--root", default=str(DEFAULT_ROOT), help="workspace root")
    search_parser.add_argument("--agent-id")
    search_parser.add_argument("--session-id")
    search_parser.add_argument("--event-type")
    search_parser.add_argument("--since", help="ISO timestamp lower bound")
    search_parser.add_argument("--until", help="ISO timestamp upper bound")
    search_parser.add_argument("--limit", type=int, default=50)
    search_parser.add_argument("--format", choices=["text", "json"], default="text")
    search_parser.set_defaults(func=command_search)

    validate_parser = subparsers.add_parser("validate", help="validate links and raw artifacts")
    validate_parser.add_argument("--root", default=str(DEFAULT_ROOT), help="workspace root")
    validate_parser.add_argument("--strict", action="store_true", help="exit non-zero when issues are found")
    validate_parser.add_argument("--format", choices=["text", "json"], default="text")
    validate_parser.set_defaults(func=command_validate)

    doctor_parser = subparsers.add_parser("doctor", help="check local installation and runtime wiring")
    doctor_parser.add_argument("--root", default=str(DEFAULT_ROOT), help="workspace root")
    doctor_parser.add_argument("--strict", action="store_true", help="exit non-zero when checks fail")
    doctor_parser.add_argument("--format", choices=["text", "json"], default="text")
    doctor_parser.set_defaults(func=command_doctor)

    return parser


def main() -> int:
    parser = build_parser()
    args = parser.parse_args()
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
