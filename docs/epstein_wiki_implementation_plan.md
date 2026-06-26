# Epstein File Wiki — Implementation Plan

**MVP Proof of Concept · 3-Day Build**
Investigative Journalism Research Platform · June 2026

| Field | Value |
|---|---|
| Version | 1.0 |
| Status | In Development |
| Build Window | Days 1–3 |
| Stack | Semiont · OpenSearch · Haiku · Ollama · Langfuse · Ragas · Promptfoo |

---

## Overview

A journalist can open Claude Desktop, ask a question about the Epstein DOJ disclosure corpus, and receive a grounded, cited answer drawn from hybrid BM25 + semantic search over persisted embeddings — with every inference call traced and retrieval quality measured.

| Day | Theme | Exit Condition |
|---|---|---|
| Day 1 | Environment up, corpus ingested, entities annotated | OpenSearch `_count` > 0; BM25 + k-NN both returning results; entity annotations visible in Semiont UI |
| Day 2 | Q&A live, routing wired, tracing on | 6 test queries answered in Claude Desktop with citations; all traces visible in Langfuse |
| Day 3 | Eval baseline recorded, prompts regression-tested, demo-ready | Ragas faithfulness + answer_relevancy > 0.7; all Promptfoo tests pass; reproducible from clean clone in < 30 min |

---

## Prerequisites

Before starting Day 1, confirm all of the following are in place:

- [ ] Docker Desktop installed and running
- [ ] Node 20+ and npm installed (`node --version`)
- [ ] Python 3.10+ installed (`python3 --version`)
- [ ] Anthropic API key set in shell: `export ANTHROPIC_API_KEY=sk-...`
- [ ] Semiont CLI installed: `npm install -g @semiont/cli`
- [ ] `semiont --version` returns successfully

---

## Day 1 — Environment, Corpus, Ingest Pipeline

**Goal:** Semiont running, all source documents uploaded and entity-annotated. OpenSearch index created and populated with chunks and embeddings.

---

### 1.1 Project Init

```bash
mkdir epstein-wiki && cd epstein-wiki
semiont init --name epstein-wiki
# Creates .semiont/config — commit this
```

### 1.2 Configure `~/.semiontconfig`

Minimal local config — paste and edit with your credentials:

```toml
[user]
name  = "Your Name"
email = "you@example.com"

[defaults]
environment = "local"
platform    = "container"

[environments.local.backend]
port       = 3001
publicURL  = "http://localhost:3001"
frontendURL = "http://localhost:3000"
corsOrigin  = "http://localhost:3000"

[environments.local.site]
domain        = "localhost"
siteName      = "Epstein Wiki (local)"
adminEmail    = "you@example.com"
enableLocalAuth = true

[environments.local.database]
host     = "localhost"
port     = 5432
name     = "semiont_local"
user     = "postgres"
password = "${POSTGRES_PASSWORD}"

# In-memory graph — no Neo4j needed for POC
[environments.local.make-meaning.graph]
type = "memory"

# OpenSearch k-NN — persisted vector embeddings + BM25 hybrid search
[environments.local.vectors]
type  = "opensearch"
host  = "localhost"
port  = 9200
index = "epstein-wiki"

# Local Ollama embeddings — free, no API key
[environments.local.embedding]
platform = "external"
type     = "ollama"
model    = "nomic-embed-text"
baseURL  = "http://localhost:11434"

[environments.local.embedding.chunking]
chunkSize = 512
overlap   = 64

# Haiku for annotation workers and Q&A
[environments.local.make-meaning.actors.gatherer.inference]
type      = "anthropic"
model     = "claude-haiku-4-5-20251001"
maxTokens = 4096
apiKey    = "${ANTHROPIC_API_KEY}"

[environments.local.make-meaning.actors.matcher.inference]
type      = "anthropic"
model     = "claude-haiku-4-5-20251001"
maxTokens = 2048
apiKey    = "${ANTHROPIC_API_KEY}"

[environments.local.workers.default.inference]
type      = "anthropic"
model     = "claude-haiku-4-5-20251001"
maxTokens = 4096
apiKey    = "${ANTHROPIC_API_KEY}"
```

> `POSTGRES_PASSWORD` and `ANTHROPIC_API_KEY` must be set in your shell before running `semiont start`. Add them to a `.env` file and `source` it — do not commit the file.

### 1.3 Start Services

```bash
semiont provision   # first-time only: sets up DB schema, creates admin user
semiont start       # starts backend, frontend, Ollama, OpenSearch via Docker Compose
semiont check       # confirm all services healthy

# Confirm OpenSearch is up
curl -s http://localhost:9200/_cluster/health | python3 -m json.tool
# Expect: "status": "green" or "yellow" (yellow is fine for single-node)

# Open http://localhost:3000 — Semiont UI should be visible
semiont login --bus http://localhost:3001 --user you@example.com
```

### 1.4 Create the OpenSearch Index

Run once before ingestion. The `nomic-embed-text` model outputs 768-dimension vectors.

```bash
curl -X PUT http://localhost:9200/epstein-wiki \
  -H 'Content-Type: application/json' -d '{
  "settings": {
    "index": {
      "knn": true,
      "knn.space_type": "cosinesimil",
      "number_of_shards": 1,
      "number_of_replicas": 0
    }
  },
  "mappings": {
    "properties": {
      "resource_id": { "type": "keyword" },
      "chunk_index": { "type": "integer" },
      "text":        { "type": "text", "analyzer": "english" },
      "doc_type":    { "type": "keyword" },
      "dataset_num": { "type": "integer" },
      "filename":    { "type": "keyword" },
      "embedding":   {
        "type": "knn_vector",
        "dimension": 768,
        "method": {
          "name": "hnsw",
          "space_type": "cosinesimil",
          "engine": "nmslib"
        }
      }
    }
  }
}'

# Confirm index created
curl -s http://localhost:9200/epstein-wiki | python3 -m json.tool
```

> **Memory constraint:** The k-NN plugin is memory-heavy. Set `ES_JAVA_OPTS=-Xms512m -Xmx512m` in `docker-compose.yml` for the OpenSearch service to prevent OOMKills on a single-node POC.

### 1.5 Define Entity Frame Flow

Run once before ingestion to register the entity types this KB recognises.

```bash
semiont frame --entity-type Person    --label "Person"
semiont frame --entity-type Org       --label "Organisation"
semiont frame --entity-type Location  --label "Location"
semiont frame --entity-type Event     --label "Event"
semiont frame --entity-type Document  --label "Document"
semiont frame --entity-type Flight    --label "Flight record"
semiont frame --entity-type Property  --label "Property / address"
```

### 1.6 Project File Layout

```
epstein-wiki/
  raw/
    dataset_1/          # DOJ disclosure set 1 PDFs
    dataset_2/          # DOJ disclosure set 2 PDFs
    ...                 # up to dataset_12
  scripts/
    batch_download_epstein_files.py   # scrapes justice.gov and downloads all PDFs
    ingest.py                         # uploads PDFs to Semiont + indexes to OpenSearch
    create_opensearch_index.sh        # idempotent index creation (run once)
  eval/
    dataset.json        # ground truth Q&A pairs
    run_ragas.py        # Ragas evaluation script
  promptfoo.yaml        # Promptfoo regression config
  .semiont/
  .env                  # NEVER commit
```

### 1.7 Download the Corpus

Install scraper dependencies and run `batch_download_epstein_files.py`:

```bash
pip install requests beautifulsoup4

python scripts/batch_download_epstein_files.py
# Output:
#   epstein_files/dataset_1/ ... dataset_12/  (all PDFs)
#   epstein_files.zip                         (full archive)

mv epstein_files/* raw/
```

**How the scraper works:** Targets 12 paginated dataset pages at `justice.gov/epstein/doj-disclosures/data-set-{N}-files`. For each dataset it harvests all PDF links, follows `Next` pagination until exhausted, and downloads in parallel with a `ThreadPoolExecutor` (8 workers). Already-downloaded files are skipped on re-run. A 300ms polite delay between page scrapes avoids rate-limiting.

> **POC corpus size:** For a 3-day build, run on datasets 1–3 only first — change `range(1,13)` to `range(1,4)` in the script. This produces ~30–80 files, sufficient to validate the full pipeline end-to-end. Full 12-dataset ingestion can run overnight once the pipeline is confirmed working. Running the full corpus will consume significant Anthropic API tokens during annotation.

### 1.8 Ingest: Upload to Semiont + Dual-Index to OpenSearch

`ingest.py` uploads every PDF to Semiont, extracts text, chunks it, embeds each chunk with Ollama, and bulk-indexes to OpenSearch.

```bash
pip install pypdf ollama requests

python scripts/ingest.py
```

**What ingest.py does per file:**

1. `semiont yield --upload <path>` → registers the document in Semiont, returns `resource_id`
2. Extracts text with `pypdf` (PDF) or reads directly (`.md`, `.txt`)
3. Splits into 512-word chunks with 64-word overlap
4. Infers `doc_type` from filename keywords (`court-filing`, `deposition`, `flight-log`, `press`, `document`)
5. Calls `ollama.embed(model="nomic-embed-text", input=chunk)` for each chunk
6. Bulk-indexes all chunks with embeddings to OpenSearch using `_id = {resource_id}_{chunk_index}` (idempotent re-runs update rather than duplicate)
7. Posts `/{OS_INDEX}/_refresh` after all files to make chunks immediately searchable

**Verify both stores are populated:**

```bash
semiont browse resources | python3 -c \
  "import json,sys; r=json.load(sys.stdin); print(f'{len(r[0])} resources in Semiont KB')"

curl -s 'http://localhost:9200/epstein-wiki/_count' | python3 -m json.tool
# Expect: "count" = (number of resources × average chunks per doc)
```

### 1.9 Run Annotation Workers — Entity Extraction (Prompt Chaining)

For each ingested resource, run the mark `--delegate` worker to extract entity references. This runs the **Prompt Chaining** pipeline: Gatherer → Gate → Matcher → Bind.

```bash
# Get all resource IDs
RESOURCE_IDS=$(semiont browse resources | python3 -c \
  "import json,sys; [print(r['@id']) for r in json.load(sys.stdin)[0]]")

# For each resource, run entity extraction for each entity type
for RID in $RESOURCE_IDS; do
  echo "Annotating $RID..."
  semiont mark $RID --delegate --motivation linking --entity-type Person
  semiont mark $RID --delegate --motivation linking --entity-type Org
  semiont mark $RID --delegate --motivation linking --entity-type Location
  semiont mark $RID --delegate --motivation linking --entity-type Event
  semiont mark $RID --delegate --motivation highlighting
done
```

**Chain behaviour:**

- **Gatherer (Haiku, 4096 tokens):** extracts all entities from the chunk, outputs a JSON array of `{ type, name, span }`
- **Gate 1 (programmatic):** validates JSON structure and required fields. If invalid: retry with clarification prompt, max 2 retries, then skip and log
- **Matcher (Haiku, 2048 tokens):** resolves coreferences, links extracted entities to existing graph nodes
- **Gate 2 (programmatic):** if confidence < 0.6, creates a new entity node rather than force a bad match
- **Bind (no LLM):** attaches matched entity IDs to source chunks — deterministic write

> **Parallelization:** Run the annotation loop in parallel across documents — each document is fully independent. Use Python `concurrent.futures.ThreadPoolExecutor` with `max_workers=4` to run `semiont mark` calls concurrently and cut annotation time by ~4×.

### 1.10 Verify Hybrid Search

Confirm BM25 and k-NN are both working before moving to Day 2.

```bash
# BM25 exact-name search
curl -s -X POST 'http://localhost:9200/epstein-wiki/_search' \
  -H 'Content-Type: application/json' -d '{
  "query": { "match": { "text": "Ghislaine Maxwell" } },
  "size": 3,
  "_source": ["filename", "doc_type", "chunk_index"]
}' | python3 -m json.tool

# k-NN semantic search — embed a question, find nearest chunks
python3 - << 'EOF'
import ollama, requests, json
q = "Who travelled on Epstein's private jet?"
vec = ollama.embed(model='nomic-embed-text', input=q)['embeddings'][0]
resp = requests.post('http://localhost:9200/epstein-wiki/_search',
    json={"size": 3,
          "query": {"knn": {"embedding": {"vector": vec, "k": 3}}},
          "_source": ["filename", "doc_type", "text"]})
for h in resp.json()['hits']['hits']:
    print(h['_score'], h['_source']['filename'])
    print(' ', h['_source']['text'][:120])
    print()
EOF
```

---

### ✅ Day 1 Checkpoint

Before ending Day 1, confirm all of the following:

- [ ] `batch_download_epstein_files.py` has populated `raw/` with PDFs from justice.gov
- [ ] `semiont browse resources` shows all uploaded documents
- [ ] `curl http://localhost:9200/epstein-wiki/_count` returns a non-zero count
- [ ] BM25 search for "Ghislaine Maxwell" returns document hits
- [ ] k-NN search for a flight-related question returns relevant chunks
- [ ] Entity annotations (Person, Org, Location, Event) are visible on documents in the Semiont UI at `localhost:3000`

---

## Day 2 — MCP Server, Query Interface, Routing, Observability

**Goal:** Claude Desktop connected to the KB. Routing working. All inference calls traced in Langfuse.

---

### 2.1 Start the MCP Service

```bash
semiont provision --service mcp   # OAuth setup — first time only
semiont start --service mcp
semiont check --service mcp
```

### 2.2 Wire Claude Desktop

Add to Claude Desktop's MCP config at `~/Library/Application Support/Claude/claude_desktop_config.json` (macOS):

```json
{
  "mcpServers": {
    "epstein-wiki": {
      "command": "semiont",
      "args": ["start", "--service", "mcp"],
      "env": {
        "SEMIONT_BUS": "http://localhost:3001",
        "SEMIONT_ENV": "local"
      }
    }
  }
}
```

Restart Claude Desktop. The `epstein-wiki` knowledge base will appear as a tool set. Run a smoke test: *"What documents do you have about Jeffrey Epstein's properties?"*

### 2.3 Implement Query Routing

Before every OpenSearch call, classify the query into one of three routes. This is the **Routing** pattern applied to retrieval path dispatch.

| Route | Trigger | OpenSearch Call |
|---|---|---|
| `EXACT` | Specific name, quoted string, date, flight number | BM25 `match` query only |
| `SEMANTIC` | Conceptual or relational question | k-NN `knn` query only |
| `HYBRID` (default) | Ambiguous or benefits from both | BM25 + k-NN combined scorer |

**Implementation — lightweight classifier:**

```python
import re

EXACT_PATTERNS = [
    r'"[^"]+"',                    # quoted string
    r'\b[A-Z][a-z]+ [A-Z][a-z]+\b',  # proper noun (Firstname Lastname)
    r'\b\d{4}-\d{2}-\d{2}\b',     # ISO date
    r'\bflight [A-Z0-9]+\b',       # flight number
]

def classify_route(query: str) -> str:
    for pattern in EXACT_PATTERNS:
        if re.search(pattern, query):
            return "EXACT"
    # Fall back to Haiku for ambiguous cases
    # (single classify call, not a full Q&A call)
    response = haiku_classify(query)
    return response  # "EXACT" | "SEMANTIC" | "HYBRID"

def search(query: str, top_k: int = 5) -> list:
    route = classify_route(query)
    if route == "EXACT":
        return opensearch_bm25(query, top_k)
    elif route == "SEMANTIC":
        return opensearch_knn(query, top_k)
    else:
        return opensearch_hybrid(query, top_k)
```

Wire this classifier into the MCP `query-kb` tool handler so every journalist query is dispatched to the right retrieval path before the augmented LLM call.

### 2.4 Define MCP Tools with Full ACI Documentation

Register four tools on the MCP server. Write each description as you would a docstring for a junior engineer — the model must understand when and how to call each tool from the description alone.

**`query-kb`**
> Search the Epstein wiki knowledge base. Route is auto-selected: pass `exact_match=true` for BM25 precision (specific names, dates); leave `false` for semantic search (conceptual questions). Returns cited chunks with source document and relevance score.

Parameters: `query` (string), `top_k` (int, default 5), `exact_match` (bool, default false), `doc_type` (keyword filter, optional), `date_range` (ISO range, optional)

**`fetch-resource`**
> Retrieve the full text of a specific source document by resource ID. Use when `query-kb` returns a promising document and you need the full surrounding context, not just the matching chunk.

Parameters: `resource_id` (string)

**`filter-by-entity`**
> Return all documents that contain annotations for a specific named entity. Use for person-centric research: "All documents mentioning [name]." More precise than `query-kb` for exact person lookups because it queries the entity graph rather than text similarity.

Parameters: `entity_name` (string), `entity_type` (`Person | Org | Location | Event`, optional)

**`filter-by-date`**
> Filter knowledge base chunks by document date range. Useful for timeline analysis and finding what was known at a specific point in time.

Parameters: `start_date` (ISO), `end_date` (ISO), `query` (string, optional — combines semantic search with date filter)

### 2.5 Configure Langfuse Tracing

Sign up at [langfuse.com](https://langfuse.com) — free cloud tier, no infra needed.

1. Create a new project: `epstein-wiki-poc`
2. Copy `LANGFUSE_PUBLIC_KEY` and `LANGFUSE_SECRET_KEY` from project settings
3. Add to `.env`:

```bash
LANGFUSE_PUBLIC_KEY=pk-lf-...
LANGFUSE_SECRET_KEY=sk-lf-...
```

4. Add tracing config to `~/.semiontconfig` under the local environment:

```toml
[environments.local.workers.default.inference.tracing]
type      = "langfuse"
publicKey = "${LANGFUSE_PUBLIC_KEY}"
secretKey = "${LANGFUSE_SECRET_KEY}"
host      = "https://cloud.langfuse.com"
```

5. Restart services to pick up tracing:

```bash
semiont stop --service backend
semiont start --service backend
```

Every `gather → inference` call now appears in the Langfuse dashboard. Confirm traces are flowing after asking one question via Claude Desktop.

**Fields captured on every trace:**

| Field | Value |
|---|---|
| `prompt_text` | Full prompt sent to Haiku |
| `completion_text` | Full model response |
| `model` | `claude-haiku-4-5-20251001` |
| `input_tokens` / `output_tokens` | Token counts |
| `latency_ms` | End-to-end inference time |
| `route_used` | `BM25 \| k-NN \| hybrid` |
| `session_id` | Journalist query session |

> **Fallback:** If Langfuse SDK integration is blocked by Semiont 0.5.7, wrap inference calls in a thin Python script with manual `langfuse.Langfuse()` SDK instrumentation around the CLI calls.

### 2.6 Run the Test Query Set

Run these six queries via Claude Desktop and verify each one:

| Query | What to Verify |
|---|---|
| "Who are the named individuals in the flight logs?" | Person entities resolved; sources cited; k-NN retrieving flight log chunks |
| "Which properties are mentioned across the documents?" | Property/Location entities linked across files; semantic search finding property chunks |
| "What events are described in the court filings from 2019?" | `doc_type=court-filing` filter narrowing results; event entities extracted |
| "Summarise the connections between [Person A] and [Person B]" | Cross-document entity linking via bind flow; BM25 + k-NN both contributing |
| "Find all documents that mention [exact name]" | BM25 exact-match working — validates keyword precision, not just semantic similarity |
| "What is the significance of Little Saint James island?" | k-NN semantic search retrieving contextually related chunks without exact keyword match |

---

### ✅ Day 2 Checkpoint

Before ending Day 2, confirm all of the following:

- [ ] All 6 test queries answered via Claude Desktop with citations (filename + document type)
- [ ] Query 5 ("Find all documents that mention [exact name]") confirms BM25 exact-name search is working
- [ ] Query 6 ("Little Saint James island") confirms k-NN semantic search is working
- [ ] All inference calls are visible in Langfuse with prompt, tokens, latency, and route logged
- [ ] Query router is classifying correctly — verify `route_used` field in Langfuse traces

---

## Day 3 — Evaluation, Prompt Regression, Demo Prep

**Goal:** Ragas eval scores > 0.7 recorded as baseline. All Promptfoo tests passing. System reproducible from clean clone in < 30 minutes.

---

### 3.1 Install Eval Dependencies

```bash
pip install ragas langchain-anthropic
npm install -g promptfoo
```

### 3.2 Create the Ground Truth Dataset

Write `eval/dataset.json` — 10–15 Q&A pairs is sufficient for a POC baseline. Include questions that span multiple document types and require both BM25 and semantic retrieval.

```json
[
  {
    "question": "Who are the named individuals in the Epstein flight logs?",
    "ground_truth": "The flight logs name [Person A], [Person B], [Person C] ...",
    "contexts": ["flight-log-2002.pdf", "flight-log-2005.pdf"]
  },
  {
    "question": "What properties did Epstein own according to the documents?",
    "ground_truth": "Documents reference the Manhattan townhouse, Little Saint James island ...",
    "contexts": ["court-filing-2019.pdf", "nyt-article-2019.md"]
  }
  // ... 8-13 more pairs covering Person, Location, Event, and date-range queries
]
```

### 3.3 Run Ragas Evaluation (Evaluator-Optimizer Loop, Pass 1)

```bash
python eval/run_ragas.py
# Target: faithfulness > 0.7, answer_relevancy > 0.7, context_recall > 0.7
# Results saved to eval/ragas_results.csv
```

**`eval/run_ragas.py` overview:**

1. Loads `eval/dataset.json`
2. For each question: calls `semiont browse resources --search <question>` to retrieve top-3 documents, then `semiont gather resource <id> --summary` to get context
3. Assembles `{ question, answer, contexts, ground_truth }` rows into a Ragas `Dataset`
4. Runs `evaluate()` with `faithfulness`, `answer_relevancy`, `context_recall`, `context_precision`
5. Prints scores and writes to `eval/ragas_results.csv`

**If any metric < 0.7 — Evaluator-Optimizer iteration:**

| Failing metric | Likely cause | Prompt to revise |
|---|---|---|
| `faithfulness` | Answer contains claims not in retrieved chunks | Q&A system prompt — tighten grounding instruction |
| `answer_relevancy` | Answer drifts from the question | Q&A system prompt — clarify response scope |
| `context_recall` | Retrieved chunks missing relevant content | Gatherer prompt — broaden entity extraction; check routing for affected query types |

Revise the relevant prompt, re-run `run_ragas.py`, check scores. Budget 2–3 iterations in Day 3 afternoon. Each iteration takes ~5–10 minutes.

### 3.4 Run Promptfoo Annotation Regression Tests

Write `promptfoo.yaml`:

```yaml
description: Epstein Wiki — annotation detection prompt regression

providers:
  - id: anthropic:messages:claude-haiku-4-5-20251001
    config:
      apiKey: ${ANTHROPIC_API_KEY}

prompts:
  - file://prompts/entity-extraction.txt

tests:
  - description: Extracts person names from court filing excerpt
    vars:
      text: "Testimony from Jane Doe, age 22, corroborated by John Smith Esq."
    assert:
      - type: contains
        value: "Jane Doe"
      - type: contains
        value: "John Smith"
      - type: is-json

  - description: Extracts location from document
    vars:
      text: "The property at East 71st Street, Manhattan was visited on three occasions."
    assert:
      - type: contains
        value: "East 71st Street"
      - type: llm-rubric
        value: "The response identifies a location entity"

  - description: Does not hallucinate entities from ambiguous text
    vars:
      text: "The meeting was attended by several unnamed associates."
    assert:
      - type: llm-rubric
        value: "The response does not invent specific names or entities"

  - description: Returns valid JSON array of at least 2 entities
    vars:
      text: "Jeffrey Epstein and Ghislaine Maxwell met at the Palm Beach estate."
    assert:
      - type: is-json
      - type: javascript
        value: "Array.isArray(output) && output.length >= 2"
```

Run:

```bash
promptfoo eval
promptfoo view   # opens browser report
```

All 4 tests must pass. If any fail, fix the entity-extraction prompt at `prompts/entity-extraction.txt` and re-run. Failing prompt tests are a hard stop — no prompt ships without passing regression.

### 3.5 Persistence Check

Confirm the OpenSearch index survived a full restart:

```bash
semiont stop
semiont start
sleep 30  # wait for OpenSearch to come back up
curl -s http://localhost:9200/epstein-wiki/_count | python3 -m json.tool
# Expect: same count as before restart
```

### 3.6 Reproducibility Check

Run the full setup from a fresh terminal with only environment variables set:

```bash
# Step 1: Clone and enter repo
git clone <repo>
cd epstein-wiki

# Step 2: Download corpus from justice.gov (datasets 1-3)
pip install requests beautifulsoup4
python scripts/batch_download_epstein_files.py
mv epstein_files/* raw/

# Step 3: Start services (includes OpenSearch)
semiont provision
semiont start
sleep 30
curl -s http://localhost:9200/_cluster/health   # confirm healthy

# Step 4: Create OpenSearch index
bash scripts/create_opensearch_index.sh

# Step 5: Ingest + annotate + dual-index
pip install pypdf ollama requests
python scripts/ingest.py
# Wait for Smelter to complete embedding (~5 min for a 50-doc corpus)

# Step 6: MCP server for Claude Desktop
semiont start --service mcp
# Open Claude Desktop — KB should be available
```

Target: a colleague with no prior knowledge completes this in under 30 minutes following the README alone.

### 3.7 Demo Walkthrough (5 minutes)

| Step | Action | Shows |
|---|---|---|
| 1 | Open Semiont UI at `localhost:3000` | Resource list with annotation counts; entity highlights on document text |
| 2 | Run BM25 `curl` query for an exact person name in terminal | OpenSearch returning persisted results instantly — embeddings survived restart |
| 3 | Open Claude Desktop; ask: "Who are the people named in the flight logs?" | MCP call → k-NN + BM25 retrieval → gather → inference → cited answer |
| 4 | Ask: "Find all documents that mention [exact name]" | BM25 precision — exact-name match working alongside semantic search |
| 5 | Open Langfuse; show the traces just created | Prompt, tokens, latency, model, route — full observability on every inference call |
| 6 | Show `eval/ragas_results.csv` | Quantitative retrieval quality baseline — POC is measured, not just working |
| 7 | Show Promptfoo report in browser | Prompt regression tests passing — prompts are tested before any code ships |

---

### ✅ Day 3 Checkpoint

Before calling the POC complete, confirm all of the following:

- [ ] `semiont start` brings full stack up including OpenSearch in one command from clean repo clone
- [ ] `curl http://localhost:9200/epstein-wiki/_count` returns non-zero after Docker restart
- [ ] BM25 exact-name search for a known person returns correct document hits
- [ ] k-NN semantic search for a flight-related question returns relevant flight log chunks
- [ ] Claude Desktop answers all 6 test queries with cited sources
- [ ] Langfuse shows traces for every inference call with prompt, tokens, latency
- [ ] Ragas `faithfulness` and `answer_relevancy` both > 0.7 in `eval/ragas_results.csv`
- [ ] All 4 Promptfoo annotation prompt regression tests pass
- [ ] Colleague can reproduce the POC from the README in under 30 minutes

---

## Upgrade Path

The POC uses in-memory stores and single-node services. Each swaps independently via one config change — no application code changes.

| POC Component | Full Build Upgrade | Config Change |
|---|---|---|
| OpenSearch single-node (local) | OpenSearch multi-node on AWS | Change `host`/`port` to AWS OpenSearch Service endpoint |
| Memory graph (in-proc) | Neo4j | `make-meaning.graph.type = "neo4j"` + URI/credentials |
| Ollama nomic-embed-text (768-dim) | Voyage AI voyage-3 (1024-dim) | `embedding.type = "voyage"` + `apiKey`; update index mapping `dimension` to 1024 |
| Docker Compose (local) | Kubernetes on EKS + Terraform | `platform = "aws"` in `semiont.toml` |
| Langfuse cloud free tier | Langfuse self-hosted | Change `host` in tracing config section |
| Manual prompt optimization | LangGraph automated optimizer | Add agent layer on top of `semiont match + bind` CLI calls |
| Claude Haiku 4.5 | Claude Sonnet for Q&A | Change `model` string in `workers.default.inference` |

---

## Risk Register

| Risk | Likelihood | Mitigation |
|---|---|---|
| OpenSearch OOMKill (k-NN plugin is memory-heavy) | High | `ES_JAVA_OPTS=-Xms512m -Xmx512m` in docker-compose |
| Full 12-dataset download too large for 3-day window | High | Run datasets 1–3 only first (~30–80 files) |
| Ollama `embed()` slow for large chunk counts | Medium | Batch calls — Ollama accepts list input; run overnight if needed |
| Ragas scores < 0.7 on first pass | Medium | Evaluator-Optimizer loop: revise prompt, re-run; budget 2–3 iterations Day 3 afternoon |
| Langfuse SDK incompatible with Semiont 0.5.7 | Low–Medium | Wrap inference calls in thin Python script with manual Langfuse SDK |
| justice.gov blocks scraper | Low–Medium | Script uses 300ms delay + standard User-Agent; fallback: manual download to `raw/` |
| MCP OAuth setup blocks Claude Desktop | Low | `semiont provision --service mcp` handles this; fallback: Semiont REST API directly |
| Scanned PDFs have no extractable text | Medium | Use text-layer PDFs where possible; document scanned files as out of scope for POC |

---

*Epstein File Wiki · Implementation Plan · v1.0 · June 2026 · Internal*
