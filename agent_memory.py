#!/usr/bin/env python3

import argparse
import datetime as dt
import hashlib
import json
import os
import re
import shutil
import sqlite3
import stat
import subprocess
import sys
import uuid
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path

try:
    import fcntl
except ImportError as exc:  # pragma: no cover
    raise RuntimeError("This tool currently requires POSIX file locking") from exc


DEFAULT_ROOT = Path.home() / "opencode"
MEMORY_INDEX_SCHEMA_VERSION = 2
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
    source_text: str


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


def validate_display_name(value: str) -> str:
    if not value or len(value) > 200 or re.search(r"[\x00-\x1f\x7f]", value):
        raise ValueError("agent name must be a non-empty single-line value")
    return value


def validate_metadata_scalar(value: str, label: str) -> str:
    if not value or len(value) > 500 or re.search(r"[\x00-\x1f\x7f]", value):
        raise ValueError(f"{label} must be a bounded single-line value")
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


def escape_reserved_headings(value: str) -> str:
    reserved = "Links|Summary|Full Message|Tool Outputs \\(summarized\\)|Raw Output Links"
    return re.sub(rf"(?m)^## ({reserved})$", r"\\## \1", value)


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
    entropy_like = re.compile(r"\b[A-Za-z0-9+/=_-]{24,}\b")

    def redact_entropy_match(match: re.Match) -> str:
        nonlocal touched
        touched = True
        return "[REDACTED_TOKEN]"

    redacted = entropy_like.sub(redact_entropy_match, redacted)

    return redacted, touched


def summarize(text: str, max_lines: int = 8, max_chars: int = 800) -> str:
    lines = text.strip().splitlines()
    clipped = "\n".join(lines[:max_lines])
    if len(clipped) > max_chars:
        clipped = clipped[: max_chars - 3] + "..."
    return clipped or "(empty)"


def _generated_tool_boundary(text: str) -> int | None:
    marker = "\n## Tool Outputs (summarized)\n"
    offset = 0
    while True:
        boundary = text.find(marker, offset)
        if boundary < 0:
            return None
        following = text[boundary + len(marker) :]
        if (
            (following.startswith("- none") or following.startswith("- tool:"))
            and "\n## Raw Output Links\n" in following
        ):
            return boundary
        offset = boundary + len(marker)


def parse_memory_sections(body: str) -> tuple[str, str]:
    links_marker = "\n## Links\n"
    summary_marker = "\n## Summary\n"
    full_message_marker = "\n## Full Message\n"
    offset = 0
    content = ""
    while True:
        links_boundary = body.find(links_marker, offset)
        if links_boundary < 0:
            return "", ""
        following = body[links_boundary + len(links_marker) :]
        if following.startswith("- master: [[memory/master]]") and summary_marker in following:
            content = following.split(summary_marker, 1)[1]
            break
        offset = links_boundary + len(links_marker)
    tool_boundary = _generated_tool_boundary(content)
    if tool_boundary is None:
        return "", ""
    searchable = content[:tool_boundary]
    boundaries = [match.start() for match in re.finditer(re.escape(full_message_marker), searchable)]
    if not boundaries:
        return searchable.strip(), ""
    summary = searchable[: boundaries[0]].strip()
    message = searchable[boundaries[-1] + len(full_message_marker) :].strip()
    return summary, message


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
    summary, message = parse_memory_sections(body)
    return ActionDocument(
        timestamp=metadata["timestamp"],
        memory_uuid=metadata["memory_uuid"],
        agent=metadata["agent_id"],
        session=metadata["session_id"],
        event_type=metadata["event_type"],
        title=title,
        summary=summary,
        message=message,
        relative_path=relative_path,
        source_bytes=source_bytes,
        source_text=text,
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
        agent_id = validate_identifier(agent_id, "agent id")
    if session_id:
        session_id = validate_identifier(session_id, "session id")
    if event_type:
        event_type = validate_identifier(event_type, "event type")
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
    scanned_directories = 0
    for current_dir, dirs, files in os.walk(action_root, followlinks=False):
        scanned_directories += 1
        if scanned_directories > 10_000:
            raise ValueError("search scan exceeds configured resource limits")
        dirs[:] = sorted(name for name in dirs if name != "raw")
        for filename in sorted(files):
            if not filename.endswith(".md"):
                continue
            path = Path(current_dir) / filename
            scanned_files += 1
            try:
                scanned_bytes += min(path.lstat().st_size, 1_000_001)
            except OSError:
                pass
            if scanned_files > 10_000 or scanned_bytes > 50_000_000:
                raise ValueError("search scan exceeds configured resource limits")
            try:
                document = parse_action_document(path, root)
            except (OSError, UnicodeError, ValueError):
                continue
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


def memory_index_path(root: Path) -> Path:
    return root.expanduser().resolve() / "memory" / ".state" / "search.sqlite3"


@contextmanager
def _secure_index_directory(root: Path):
    root = root.expanduser().resolve()
    flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0)
    root_descriptor = os.open(root, flags)
    memory_descriptor = None
    state_descriptor = None
    try:
        for parent_descriptor, name in ((root_descriptor, "memory"),):
            try:
                os.mkdir(name, 0o700, dir_fd=parent_descriptor)
            except FileExistsError:
                pass
        try:
            memory_descriptor = os.open("memory", flags, dir_fd=root_descriptor)
        except (NotADirectoryError, OSError) as exc:
            raise ValueError("refusing symlinked memory index directory: memory") from exc
        try:
            os.mkdir(".state", 0o700, dir_fd=memory_descriptor)
        except FileExistsError:
            pass
        try:
            state_descriptor = os.open(".state", flags, dir_fd=memory_descriptor)
        except (NotADirectoryError, OSError) as exc:
            raise ValueError("refusing symlinked memory index directory: .state") from exc
        details = os.fstat(state_descriptor)
        if not stat.S_ISDIR(details.st_mode) or details.st_uid != os.geteuid():
            raise ValueError("memory index directory is not private and owned")
        os.fchmod(state_descriptor, 0o700)
        yield state_descriptor
    finally:
        if state_descriptor is not None:
            os.close(state_descriptor)
        if memory_descriptor is not None:
            os.close(memory_descriptor)
        os.close(root_descriptor)


def _validate_index_entry(directory_fd: int, name: str) -> os.stat_result | None:
    try:
        details = os.stat(name, dir_fd=directory_fd, follow_symlinks=False)
    except FileNotFoundError:
        return None
    if not stat.S_ISREG(details.st_mode):
        raise ValueError(f"memory index is not a regular file: {name}")
    if details.st_nlink != 1:
        raise ValueError(f"refusing hardlinked memory index file: {name}")
    if details.st_uid != os.geteuid():
        raise ValueError(f"memory index has unexpected owner: {name}")
    return details


def _reject_canonical_sidecars(directory_fd: int) -> None:
    for name in ("search.sqlite3-wal", "search.sqlite3-shm", "search.sqlite3-journal"):
        try:
            os.stat(name, dir_fd=directory_fd, follow_symlinks=False)
        except FileNotFoundError:
            continue
        raise ValueError(f"canonical SQLite sidecar requires fallback or rebuild: {name}")


def _is_sqlite_corruption(exc: BaseException) -> bool:
    markers = (
        "not a database",
        "malformed",
        "no such table",
        "file is encrypted",
        "schema migration required",
    )
    return any(marker in str(exc).lower() for marker in markers)


def _stable_directory_path(directory_fd: int) -> Path:
    for base in (Path("/proc/self/fd"), Path("/dev/fd")):
        candidate = base / str(directory_fd)
        if candidate.exists():
            return candidate
    raise RuntimeError("stable file-descriptor paths are unavailable")


def _remove_index_files(root: Path) -> None:
    with _secure_index_directory(root) as directory_fd:
        for name in ("search.sqlite3", "search.sqlite3-wal", "search.sqlite3-shm"):
            if _validate_index_entry(directory_fd, name) is not None:
                os.unlink(name, dir_fd=directory_fd)


@contextmanager
def _connect_memory_index(
    root: Path, *, read_only: bool = False, create: bool = True, reset: bool = False
):
    with _secure_index_directory(root) as directory_fd:
        original = _validate_index_entry(directory_fd, "search.sqlite3")
        _reject_canonical_sidecars(directory_fd)
        if original is None and not create:
            raise FileNotFoundError(memory_index_path(root))
        temporary_name = f".search-{uuid.uuid4().hex}.sqlite3"
        temporary_fd = os.open(
            temporary_name,
            os.O_RDWR | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0),
            0o600,
            dir_fd=directory_fd,
        )
        temporary_identity = os.fstat(temporary_fd)
        lock_name = ""
        lock_connection = None
        try:
            if original is not None:
                source_fd = os.open(
                    "search.sqlite3", os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0), dir_fd=directory_fd
                )
                try:
                    source_details = os.fstat(source_fd)
                    if source_details.st_ino != original.st_ino or source_details.st_dev != original.st_dev:
                        raise ValueError("memory index changed while snapshotting")
                    lock_name = f".search-lock-{uuid.uuid4().hex}.sqlite3"
                    os.link(
                        "search.sqlite3",
                        lock_name,
                        src_dir_fd=directory_fd,
                        dst_dir_fd=directory_fd,
                        follow_symlinks=False,
                    )
                    lock_details = os.stat(lock_name, dir_fd=directory_fd, follow_symlinks=False)
                    if lock_details.st_ino != source_details.st_ino or lock_details.st_dev != source_details.st_dev:
                        raise ValueError("memory index changed while locking")
                    try:
                        lock_connection = sqlite3.connect(
                            _stable_directory_path(directory_fd) / lock_name, timeout=0
                        )
                        lock_connection.execute("BEGIN" if read_only else "BEGIN IMMEDIATE")
                        lock_connection.execute("SELECT COUNT(*) FROM sqlite_master").fetchone()
                    except sqlite3.DatabaseError as exc:
                        if not (reset and _is_sqlite_corruption(exc)):
                            raise
                        if lock_connection is not None:
                            lock_connection.close()
                            lock_connection = None
                    if not reset:
                        while True:
                            block = os.read(source_fd, 1024 * 1024)
                            if not block:
                                break
                            os.write(temporary_fd, block)
                finally:
                    os.close(source_fd)
                if not reset:
                    _reject_canonical_sidecars(directory_fd)
                    current_source = os.stat(
                        "search.sqlite3", dir_fd=directory_fd, follow_symlinks=False
                    )
                    if (
                        current_source.st_ino != original.st_ino
                        or current_source.st_dev != original.st_dev
                        or current_source.st_size != original.st_size
                        or current_source.st_mtime_ns != original.st_mtime_ns
                    ):
                        raise ValueError("memory index changed while snapshotting")
            os.fsync(temporary_fd)
        except BaseException:
            if lock_connection is not None:
                lock_connection.rollback()
                lock_connection.close()
            if lock_name:
                try:
                    os.unlink(lock_name, dir_fd=directory_fd)
                except FileNotFoundError:
                    pass
            try:
                os.unlink(temporary_name, dir_fd=directory_fd)
            except FileNotFoundError:
                pass
            raise
        finally:
            os.close(temporary_fd)
        stable_path = _stable_directory_path(directory_fd) / temporary_name
        connection = sqlite3.connect(stable_path, timeout=0)
        successful = False
        try:
            connection.execute("PRAGMA foreign_keys = ON")
            connection.execute("PRAGMA journal_mode = DELETE")
            if not read_only:
                connection.executescript(
                    """
                    CREATE TABLE IF NOT EXISTS index_meta (
                        key TEXT PRIMARY KEY,
                        value TEXT NOT NULL
                    );
                    CREATE TABLE IF NOT EXISTS documents (
                        memory_uuid TEXT PRIMARY KEY,
                        relative_path TEXT NOT NULL UNIQUE,
                        content_hash TEXT NOT NULL,
                        timestamp TEXT NOT NULL,
                        agent TEXT NOT NULL,
                        session TEXT NOT NULL,
                        event_type TEXT NOT NULL,
                        title TEXT NOT NULL,
                        summary TEXT NOT NULL,
                        message TEXT NOT NULL,
                        row_hash TEXT NOT NULL
                    );
                    CREATE VIRTUAL TABLE IF NOT EXISTS documents_fts USING fts5(
                        memory_uuid UNINDEXED,
                        title,
                        summary,
                        message,
                        tokenize = 'unicode61'
                    );
                    """
                )
                columns = {row[1] for row in connection.execute("PRAGMA table_info(documents)")}
                if "row_hash" not in columns:
                    raise sqlite3.DatabaseError("schema migration required")
                connection.execute(
                    "INSERT OR REPLACE INTO index_meta(key, value) VALUES('schema_version', ?)",
                    (str(MEMORY_INDEX_SCHEMA_VERSION),),
                )
                connection.commit()
            yield connection
            successful = True
        finally:
            connection.close()
            installation_error = None
            try:
                if successful and original is not None:
                    _reject_canonical_sidecars(directory_fd)
                    stable_current = os.stat(
                        "search.sqlite3", dir_fd=directory_fd, follow_symlinks=False
                    )
                    if (
                        stable_current.st_ino != original.st_ino
                        or stable_current.st_dev != original.st_dev
                        or stable_current.st_size != original.st_size
                        or stable_current.st_mtime_ns != original.st_mtime_ns
                    ):
                        raise ValueError("memory index changed during snapshot use")
                if successful and not read_only:
                    current = os.stat(
                        "search.sqlite3", dir_fd=directory_fd, follow_symlinks=False
                    ) if original is not None else _validate_index_entry(directory_fd, "search.sqlite3")
                    if original is None:
                        if current is not None:
                            raise ValueError("memory index appeared while updating")
                    elif current.st_ino != original.st_ino or current.st_dev != original.st_dev:
                        raise ValueError("memory index changed while updating")
                    install_fd = os.open(
                        temporary_name,
                        os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0),
                        dir_fd=directory_fd,
                    )
                    try:
                        install_details = os.fstat(install_fd)
                        if (
                            install_details.st_ino != temporary_identity.st_ino
                            or install_details.st_dev != temporary_identity.st_dev
                            or install_details.st_nlink != 1
                            or install_details.st_uid != os.geteuid()
                        ):
                            raise ValueError("memory index snapshot changed before installation")
                        os.replace(
                            temporary_name,
                            "search.sqlite3",
                            src_dir_fd=directory_fd,
                            dst_dir_fd=directory_fd,
                        )
                        try:
                            installed = os.stat(
                                "search.sqlite3", dir_fd=directory_fd, follow_symlinks=False
                            )
                            if (
                                installed.st_ino != install_details.st_ino
                                or installed.st_dev != install_details.st_dev
                            ):
                                raise ValueError("memory index snapshot changed during installation")
                            os.fchmod(install_fd, 0o600)
                        except BaseException:
                            if original is not None and lock_name:
                                os.replace(
                                    lock_name,
                                    "search.sqlite3",
                                    src_dir_fd=directory_fd,
                                    dst_dir_fd=directory_fd,
                                )
                                lock_name = ""
                            elif original is None:
                                os.unlink("search.sqlite3", dir_fd=directory_fd)
                            raise
                        temporary_name = ""
                    finally:
                        os.close(install_fd)
            except BaseException as exc:
                installation_error = exc
            finally:
                if lock_connection is not None:
                    lock_connection.rollback()
                    lock_connection.close()
                if temporary_name:
                    for suffix in ("", "-journal", "-wal", "-shm"):
                        try:
                            os.unlink(f"{temporary_name}{suffix}", dir_fd=directory_fd)
                        except FileNotFoundError:
                            pass
                if lock_name:
                    try:
                        os.unlink(lock_name, dir_fd=directory_fd)
                    except FileNotFoundError:
                        pass
            if installation_error is not None:
                raise installation_error


def _discover_index_documents(root: Path) -> list[tuple[ActionDocument, str]]:
    root = root.expanduser().resolve()
    action_root = root / "actions"
    if not action_root.is_dir() or action_root.is_symlink():
        return []
    documents: list[tuple[ActionDocument, str]] = []
    seen_uuids: set[str] = set()
    scanned_files = 0
    scanned_bytes = 0
    scanned_directories = 0
    for current_dir, dirs, files in os.walk(action_root, followlinks=False):
        scanned_directories += 1
        if scanned_directories > 10_000:
            raise ValueError("index scan exceeds configured resource limits")
        dirs[:] = sorted(name for name in dirs if name != "raw")
        for filename in sorted(files):
            if not filename.endswith(".md"):
                continue
            scanned_files += 1
            path = Path(current_dir) / filename
            try:
                scanned_bytes += min(path.lstat().st_size, 1_000_001)
            except OSError:
                pass
            if scanned_files > 10_000 or scanned_bytes > 50_000_000:
                raise ValueError("index scan exceeds configured resource limits")
            try:
                document = parse_action_document(path, root)
            except (OSError, UnicodeError, ValueError):
                continue
            if document.memory_uuid in seen_uuids:
                raise ValueError(f"duplicate memory UUID: {document.memory_uuid}")
            seen_uuids.add(document.memory_uuid)
            content_hash = hashlib.sha256(document.source_text.encode("utf-8")).hexdigest()
            documents.append((document, content_hash))
    return documents


def _indexed_row_hash(values: tuple[str, ...]) -> str:
    encoded = json.dumps(values, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _fts_text(value: str) -> str:
    return " ".join(re.findall(r"\w+", value.casefold(), flags=re.UNICODE))


def _check_fts_integrity(connection: sqlite3.Connection) -> None:
    connection.execute(
        "INSERT INTO documents_fts(documents_fts) VALUES('integrity-check')"
    )


def _sync_memory_index_once(root: Path, *, reset: bool = False) -> dict:
    documents = _discover_index_documents(root)
    with _connect_memory_index(root, reset=reset) as connection:
        _check_fts_integrity(connection)
        existing = {
            row[0]: tuple(row[1:])
            for row in connection.execute(
                """
                SELECT d.memory_uuid, d.content_hash, d.relative_path, d.timestamp,
                       d.agent, d.session, d.event_type, d.title, d.summary, d.message,
                       d.row_hash, COUNT(f.rowid), MIN(f.title), MIN(f.summary), MIN(f.message)
                FROM documents AS d
                LEFT JOIN documents_fts AS f ON f.memory_uuid = d.memory_uuid
                GROUP BY d.memory_uuid
                """
            )
        }
        added = updated = unchanged = 0
        seen: set[str] = set()
        for document, content_hash in documents:
            seen.add(document.memory_uuid)
            prior = existing.get(document.memory_uuid)
            title, _ = redact_text(document.title)
            summary, _ = redact_text(document.summary)
            message, _ = redact_text(document.message)
            row_values = (
                content_hash,
                document.relative_path,
                document.timestamp,
                document.agent,
                document.session,
                document.event_type,
                title,
                summary,
                message,
            )
            row_hash = _indexed_row_hash(row_values)
            expected_fts = (_fts_text(title), _fts_text(summary), _fts_text(message))
            if prior == (*row_values, row_hash, 1, *expected_fts):
                unchanged += 1
                continue
            connection.execute(
                """
                INSERT OR REPLACE INTO documents(
                    memory_uuid, relative_path, content_hash, timestamp, agent, session,
                    event_type, title, summary, message, row_hash
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    document.memory_uuid,
                    document.relative_path,
                    content_hash,
                    document.timestamp,
                    document.agent,
                    document.session,
                    document.event_type,
                    title,
                    summary,
                    message,
                    row_hash,
                ),
            )
            connection.execute("DELETE FROM documents_fts WHERE memory_uuid = ?", (document.memory_uuid,))
            connection.execute(
                "INSERT INTO documents_fts(memory_uuid, title, summary, message) VALUES (?, ?, ?, ?)",
                (document.memory_uuid, *expected_fts),
            )
            if prior is None:
                added += 1
            else:
                updated += 1
        removed_uuids = sorted(set(existing) - seen)
        for memory_uuid in removed_uuids:
            connection.execute("DELETE FROM documents WHERE memory_uuid = ?", (memory_uuid,))
            connection.execute("DELETE FROM documents_fts WHERE memory_uuid = ?", (memory_uuid,))
        connection.execute(
            "DELETE FROM documents_fts WHERE memory_uuid NOT IN (SELECT memory_uuid FROM documents)"
        )
        connection.commit()
    return {
        "added": added,
        "updated": updated,
        "removed": len(removed_uuids),
        "unchanged": unchanged,
        "recovered": False,
    }


def sync_memory_index(root: Path) -> dict:
    root = root.expanduser().resolve()
    try:
        return _sync_memory_index_once(root)
    except sqlite3.DatabaseError as exc:
        if not _is_sqlite_corruption(exc):
            raise
        result = _sync_memory_index_once(root, reset=True)
        result["recovered"] = True
        return result


def rebuild_memory_index(root: Path) -> dict:
    root = root.expanduser().resolve()
    return _sync_memory_index_once(root, reset=True)


def memory_index_status(root: Path) -> dict:
    path = memory_index_path(root)
    try:
        with _connect_memory_index(root, read_only=True, create=False) as connection:
            integrity = connection.execute("PRAGMA integrity_check").fetchone()[0]
            _check_fts_integrity(connection)
            schema_row = connection.execute(
                "SELECT value FROM index_meta WHERE key = 'schema_version'"
            ).fetchone()
            count = connection.execute("SELECT COUNT(*) FROM documents").fetchone()[0]
            fts_count = connection.execute("SELECT COUNT(*) FROM documents_fts").fetchone()[0]
            missing_fts = connection.execute(
                """
                SELECT COUNT(*) FROM documents AS d
                WHERE NOT EXISTS (
                    SELECT 1 FROM documents_fts AS f WHERE f.memory_uuid = d.memory_uuid
                )
                """
            ).fetchone()[0]
            orphan_fts = connection.execute(
                """
                SELECT COUNT(*) FROM documents_fts AS f
                WHERE NOT EXISTS (
                    SELECT 1 FROM documents AS d WHERE d.memory_uuid = f.memory_uuid
                )
                """
            ).fetchone()[0]
            payload_rows = connection.execute(
                """
                SELECT d.title, d.summary, d.message, f.title, f.summary, f.message
                FROM documents AS d
                JOIN documents_fts AS f ON f.memory_uuid = d.memory_uuid
                """
            ).fetchall()
            payload_ok = all(
                (_fts_text(row[0]), _fts_text(row[1]), _fts_text(row[2])) == tuple(row[3:])
                for row in payload_rows
            )
            document_rows = connection.execute(
                """
                SELECT content_hash, relative_path, timestamp, agent, session, event_type,
                       title, summary, message, row_hash
                FROM documents
                """
            ).fetchall()
            document_rows_ok = all(
                _indexed_row_hash(tuple(row[:9])) == row[9] for row in document_rows
            )
        schema_version = int(schema_row[0]) if schema_row else None
        return {
            "ok": (
                integrity == "ok"
                and schema_version == MEMORY_INDEX_SCHEMA_VERSION
                and fts_count == count
                and missing_fts == 0
                and orphan_fts == 0
                and payload_ok
                and document_rows_ok
            ),
            "exists": True,
            "schema_version": schema_version,
            "document_count": count,
            "path": path.as_posix(),
        }
    except FileNotFoundError:
        return {
            "ok": False,
            "exists": False,
            "schema_version": None,
            "document_count": 0,
            "path": path.as_posix(),
        }
    except (OSError, sqlite3.DatabaseError, TypeError, ValueError):
        return {
            "ok": False,
            "exists": True,
            "schema_version": None,
            "document_count": 0,
            "path": path.as_posix(),
        }


def search_indexed_documents(
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
    terms = _query_terms(query)
    if limit < 1 or limit > 1000:
        raise ValueError("limit must be between 1 and 1000")
    if agent_id:
        agent_id = validate_identifier(agent_id, "agent id")
    if session_id:
        session_id = validate_identifier(session_id, "session id")
    if event_type:
        event_type = validate_identifier(event_type, "event type")
    since_value = parse_timestamp(since, "since timestamp") if since else None
    until_value = parse_timestamp(until, "until timestamp") if until else None
    if since_value and until_value and since_value > until_value:
        raise ValueError("since timestamp must not be after until timestamp")
    fts_query = " AND ".join(f'"{term.replace(chr(34), chr(34) * 2)}"' for term in terms)
    use_fts = all(term.isascii() and term.isalnum() for term in terms)
    clauses = []
    parameters: list[str] = []
    if use_fts:
        clauses.append("documents_fts MATCH ?")
        parameters.append(fts_query)
    for column, value in (("d.agent", agent_id), ("d.session", session_id), ("d.event_type", event_type)):
        if value:
            clauses.append(f"{column} = ?")
            parameters.append(value)
    with _connect_memory_index(root, read_only=True, create=False) as connection:
        source = "documents_fts JOIN documents AS d USING(memory_uuid)" if use_fts else "documents AS d"
        where_clause = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        rows = connection.execute(
            f"""
            SELECT d.timestamp, d.memory_uuid, d.agent, d.session, d.event_type,
                   d.relative_path, d.title, d.summary, d.message
            FROM {source}
            {where_clause}
            """,
            parameters,
        ).fetchall()
    field_order = {"title": 0, "summary": 1, "message": 2}
    results: list[SearchResult] = []
    for timestamp, memory_uuid, agent, session, kind, relative_path, title, summary, message in rows:
        if agent_id and agent != agent_id:
            continue
        if session_id and session != session_id:
            continue
        if event_type and kind != event_type:
            continue
        timestamp_value = parse_timestamp(timestamp)
        if since_value and timestamp_value < since_value:
            continue
        if until_value and timestamp_value > until_value:
            continue
        candidates = []
        for field, content in (("title", title), ("summary", summary), ("message", message)):
            score = _matching_score(content, terms)
            if score:
                candidates.append((score, field_order[field], field, single_line_text(content)))
        if not candidates:
            continue
        score, _, matched_field, snippet = sorted(candidates, key=lambda item: (-item[0], item[1]))[0]
        results.append(
            SearchResult(
                timestamp=timestamp,
                memory_uuid=memory_uuid,
                agent=agent,
                session=session,
                event_type=kind,
                relative_path=relative_path,
                matched_field=matched_field,
                snippet=snippet,
                score=score,
            )
        )
    results.sort(key=lambda item: (parse_timestamp(item.timestamp), item.memory_uuid), reverse=True)
    results.sort(key=lambda item: (-item.score, field_order[item.matched_field]))
    return results[:limit]


def search_memories(root: Path, query: str, *, mode: str = "auto", **filters) -> list[SearchResult]:
    if mode not in {"auto", "index", "scan"}:
        raise ValueError("search mode must be auto, index, or scan")
    if mode == "scan":
        return search_action_documents(root, query, **filters)
    if mode == "index":
        return search_indexed_documents(root, query, **filters)
    if memory_index_status(root)["ok"]:
        try:
            sync_memory_index(root)
            return search_indexed_documents(root, query, **filters)
        except (OSError, sqlite3.DatabaseError, ValueError):
            pass
    return search_action_documents(root, query, **filters)


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

    agent_id = validate_identifier(args.agent_id, "agent id")
    agent_name = validate_display_name(args.agent_name or agent_id)
    session_id = validate_identifier(args.session_id, "session id")
    event_type = validate_identifier(args.event_type, "event type")
    model = validate_metadata_scalar(args.model or "unknown", "model")
    provider = validate_metadata_scalar(args.provider or "unknown", "provider")
    variant = validate_metadata_scalar(args.variant or "unknown", "variant")

    message = parse_message(args)
    tool_output = parse_tool_output(args)
    tool_used = bool(args.tool_name or tool_output)

    message_redacted, message_masked = redact_text(message)
    tool_redacted, tool_masked = redact_text(tool_output)
    message_redacted = escape_reserved_headings(message_redacted)
    tool_document_text = escape_reserved_headings(tool_redacted)
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
        raw_summary = summarize(tool_document_text)
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
    title = single_line_text(title, max_chars=200)
    summary_text, summary_masked = redact_text(
        args.summary or summarize(message_redacted, max_lines=3, max_chars=280)
    )
    summary_text = escape_reserved_headings(summary_text)
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


def canonical_action_preview(entry: IndexEntry, root: Path, max_chars: int) -> tuple[Path | None, str | None, str | None]:
    try:
        action_path = link_to_path(entry.action_link, root)
    except ValueError as exc:
        return None, None, f"unsafe action link: {exc}"
    if action_path.suffix != ".md":
        return action_path, None, "unsafe action link: action link does not reference canonical Markdown"
    try:
        document = parse_action_document(action_path, root)
        return action_path, summarize(document.source_text, max_lines=12, max_chars=max_chars), None
    except (OSError, UnicodeError, ValueError) as exc:
        return action_path, None, f"unsafe action link: {exc}"


def command_load(args: argparse.Namespace) -> int:
    root = Path(args.root).expanduser().resolve()
    ensure_layout(root)

    source = args.source
    if source == "master":
        path = root / "memory" / "master.md"
    elif source == "agent":
        if not args.agent_id:
            raise SystemExit("--agent-id is required when --source agent")
        requested_agent = validate_identifier(args.agent_id, "agent id")
        path = root / "memory" / "agents" / f"{requested_agent}.md"
    else:
        if not args.session_id:
            raise SystemExit("--session-id is required when --source session")
        path = root / "memory" / "sessions" / f"{validate_identifier(args.session_id, 'session id')}.md"

    entries = load_index_entries(path)
    if source == "agent":
        canonical_entries = entries
        master_entries = load_index_entries(root / "memory" / "master.md")
        entries = []
        seen_memory_uuids: set[str] = set()
        for entry in canonical_entries + master_entries:
            if entry.agent != requested_agent:
                continue
            if entry.memory_uuid in seen_memory_uuids:
                continue
            entries.append(entry)
            seen_memory_uuids.add(entry.memory_uuid)

    if args.agent_id:
        entries = [entry for entry in entries if entry.agent == validate_identifier(args.agent_id, "agent id")]
    if args.session_id:
        entries = [entry for entry in entries if entry.session == validate_identifier(args.session_id, "session id")]
    if args.event_type:
        entries = [entry for entry in entries if entry.event_type == validate_identifier(args.event_type, "event type")]

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
                action_path, preview, error = canonical_action_preview(entry, root, 1600)
                if action_path:
                    row["action_path"] = action_path.as_posix()
                if preview:
                    row["action_preview"] = preview
                if error:
                    row["action_error"] = error
            payload.append(row)
        print(json.dumps(payload, indent=2))
        return 0

    for entry in entries:
        safe_values = [
            single_line_text(value, max_chars=500)
            for value in (
                entry.timestamp,
                entry.memory_uuid,
                entry.agent,
                entry.session,
                entry.event_type,
                entry.action_link,
                entry.summary,
            )
        ]
        print(
            f"{safe_values[0]} | {safe_values[1]} | {safe_values[2]} | "
            f"{safe_values[3]} | {safe_values[4]} | {safe_values[5]} | {safe_values[6]}"
        )
        if args.with_action:
            action_path, preview, error = canonical_action_preview(entry, root, 1200)
            if action_path and preview:
                print(f"  action: {single_line_text(action_path.as_posix(), max_chars=500)}")
                print(f"  preview: {single_line_text(preview, max_chars=1200)}")
            else:
                print(f"  action: rejected ({single_line_text(error or 'unknown error', max_chars=500)})")

    return 0


def command_search(args: argparse.Namespace) -> int:
    results = search_memories(
        Path(args.root),
        args.query,
        mode=args.mode,
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
        safe_path = single_line_text(result.relative_path, max_chars=500)
        print(
            f"{result.score:.1f} | {result.timestamp} | {result.memory_uuid} | "
            f"{result.agent} | {result.session} | {result.event_type} | "
            f"{safe_path} | {result.matched_field} | {result.snippet}"
        )
    return 0


def command_index(args: argparse.Namespace) -> int:
    root = Path(args.root)
    if args.index_command == "sync":
        payload = sync_memory_index(root)
    elif args.index_command == "rebuild":
        payload = rebuild_memory_index(root)
    else:
        payload = memory_index_status(root)
    if args.format == "json":
        print(json.dumps(payload, indent=2))
    else:
        print(" | ".join(f"{key}: {value}" for key, value in payload.items()))
    if args.index_command == "status" and not payload["ok"]:
        return 1
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
    search_parser.add_argument("--mode", choices=["auto", "index", "scan"], default="auto")
    search_parser.add_argument("--format", choices=["text", "json"], default="text")
    search_parser.set_defaults(func=command_search)

    index_parser = subparsers.add_parser("index", help="manage the disposable SQLite search index")
    index_subparsers = index_parser.add_subparsers(dest="index_command", required=True)
    for index_command in ("sync", "status", "rebuild"):
        child = index_subparsers.add_parser(index_command)
        child.add_argument("--root", default=str(DEFAULT_ROOT), help="workspace root")
        child.add_argument("--format", choices=["text", "json"], default="text")
        child.set_defaults(func=command_index)

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
