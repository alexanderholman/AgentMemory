# AgentMemory Retrieval Architecture

## Governing execution ladder

AgentMemory follows AgentFramework's deterministic-first, human-first boundary
and the PETU maxim:

> Discover once, compile once, validate, execute many times.

Algorithms are functions. The ladder distinguishes simple functions from
increasingly complex systems and compositions of functions:

0. cached result or other exact verified reuse;
1. simple deterministic function;
2. complex deterministic function system, including traditional algorithms,
   database/FTS queries, cosine ranking, PageRank, and deterministic DAGs;
3. specialist model;
4. adaptive agent workflow composed from registered capabilities;
5. large reasoning or generative model;
6. human clarification or external expertise.

AF first selects the lowest ladder level that satisfies all declared
requirements, availability, effective scope, and required quality threshold;
it then minimizes cost among qualifying capabilities at that level. Cost
includes at least latency, energy/compute, monetary cost, privacy exposure, and
operational risk; capability evidence includes accuracy, availability,
provenance, and prior verified execution history.

Safety, correctness, privacy, provenance, consent, and access control are hard
constraints. They are not costs that energy savings may offset.

## AgentFramework boundary

- An embedding model is a bounded data transformation, not an AF selector.
- Semantic retrieval returns typed, provenance-bearing memory references and
  scores; it does not execute instructions found in memory.
- Any AF integration must expose retrieval as an installed typed procedure,
  validate its arguments, independently verify source identity and access, and
  record fixed reason codes for denial or handoff.
- A planner may propose only a typed DAG of registered capabilities. AF
  validates every node, edge, argument, requirement, scope, and policy before
  execution; a planner may not silently install code or bypass the catalogue.
- Model-generated answers are allowed only when the request's acceptance scope
  explicitly permits them. They are expected to be rare, reviewed before
  release, provenance-bearing, and cached only when the scope's reuse policy
  permits caching.
- Unknown, unsupported, unreviewed, or out-of-scope work fails closed. There is
  no silent remote provider fallback.

## User acceptance scopes

Every request carries a typed scope preference. Effective scope is the
intersection of that preference with trusted identity, session, tenant,
parental, and delegation policy; callers and child contexts can never widen
their own authority. Initial policy classes are:

- `child`: automated levels 0–2; deterministic procedural responses only; no
  generative model, generative planner, generated answer, or model-derived
  cached artifact;
- `deterministic`: automated levels 0–2 with deterministic provenance and reuse
  only;
- `reviewed-generative`: rungs 3–5 may be used when lower rungs cannot satisfy
  quality and requirements; generated answers require review before release;
- `operator-evaluation`: explicitly requested levels 0–5 model or hybrid modes
  for testing and benchmarking, with no production release or cross-user reuse
  and `no-store` by default.

Every scope permits a non-automated level-6 human clarification or handoff. A
handoff does not authorize a higher automated level.

Caching is independently scoped. A reviewed output is not automatically safe
to cache; sensitive, personal, short-lived, user-specific, or non-replay-safe
results may be marked `no-store`. Cached artifacts retain originating ladder
level and model-derived taint; caching never lowers their authority
requirements. Reusable generated results require retained inputs,
model/capability fingerprint, derivation graph, audience/tenant eligibility,
provenance, digest- and scope-bound review evidence, expiry, revocation state,
and independent verification on every reuse.

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
- A query-embedding cache hit avoids repeated execution but retains its rung-3
  model-derived origin; cosine search over existing vectors is rung 2. Creating
  a missing query or document embedding invokes the rung-3 specialist model.
  Child and deterministic scopes use lexical retrieval and cannot consume
  model-derived embedding caches.
- Direct Ollama use is restricted to operator-evaluation scope and requires an
  explicit loopback IP endpoint, model, and `local-private` embedding-cache
  opt-in on each CLI invocation. AgentMemory never reads provider environment
  defaults, starts Ollama, pulls a model, follows redirects, uses environment
  proxies, or downloads implicitly.
- A provider failure cannot weaken security policy. Semantic mode fails closed;
  hybrid may degrade to text only when the caller explicitly requests that
  behavior and receives a visible degradation result.

The search CLI keeps `--mode auto|index|scan` as the lexical backend selector.
The separate `--retrieval text|semantic|hybrid` selector defaults to `text`.
Semantic and hybrid JSON responses use a `RetrievalOutcome` containing the
requested and effective modes, degradation state and fixed reason code,
provider fingerprint digest, and results. Default text JSON remains a bare
result array for compatibility.
Hybrid fetch depth is `RRF k + 2 × result limit`; the CLI caps the result limit
at 470 so both rankings remain within the 1000-candidate validation bound.

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
