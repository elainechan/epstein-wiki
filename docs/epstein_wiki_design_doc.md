# Epstein File Wiki — System Design Document

**MVP Proof of Concept**
Investigative Journalism Research Platform · June 2026

| Field | Value |
|---|---|
| Version | 1.0 — MVP POC |
| Status | In Development |
| Build Window | 5-7 Days |
| Classification | Internal — Investigative Research |

---

## Table of Contents

1. [Purpose & Scope](#1-purpose--scope)
2. [Architecture Overview](#2-architecture-overview)
3. [Applied Anthropic Agent Patterns](#3-applied-anthropic-agent-patterns)
4. [3-Day Implementation Plan](#4-3-day-implementation-plan)
5. [MCP Tool Design (ACI)](#5-mcp-tool-design-aci)
6. [Observability & Evaluation](#6-observability--evaluation)
7. [Definition of Done](#7-definition-of-done)
8. [Upgrade Path to Full Build](#8-upgrade-path-to-full-build)
9. [Risks & Mitigations](#9-risks--mitigations)
10. [References](#10-references)

---

## 1. Purpose & Scope

This document describes the system architecture and agentic design patterns for the Epstein File Wiki MVP Proof of Concept. The POC delivers a working investigative research knowledge base that a journalist can query in plain language and receive grounded, cited answers drawn from a corpus of Epstein-related DOJ disclosure documents.

The design applies patterns from Anthropic's *Building Effective Agents* guidance. Each pattern is mapped to a concrete system component rather than adopted generically.

### 1.1 Goals
-
- Ingest DOJ Epstein disclosure PDFs into Semiont with entity annotation
- Index all chunks into OpenSearch for hybrid BM25 + k-NN retrieval
- Expose the knowledge base to Claude Desktop via the Semiont MCP server
- Trace every inference call in Langfuse
- Validate retrieval quality with Ragas and annotation quality with Promptfoo
- Be fully reproducible: one `docker compose up` and one ingest script

### 1.2 Explicit Non-Goals (deferred to full build)

Out of scope for this POC: Kubernetes, Terraform, AWS, LangGraph, AgentsView, Neo4j, Qdrant, Pinecone, ClickHouse, IndexedDB, Cognee, Gastown, Swamp, Caveman, DeepEval, RTK. Each is a named upgrade path requiring a single config change.

---

## 2. Architecture Overview

The system is a **workflow architecture — not an autonomous agent** — consistent with Anthropic's guidance to match complexity to need. All LLM calls are deterministic steps in a fixed pipeline except the Q&A layer, which is an augmented LLM responding to open-ended journalist queries.

### 2.1 Stack

| Component | Technology | Role |
|---|---|---|
| KB Platform | Semiont v0.5.7 | Event store, actors, CLI, React UI, MCP server |
| Vector + BM25 | OpenSearch (k-NN) | Persisted hybrid search — exact name + semantic |
| In-proc Graph | Semiont memory type | Entity linking; Neo4j upgrade in full build |
| Inference | Claude Haiku 4.5 | Annotation workers and Q&A; cost-optimised for POC |
| Embeddings | Ollama nomic-embed-text | Local, free, 768-dim; Voyage AI upgrade in full build |
| Observability | Langfuse (cloud free tier) | Traces every inference call; zero infra |
| RAG Eval | Ragas (Python) | Faithfulness, answer_relevancy, context_recall |
| Prompt Regression | Promptfoo (CLI) | Annotation detection prompt regression tests |
| Corpus Fetch | batch_download_epstein_files.py | Scrapes 12 DOJ datasets; 8 parallel workers |
| Orchestration | Docker Compose | Single command to start all services |

### 2.2 Data Flow — End to End

The system has two distinct flows: the **Ingest Pipeline** (batch, runs once per corpus) and the **Query Loop** (interactive, runs per journalist question).

#### Ingest Pipeline

```
justice.gov PDFs
  → batch_download_epstein_files.py  (8-worker parallel fetch)
  → raw/*.pdf
  → ingest.py  (semiont yield --upload, skip-on-rerun idempotent)
  → Semiont Smelter
  → chunk (512 tokens, 64 overlap)
  → Ollama embed
  → OpenSearch k-NN index
  → Gatherer worker (Haiku)  → extract Person / Org / Location / Event → JSON
  → Gate: valid JSON?  →  if no: retry (max 2) → else skip + log
  → Matcher worker (Haiku)  → resolve + link entities across documents
  → Gate: confidence ≥ 0.6?  →  if no: create new node
  → Bind  →  attach entity annotations to source chunks in Semiont
```

#### Query Loop

```
Journalist query (Claude Desktop)
  → MCP server receives query
  → Router classifies: BM25 / k-NN / hybrid
  → OpenSearch executes search → top-k chunks
  → Augmented Haiku call: query + chunks + entity graph context
  → Cited answer → Claude Desktop
  → Langfuse records: prompt, tokens, latency, model, route taken
```

---

## 3. Applied Anthropic Agent Patterns

Five patterns from Anthropic's *Building Effective Agents* are applied. The table below shows the full mapping before the detailed sections.

| Pattern | Where Applied | Day |
|---|---|---|
| 1 — Augmented LLM | Q&A interface: Claude Desktop + MCP + OpenSearch + memory graph | Day 2 |
| 2 — Prompt Chaining | Ingest annotation pipeline: gatherer → gate → matcher → bind | Day 1 |
| 3 — Parallelization (Sectioning) | Batch download (8 workers) + parallel per-document annotation | Day 1 |
| 4 — Routing | Query classifier: BM25 vs k-NN vs hybrid dispatch | Day 2 |
| 5 — Evaluator-Optimizer | Ragas + Promptfoo feedback loop on retrieval and annotation | Day 3 |

> **Patterns NOT used:** Orchestrator-Workers (requires LangGraph — full build) and Autonomous Agents (journalist is always in the loop; autonomous operation would compound errors without the evaluation infrastructure to catch them).

---

### Pattern 1: Augmented LLM — Core Q&A Building Block

> *Anthropic: "The basic building block of agentic systems is an LLM enhanced with retrieval, tools, and memory."*

**Applied to:** The journalist-facing query interface — Claude Desktop + MCP + OpenSearch + in-proc entity graph.

The three augmentations:

| Augmentation | Implementation |
|---|---|
| Retrieval | OpenSearch BM25 + k-NN returns relevant chunks for every query |
| Tools | Semiont MCP server exposes `query-kb`, `fetch-resource`, `filter-by-entity`, `filter-by-date` |
| Memory | In-proc entity graph surfaces who appears across which documents |

**Model:** Claude Haiku 4.5 — sufficient for cite-and-answer; upgrade to Sonnet in full build.

**Why not an Agent?** Q&A is a single-turn augmented call. Adding autonomous multi-step loops would increase cost and error rate without improving answer quality for journalist queries.

**Implementation note:** The MCP server is the single integration point between Claude Desktop and all KB capabilities. The journalist never calls OpenSearch directly — all search is mediated through MCP tools. This keeps the ACI (agent-computer interface) clean and fully observable via Langfuse.

---

### Pattern 2: Prompt Chaining — Ingest Annotation Pipeline

> *Anthropic: "Decomposes a task into a sequence of steps, where each LLM call processes the output of the previous one."*

**Applied to:** The per-document annotation pipeline that runs during corpus ingestion.

```
Step 1  Gatherer (Haiku, 4096 tokens)
        → Extract all Person / Org / Location / Event entities from chunk text
        → Output: JSON array with { type, name, span } per entity

Gate 1  Programmatic: is output valid JSON? Does each entity have required fields?
        → NO: retry with clarification prompt (max 2 retries, then skip + log)
        → YES: pass to Step 2

Step 2  Matcher (Haiku, 2048 tokens)
        → Given extracted entities + existing graph state
        → Resolve coreferences, link to known entities
        → Output: matched entity IDs with confidence scores

Gate 2  Programmatic: confidence ≥ 0.6?
        → NO: create new entity node (don't force a bad match)
        → YES: bind to existing node

Step 3  Bind (no LLM)
        → Attach matched entities to source chunks in Semiont
        → Purely deterministic write
```

**Key design rule:** Each Haiku call is single-purpose. The gatherer prompt contains no resolution logic; the matcher prompt contains no extraction logic. Anthropic's guidance: *"make each LLM call an easier task"* — applied literally here.

> **Gate implementation:** Gates are two-line Python checks (`json.loads` + field validation), not LLM calls. Programmatic gates are faster, cheaper, and more reliable than using an LLM to validate LLM output at this stage.

---

### Pattern 3: Parallelization (Sectioning) — Batch Download & Annotation

> *Anthropic: "Parallelization is effective when divided subtasks can be parallelized for speed."*

**Applied to:** Two stages — corpus download and per-document annotation.

| Stage | Implementation |
|---|---|
| Download | `batch_download_epstein_files.py`: 8 concurrent workers fetch PDFs from justice.gov with 300ms polite delay and resume-safe state |
| Annotation | `ingest.py`: per-document annotation chains run in parallel across the corpus — each document is independent, no cross-document state needed during extraction |
| Embedding | Ollama `embed()` accepts a list of inputs — chunk embeddings are batched per document (up to 20 chunks) rather than called one at a time |

**Voting?** Not used. A single annotation pass per document is sufficient for the POC's quality bar (Ragas > 0.7). Voting would triple Haiku costs with marginal benefit.

**Constraint:** OpenSearch k-NN plugin is memory-heavy. Set `ES_JAVA_OPTS=-Xms512m -Xmx512m` in docker-compose for single-node POC to prevent OOMKills.

**Practical scope:** Run the scraper on datasets 1–3 first (~30–80 files). Sufficient to validate the full pipeline end-to-end within the 3-day window. Full 12-dataset ingestion can run overnight once the pipeline is confirmed working.

---

### Pattern 4: Routing — Query-Type Dispatch to Retrieval Path

> *Anthropic: "Routing classifies an input and directs it to a specialized follow-up task, allowing separation of concerns."*

**Applied to:** The query classification step that precedes every OpenSearch call.

| Route | Trigger | OpenSearch Path | Example Query |
|---|---|---|---|
| EXACT | Specific name, date, flight number, or location in query | BM25 only | "Find all documents that mention Ghislaine Maxwell" |
| SEMANTIC | Conceptual or relational question | k-NN only | "Who had access to the private island?" |
| HYBRID (default) | Ambiguous or benefits from both | BM25 + k-NN combined scorer | "What do the flight logs say about Palm Beach?" |

**Implementation:** Lightweight classifier — regex heuristics (quoted strings, proper nouns, date patterns → EXACT) or a single Haiku classify call for ambiguous cases. Not a full LLM call for every query.

**Why routing?** Without routing, a semantic query sent to BM25 returns noise; an exact-name query sent to k-NN may miss precise hits. Routing is what makes BM25 and k-NN complementary rather than redundant.

**ACI implication:** Every MCP tool description explicitly states which route it uses. The model should understand from the tool description alone — not from implementation inspection — when to pass `exact_match=true`.

---

### Pattern 5: Evaluator-Optimizer — Ragas + Promptfoo Feedback Loop

> *Anthropic: "One LLM call generates a response while another provides evaluation and feedback in a loop."*

**Applied to:** Two separate evaluation loops — retrieval quality (Ragas) and annotation quality (Promptfoo).

```
Eval Loop A — Retrieval Quality (Ragas)
  Generator:  Q&A system (gatherer + matcher + augmented LLM)
  Evaluator:  Ragas scores faithfulness, answer_relevancy, context_recall
  Loop:       Score < 0.7 → identify failing questions → revise prompt → re-run

Eval Loop B — Annotation Quality (Promptfoo)  
  Generator:  Gatherer entity-extraction prompt
  Evaluator:  4 regression tests (entity extraction, no hallucination, valid JSON)
  Loop:       Any test fails → fix annotation prompt → re-run
```

**Loop trigger:** Any change to gatherer or matcher prompts re-runs both loops. The loops are the gate — no prompt ships without passing evals.

**Optimizer:** In this POC the human engineer is the optimizer: reads Ragas/Promptfoo output, revises the prompt, re-runs. LangGraph-based automated optimization is the full-build upgrade.

**Day structure:** Loops are designed on Day 1 alongside the pipeline. Day 3 runs the first full eval pass and records baseline scores. Subsequent prompt iterations happen within Day 3 afternoon.

> **Baseline requirement:** Ragas `faithfulness` and `answer_relevancy` > 0.7 is a hard Definition of Done criterion. If Day 3 evals fall below this threshold, prompt iteration continues until the threshold is met or the limitation is documented.

---

## 4. 3-Day Implementation Plan

### Day 1 — Environment, Corpus, Ingest Pipeline

**Goal:** Semiont running, all source documents uploaded and entity-annotated. OpenSearch index created and populated.

| Time | Task | Pattern |
|---|---|---|
| Morning (3–4 hr) | Prerequisites: Docker Desktop, Node 20+, Anthropic API key. Install Semiont CLI. `semiont init`. | — |
| Morning | Configure `~/.semiontconfig`: OpenSearch k-NN, Ollama embeddings, Haiku for annotation workers. | Augmented LLM (setup) |
| Morning | `semiont provision` + `semiont start`. Create OpenSearch index with k-NN + BM25 mapping (768-dim, HNSW, cosinesimil). | — |
| Midday (1 hr) | Define entity frame flow: Person, Organisation, Location, Event. Run once before ingestion. | Prompt Chaining (setup) |
| Afternoon (3 hr) | Run `batch_download_epstein_files.py` on datasets 1–3 (8 parallel workers). Run `ingest.py` — upload, embed, dual-index. | Parallelization |
| Afternoon | Verify: `curl` OpenSearch `_count` confirms chunks indexed. BM25 test on known name. k-NN test on semantic query. | — |
| Afternoon | Implement gatherer + matcher + bind annotation chain with JSON gate between gatherer and matcher. | Prompt Chaining |
| Afternoon | Confirm entity annotations visible in Semiont UI at `localhost:3000`. | — |

### Day 2 — MCP Server, Query Interface, Routing, Observability

**Goal:** Claude Desktop connected to the KB, routing working, all inference calls traced in Langfuse.

| Time | Task | Pattern |
|---|---|---|
| Morning (2 hr) | `semiont start --service mcp`. Connect Claude Desktop. Verify MCP tool list visible. | Augmented LLM |
| Morning | Define and document 4 MCP tools with full ACI documentation per Anthropic Appendix 2 guidance. | Augmented LLM |
| Morning | Implement query classifier: regex heuristics for EXACT route; Haiku classify call for ambiguous queries. | Routing |
| Midday (2 hr) | Wire classifier output to BM25 / k-NN / hybrid OpenSearch call in MCP query tool. | Routing |
| Midday | Configure Langfuse. Add keys to `.env`. Wrap every Haiku call with Langfuse trace decorator. | Augmented LLM (observability) |
| Afternoon (2 hr) | Run 6 demo test queries: 1 pure BM25, 1 pure k-NN, 4 hybrid. Verify citations in answers. | — |
| Afternoon | Open Langfuse dashboard. Confirm all 6 queries traced with prompt, tokens, latency, model, route taken. | — |

### Day 3 — Evaluation, Regression, Demo Prep

**Goal:** Ragas scores > 0.7 recorded as baseline. Promptfoo tests passing. Full system reproducible.

| Time | Task | Pattern |
|---|---|---|
| Morning (2 hr) | Write `eval/questions.json`: 20 journalist test questions with ground truth answers and expected source documents. | Evaluator-Optimizer (setup) |
| Morning | Run `eval/run_ragas.py`. Record faithfulness, answer_relevancy, context_recall in `eval/ragas_results.csv`. | Evaluator-Optimizer |
| Morning | If any metric < 0.7: identify failing questions, revise relevant prompt, re-run Ragas. Budget 2–3 iterations. | Evaluator-Optimizer |
| Midday (1 hr) | Write 4 Promptfoo test cases: entity extraction, location extraction, no hallucination, valid JSON output. | Evaluator-Optimizer |
| Midday | Run `promptfoo eval`. All 4 tests must pass. Fix annotation prompt if any fail. | Evaluator-Optimizer |
| Midday | Restart Docker Compose. Re-run `curl` OpenSearch `_count` to confirm index survived restart (persistence check). | — |
| Afternoon (2 hr) | Reproducibility check: fresh terminal, `git clone`, 5-step setup script, all services up in < 30 min. | — |
| Afternoon | 5-minute demo walkthrough: Semiont UI → BM25 curl → Claude Desktop 6 queries → Langfuse traces → Ragas CSV → Promptfoo report. | — |

---

## 5. MCP Tool Design (ACI)

Per Anthropic Appendix 2: *"Tool definitions and specifications should be given just as much prompt engineering attention as your overall prompts."* Each MCP tool is documented with name, description, parameters, return format, and explicit route annotation.

---

### `query-kb`

Search the Epstein wiki knowledge base. Route is auto-selected: pass `exact_match=true` for BM25 precision (specific names, dates); leave `false` for semantic search (conceptual questions).

**Parameters:**

| Parameter | Type | Default | Description |
|---|---|---|---|
| `query` | string | required | The journalist's question |
| `top_k` | int | 5 | Number of chunks to return |
| `exact_match` | bool | false | true → BM25 route; false → k-NN or hybrid |
| `doc_type` | keyword | optional | Filter by document type |
| `date_range` | ISO range | optional | Filter by document date |

**Returns:** `array of { chunk_text, source_doc, page, entities, relevance_score, route_used }`

---

### `fetch-resource`

Retrieve the full text of a specific source document by resource ID. Use when `query-kb` returns a promising document and you need the surrounding context.

**Parameters:** `resource_id` (string, required)

**Returns:** `{ full_text, metadata, entity_annotations }`

---

### `filter-by-entity`

Return all documents that contain annotations for a specific named entity. Use for person-centric research ("All documents mentioning [name]").

**Parameters:**

| Parameter | Type | Description |
|---|---|---|
| `entity_name` | string | Name of the entity to search for |
| `entity_type` | enum | `Person \| Org \| Location \| Event` (optional) |

**Returns:** `array of { resource_id, source_doc, annotation_count, entity_spans }`

---

### `filter-by-date`

Filter knowledge base chunks by document date range. Useful for timeline analysis.

**Parameters:**

| Parameter | Type | Description |
|---|---|---|
| `start_date` | ISO date | Start of date range |
| `end_date` | ISO date | End of date range |
| `query` | string | Optional — combines semantic search with date filter |

**Returns:** array of chunks with date metadata

---

> **ACI rule applied:** Every tool description includes the routing decision rationale. The model should not need to infer when to use which route — the description makes it explicit. This follows Anthropic's guidance to *"make it obvious how to use the tool based on description and parameters alone."*

---

## 6. Observability & Evaluation

Every inference call is traced. Every prompt ships with a regression test. This is the minimum viable observability posture for an investigative journalism tool where citation accuracy is non-negotiable.

### 6.1 Langfuse Tracing

Fields captured on every Haiku call (gatherer, matcher, query Q&A, classifier):

- `prompt_text` — full prompt sent
- `completion_text` — full model response
- `model` — model string
- `input_tokens` / `output_tokens`
- `latency_ms`
- `route_used` — `BM25 | k-NN | hybrid`
- `session_id` — journalist query session

**Fallback:** If Langfuse SDK integration with Semiont 0.5.7 is blocked, wrap inference calls in a thin Python script with manual Langfuse SDK instrumentation around the CLI calls.

### 6.2 Ragas Evaluation

| Metric | Target | Interpretation |
|---|---|---|
| `faithfulness` | > 0.7 | Answer claims are grounded in retrieved chunks. Low score = hallucination. |
| `answer_relevancy` | > 0.7 | Answer addresses the journalist's actual question. Low score = retrieval or prompt drift. |
| `context_recall` | > 0.7 | Retrieved chunks contain the information needed to answer. Low score = retrieval coverage gap. |

### 6.3 Promptfoo Regression Tests

| Test | Assertion |
|---|---|
| Extracts person names from court filing excerpt | Output contains "Jane Doe" and "John Smith"; output is valid JSON |
| Extracts location from document | Output contains "East 71st Street"; LLM rubric: location entity identified |
| Does not hallucinate from ambiguous text | LLM rubric: response does not invent specific names or entities |
| Returns valid JSON array of ≥ 2 entities | `is-json` assertion + JavaScript: `Array.isArray(output) && output.length >= 2` |

---

## 7. Definition of Done

The POC is complete when all of the following are confirmed:

| Criterion | Verification |
|---|---|
| Services start in one command | `semiont start` brings Semiont + OpenSearch + Ollama up from clean repo clone |
| Corpus downloaded | `batch_download_epstein_files.py` fetches DOJ disclosure PDFs into `raw/` |
| Chunks indexed and persisted | `curl http://localhost:9200/epstein-wiki/_count` returns non-zero after Docker restart |
| BM25 exact-name search works | Known name (e.g. Ghislaine Maxwell) returns correct document hits |
| k-NN semantic search works | Flight-related question returns relevant flight log chunks |
| Entity annotations visible | Person / Org / Location / Event annotations appear on documents in Semiont UI |
| Claude Desktop answers 6 queries | Answers include cited sources; at least one validates BM25, one validates k-NN |
| All inference calls traced | Langfuse shows trace for every MCP query with prompt, tokens, latency |
| Ragas baseline recorded | `faithfulness` and `answer_relevancy` both > 0.7 in `eval/ragas_results.csv` |
| Promptfoo tests pass | All 4 annotation prompt regression tests pass |
| Reproducible in < 30 min | Colleague can complete 5-step setup from README without prior knowledge |

---

## 8. Upgrade Path to Full Build

The POC uses in-memory stores and single-node services to eliminate infrastructure setup time. Each component can be swapped independently via a single config change — no application code changes required.

| POC Component | Full Build Upgrade | Config Change |
|---|---|---|
| OpenSearch single-node (local) | OpenSearch multi-node cluster on AWS | Change `host`/`port` to AWS OpenSearch Service endpoint |
| Memory graph (in-proc) | Neo4j | `make-meaning.graph.type = "neo4j"` + URI/credentials |
| Ollama nomic-embed-text (768-dim) | Voyage AI voyage-3 (1024-dim) | `embedding.type = "voyage"` + `apiKey`; update index mapping `dimension` |
| Docker Compose (local) | Kubernetes on EKS + Terraform | `platform = "aws"` in `semiont.toml` |
| Langfuse cloud free tier | Langfuse self-hosted | Change `host` in tracing config section |
| Manual prompt optimization | LangGraph / CrewAI Linker Agent | Add agent layer on top of `semiont match + bind` CLI calls |
| Claude Haiku 4.5 | Claude Sonnet or Opus for Q&A | Change `model` string in `workers.default.inference` config |

> **Pattern upgrade:** The Evaluator-Optimizer pattern currently has a human in the optimization loop. The full build replaces this with an automated LangGraph agent that reads Ragas scores, generates prompt variants, tests them, and promotes the best performer — removing the manual iteration step entirely.

---

## 9. Risks & Mitigations

| Risk | Likelihood | Mitigation |
|---|---|---|
| OpenSearch OOMKill (k-NN plugin is memory-heavy) | High | Set `ES_JAVA_OPTS=-Xms512m -Xmx512m` in docker-compose. Single-node with 50-doc corpus is fine at 1 GB. |
| Full 12-dataset download too large for 3-day window | High | Run scraper on datasets 1–3 first (~30–80 files). Sufficient to validate pipeline end-to-end. |
| Ollama `embed()` slow for large chunk counts | Medium | Batch Ollama calls (accepts list input). 50 docs × 20 chunks ≈ 1,000 calls — run overnight if needed. |
| Ragas scores < 0.7 on first pass | Medium | Evaluator-Optimizer loop is the designed response: read failing questions, revise gatherer or Q&A prompt, re-run. Budget 2–3 iterations on Day 3 afternoon. |
| Langfuse SDK not compatible with Semiont 0.5.7 | Low–Medium | Fall back to manual SDK wrapping in a thin Python script around the CLI inference calls. |
| justice.gov URL structure changes / blocks scraper | Low–Medium | Script uses 300ms delay + standard User-Agent. If blocked, download manually from justice.gov/epstein — `ingest.py` is source-agnostic. |
| MCP OAuth setup blocks Claude Desktop | Low | `semiont provision --service mcp` handles this. Fallback: use Semiont REST API directly from Claude Desktop custom tool. |
| PDF text extraction poor on scanned documents | Medium | Use text-layer PDFs where possible. Scanned PDFs require OCR pre-processing (out of scope; document the limitation). |

---

## 10. References

- Anthropic Engineering: *Building Effective Agents* (Dec 2024)
- Epstein File Wiki — MVP POC 3-Day Build Plan (internal, June 2026)
- Semiont v0.5.7 documentation — Apache-2.0
- OpenSearch k-NN documentation — Apache-2.0
- Ragas documentation — Apache-2.0
- Langfuse documentation — MIT
- Promptfoo documentation — MIT

---

*Epstein File Wiki · System Design Document · v1.0 · June 2026 · Internal*
