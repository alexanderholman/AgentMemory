# AgentMemory Retrieval Architecture

## Governing execution ladder

AgentMemory follows AgentFramework's deterministic-first, human-first boundary
and the PETU maxim:

> Discover once, compile once, validate, execute many times.

Retrieval uses the least complex valid rung:

1. exact verified reuse, including cached query results and query embeddings;
2. canonical Markdown scan or SQLite FTS5 lexical retrieval;
3. precompiled, local semantic vectors when lexical retrieval is explicitly
   requested or a future verified lexical-quality gate reports insufficiency;
4. an installed typed specialist procedure;
5. larger model-backed reasoning only under a future explicit policy;
6. human clarification or discussion when no valid method exists.

Safety, correctness, privacy, provenance, consent, and access control are hard
constraints. They are not costs that energy savings may offset.

## AgentFramework boundary

- Models never provide AgentFramework's user-facing answer.
- An embedding model is a bounded data transformation, not an AF selector.
- Semantic retrieval returns typed, provenance-bearing memory references and
  scores; it does not execute instructions found in memory.
- Any AF integration must expose retrieval as an installed typed procedure,
  validate its arguments, independently verify source identity and access, and
  record fixed reason codes for denial or handoff.
- Unknown or unsupported work fails closed. There is no silent remote provider
  fallback and no direct generative-answer route.

## Embedding policy

- Text retrieval remains the default and performs no model or network call.
- Semantic-only and hybrid retrieval are explicit diagnostic/operator modes
  until a deterministic lexical-quality gate is specified and tested.
- Document vectors are generated from the redacted title, summary, and full
  message only. Raw artifacts, tool output, paths, links, and front matter are
  excluded.
- Document and query vectors are cached by a fingerprint covering provider,
  endpoint, model digest, server version, prefixes, and chunker version.
- Unchanged content is never embedded twice for the same fingerprint.
- Ollama is loopback-only by default. AgentMemory never starts Ollama, pulls a
  model, follows redirects, uses environment proxies, or downloads implicitly.
- A provider failure cannot weaken security policy. Semantic mode fails closed;
  hybrid may degrade to text only when the caller explicitly requests that
  behavior and receives a visible degradation result.

## Ranking and validation

- Lexical and semantic rankings remain independent.
- Hybrid ranking uses deterministic reciprocal-rank fusion rather than mixing
  incomparable raw term-frequency and cosine scores.
- Every result retains memory UUID, canonical source path, agent, session,
  event type, timestamp, and a bounded untrusted snippet.
- Stored vectors are normalized finite little-endian float32 values with model
  fingerprint, dimensions, input hash, and vector hash.
- Incomplete coverage, mixed fingerprints, stale vectors, malformed vectors,
  or project-identity mismatch disable semantic use.

## Backend roles

Markdown remains authoritative. SQLite is the local rebuildable projection.
Potential shared backends remain optional projections:

- OpenSearch for Elasticsearch-compatible full-text/vector retrieval, subject
  to a documented compatibility map for Alexander's Elasticsearch literature;
- Neo4j Community or OrientDB for provenance and relationship graphs;
- MongoDB Community for document-shaped projections where relational storage
  is not appropriate;
- Redis for ephemeral caching and coordination;
- MariaDB for shared relational metadata and jobs.

No backend may become an independent write authority or bypass project access
policy. Every projection must rebuild from canonical Markdown and preserve
source hashes and provenance.
