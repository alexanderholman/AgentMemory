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
- `memory/agents/{agent_name}.md`
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
