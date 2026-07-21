# AgentMemory

AgentMemory is a markdown-first continuity logger for multi-agent workflows.

It tracks:

- global master memory
- per-agent memory
- per-session memory
- per-event action documents with `memory_uuid` in filename
- summarized tool output with links to raw artifacts

## Repository status

This repository is initialized locally and not published.
Intended remote: `github.com/alexanderholman/AgentMemory`.

## Quick start

```bash
python3 agent_memory.py init --root ~/opencode
```

Install `memlog` into `~/.local/bin`:

```bash
./install.sh
```

Or replace an existing install:

```bash
./install.sh --force
```

Log an event:

```bash
python3 agent_memory.py log \
  --root ~/opencode \
  --agent-id opencode-manager \
  --agent-name OpenCodeManager \
  --session-id ses_example \
  --event-type message \
  --model gpt-5.3-codex \
  --provider openai \
  --variant default \
  --message "I will run implementation tasks now." \
  --tool-name bash
```

Load/query memory:

```bash
python3 agent_memory.py load --root ~/opencode --source master --limit 20 --reverse
python3 agent_memory.py load --root ~/opencode --source session --session-id ses_example --format json
```

Search canonical action memories without indexing raw tool artifacts:

```bash
memlog search "routing decision" --root ~/opencode --limit 10
memlog search "routing decision" --root ~/opencode \
  --agent-id pr.command.zordon --since 2026-07-01T00:00:00Z --format json
memlog search "routing decision" --root ~/opencode --mode index
```

Search is deliberately local and deterministic. Every query term must occur
in one canonical field (`title`, `summary`, or `full message`). Results include
the memory UUID, source path, matched field, snippet, and score. Raw artifacts,
symlinks, malformed action documents, and paths outside the memory root are
never searched.

### Disposable SQLite index

Build or refresh the local FTS5 projection:

```bash
memlog index sync --root ~/opencode
memlog index status --root ~/opencode --format json
memlog index rebuild --root ~/opencode
```

For an exclusively owned memory root, the index lives at
`memory/.state/search.sqlite3`. Shared roots use a private per-user location
under `$XDG_STATE_HOME/agentmemory/indexes/<project-key>/search.sqlite3`
(defaulting to `~/.local/state`). The index is mode `0600` and is derived
entirely from canonical action Markdown. Synchronization is incremental by
content hash and removes deleted documents. Status checks both SQLite
integrity and correspondence between relational and FTS rows.

Search modes are:

- `auto` (default): use a healthy index after synchronizing it, otherwise scan
  canonical Markdown directly;
- `index`: require the SQLite index and fail if it is unavailable;
- `scan`: bypass SQLite and use the deterministic Markdown fallback.

`--mode` selects only the text backend. Text retrieval remains the default and
keeps the existing bare JSON array output. Operator-only semantic retrieval is
an independent, explicit mode:

```bash
memlog search "routing decision" --root ~/opencode \
  --retrieval semantic \
  --ollama-endpoint http://127.0.0.1:11434 \
  --ollama-model nomic-embed-text:v1.5 \
  --embedding-cache local-private \
  --format json
```

`--retrieval hybrid` deterministically fuses text and semantic rankings. Both
semantic modes require every Ollama and cache option shown above; AgentMemory
does not read provider defaults from the environment, start Ollama, or download
models. They fail closed on provider, configuration, or embedding-integrity
errors. Hybrid may return text results only with
`--allow-hybrid-text-fallback`; JSON then reports a degraded `RetrievalOutcome`,
and text output prints a warning with a fixed reason code.

Hybrid result limits are capped at 470 so both source rankings can be fetched
deeply enough (`60 + 2 × limit`, at most 1000) for exact two-list RRF results.

Corrupt indexes are rebuilt only for recognized corruption errors. Lock
contention is never treated as corruption, and symlinked or hardlinked index
paths are rejected. SQLite operates on verified private snapshots and installs
updates atomically; failed installation restores the prior inode. Canonical
WAL, shared-memory, or hot-journal sidecars force `auto` mode to use the
Markdown scan rather than risk stale indexed results.

Validate links and orphan raw artifacts:

```bash
python3 agent_memory.py validate --root ~/opencode --strict
```

Run installation and runtime health checks:

```bash
python3 agent_memory.py doctor --root ~/opencode
```

## Structure under `--root`

- `memory/master.md`
- `memory/agents/{agent_id}.md`
- `memory/sessions/{session_id}.md`
- `actions/{Y}/{m}/{His.millisecond}-{memory_uuid}.md`
- `actions/{Y}/{m}/raw/{His.millisecond}-{memory_uuid}.raw`

## Heuristic phase switch

Logging starts in `context_building`.
It switches to `actions` when:

- any tool is used, or
- message text contains action-language triggers (`i will`, `implemented`, `fix`, `applied patch`, and similar).

While in `context_building`, log calls are skipped.

## Concurrency safety

Writes to index files, state files, action files, and raw artifacts use lock files (`*.lock`) with POSIX advisory locking.

## Security redaction

The logger redacts likely secrets before write:

- Authorization bearer headers
- API keys/tokens/secrets/password key-value patterns
- private key blocks
- high-entropy token-like strings

Identifiers used in paths are validated rather than normalized. Empty values,
`.`/`..`, separators, control characters, and unsupported characters are
rejected. Wiki links are resolved beneath the configured memory root before
they may be read.

## Retrieval roadmap

Markdown action documents remain authoritative. Search databases, embeddings,
profiles, and graph scores are disposable projections that must be rebuildable
from those documents.

The execution order and AgentFramework integration boundary are defined in
[Retrieval Architecture](RETRIEVAL_ARCHITECTURE.md). Deterministic text
retrieval remains first and model-backed embeddings never produce user-facing
answers.

Planned incremental layers are:

1. ~~SQLite FTS5 catalog with direct-scan fallback.~~
2. ~~Explicitly configured local embeddings and hybrid retrieval.~~
3. Provenance-backed profile candidates requiring approval before injection.
4. Opt-in, token-budgeted context construction that quotes memories as
   untrusted data.
5. Explicit project registry for cross-project recall.
6. Bounded PageRank-like importance over canonical memory links.
7. Optional Redis, OpenSearch, or MariaDB adapters for multi-host scale; none
   becomes authoritative and none is contacted implicitly.

Run the tests with:

```bash
python3 -m pip install -r requirements-dev.txt
python3 -m pytest -q
```
