```
 ______ _____   _____ _______ ______ _____ _   _ 
|  ____|  __ \ / ____|__   __|  ____|_   _| \ | |
| |__  | |__) | (___    | |  | |__    | | |  \| |
|  __| |  ___/ \___ \   | |  |  __|   | | | . ` |
| |____| |     ____) |  | |  | |____ _| |_| |\  |
|______|_|    |_____/   |_|  |______|_____|_| \_|

 ______ _____ _      ______  _____ 
|  ____|_   _| |    |  ____|/ ____|
| |__    | | | |    | |__  | (___  
|  __|   | | | |    |  __|  \___ \ 
| |     _| |_| |____| |____ ____) |
|_|    |_____|______|______|_____/ 
```

# Epstein File Wiki — POC

**Investigative Journalism Research Knowledge Base**
Stack: OpenSearch · Ollama · Langfuse · Ragas · Promptfoo

> **Semiont suspended** — pagination bug causes ingest to hang on large corpora. Not used until patched. All search/eval runs directly against OpenSearch.

---

## From-Scratch Setup (new machine)

Complete start-to-finish sequence. Run once on a fresh machine. After this, use **Restart Services** below for daily use.

### Step 1 — System dependencies

```bash
# macOS (Homebrew)
brew install python@3.12 node tesseract
brew install --cask docker          # Docker Desktop

# Verify
docker --version
node --version        # need 20+
python3 --version     # need 3.10+
```

### Step 2 — Install Ollama (local, not Docker)

Ollama runs natively on the host for GPU access. Do NOT use the Docker Ollama service — comment it out.

```bash
# macOS
brew install ollama

# Start the Ollama daemon (runs on :11434)
ollama serve &

# Pull required models (one-time, ~5 GB total)
ollama pull nomic-embed-text    # 274 MB — embeddings
ollama pull llama3:8b           # 4.7 GB — generator + judge LLM

# Verify
curl http://localhost:11434/api/tags | python3 -c \
  "import sys,json; [print(m['name']) for m in json.load(sys.stdin)['models']]"
# Expected: nomic-embed-text:latest, llama3:8b
```

> Ollama auto-starts on macOS after `brew services start ollama`. To keep it manual: `ollama serve`.

### Step 3 — Start Docker stack

```bash
cd epstein-wiki
docker compose up -d

# Wait ~60s for healthchecks. Verify:
docker ps --format "table {{.Names}}\t{{.Status}}\t{{.Ports}}"
```

Expected containers and ports:

| Container | Port | Status |
|---|---|---|
| `epstein-opensearch` | 9200, 9600 | healthy |
| `epstein-dashboards` | 5601 | running |
| `epstein-langfuse-db` | (internal) | healthy |
| `epstein-langfuse` | 3000 | healthy |
| `epstein-semiont-db` | (internal) | healthy (unused) |
| `epstein-semiont-backend` | 4000 | healthy (unused) |
| `epstein-semiont-frontend` | 3001 | healthy (unused) |

> `epstein-langfuse` may show `unhealthy` — the health endpoint `/api/public/health` sometimes lags. Check `http://localhost:3000` loads instead.

### Step 4 — Create Langfuse account

Langfuse has no default user. Create one on first run:

```bash
open http://localhost:3000
```

1. Click **Sign up**
2. Email: `your@email.com`, Password: choose a strong password
3. Create organization: `wiki`
4. Create project: `epstein-wiki`
5. Go to **Settings → API Keys → Create new secret key**
6. Copy the public key (`pk-lf-...`) and secret key (`sk-lf-...`)

Save to `.env` (never commit this file):

```bash
cat >> .env <<'EOF'
LANGFUSE_EMAIL=your@email.com
LANGFUSE_PASSWORD=yourpassword
LANGFUSE_PUBLIC_KEY=pk-lf-...
LANGFUSE_SECRET_KEY=sk-lf-...
LANGFUSE_HOST=http://localhost:3000
OPENSEARCH_URL=http://localhost:9200
OLLAMA_URL=http://localhost:11434
EOF
```

### Step 5 — Python virtualenv + deps

```bash
cd epstein-wiki
python3 -m venv .venv
source .venv/bin/activate

pip install requests beautifulsoup4 pymupdf pytesseract Pillow
pip install langfuse langchain-ollama langchain-community datasets ragas==0.4.3
```

### Step 6 — Start Search UI

```bash
source .env
cd search-ui
python3 server.py
# Open http://localhost:8765
```

### Step 7 — Verify everything

```bash
# OpenSearch alive
curl http://localhost:9200/_cluster/health | python3 -m json.tool

# Ollama alive
curl http://localhost:11434/api/tags | python3 -c \
  "import sys,json; [print(m['name']) for m in json.load(sys.stdin)['models']]"

# Langfuse alive
curl http://localhost:3000/api/public/health

# Search UI
open http://localhost:8765

# Index has data
curl http://localhost:9200/epstein-wiki/_count
```

---

## START HERE — Daily Startup

Run these 3 commands every session (after first-time setup):

```bash
# 1. Ollama (embeddings + LLM judge)
ollama serve &

# 2. Docker stack (OpenSearch + Langfuse + Semiont containers)
cd /Users/augustus/Codebase/epstein-wiki
docker compose up -d

# 3. Search UI (browser interface on :8765)
source .env && python3 search-ui/server.py
```

Verify everything is up:
```bash
curl -s http://localhost:9200/epstein-wiki/_count   # must show count ~764602
curl -s http://localhost:11434/api/tags | python3 -c "import sys,json; print([m['name'] for m in json.load(sys.stdin)['models']])"
curl -s http://localhost:3000/api/public/health
docker ps --format "table {{.Names}}\t{{.Status}}"
open http://localhost:8765   # Search UI
open http://localhost:3000   # Langfuse traces
```

## Safe Shutdown

```bash
# 1. Kill Search UI
lsof -ti :8765 | xargs kill -9

# 2. Stop Docker stack (volumes preserved)
cd /Users/augustus/Codebase/epstein-wiki
docker compose stop

# 3. Stop Ollama
pkill -f "ollama serve"
```

---

## ⚠️ DANGER — Commands That Wipe OpenSearch Data

**Reingest takes 24+ hours. These commands permanently destroy the index.**

```bash
# DESTROYS ALL DATA — never run these:
docker compose down -v                          # wipes ALL named volumes
docker volume rm epstein-wiki_opensearch-data  # wipes OpenSearch index only
docker volume prune                            # wipes all unused volumes (may include opensearch-data if container is stopped)
```

**Safe alternatives:**

```bash
# Stop/restart without data loss:
docker compose stop
docker compose start
docker compose restart opensearch

# Remove containers but keep volumes:
docker compose down        # NO -v flag
```

**Verify data is intact after any restart:**
```bash
curl http://localhost:9200/epstein-wiki/_count
# Must return: {"count":764602,...}
# If count is 0 or index missing → data was wiped, reingest required
```

---

## Prerequisites

```bash
# Required
docker desktop running
python3 --version     # 3.10+
brew install tesseract     # macOS (for scanned PDF OCR)
# sudo apt install tesseract-ocr   # Ubuntu
```

---

## Day 1 — Environment, Corpus, Ingest

### 1. Start the stack

```bash
docker compose up -d
# Wait ~60s for OpenSearch healthcheck
docker compose ps       # opensearch + langfuse containers should show healthy
```

Services in use:
- OpenSearch: http://localhost:9200
- OpenSearch Dashboards: http://localhost:5601
- Langfuse: http://localhost:3000
- Ollama: http://localhost:11434 (native, not Docker)

### 2. Download corpus (POC: datasets 1–3)

```bash
python scripts/batch_download_epstein_files.py --datasets 1 3
# ~30–80 PDFs → raw/dataset_{1,2,3}/
# Scanned PDFs flagged → logs/scanned_files.txt
```

> **Full corpus:** `--datasets 1 12` — only do this after validating pipeline on 1–3.

### 4. OCR pre-processing (if scanned files detected)

```bash
# Check if any were flagged:
cat logs/scanned_files.txt

# If non-empty:
python scripts/ocr_preprocess.py
# Writes .ocr.txt sidecars alongside originals
# ingest.py picks these up automatically
```

> ⚠️ **Known Gap:** Semiont has no native OCR. See [Known Gaps](#known-gaps) below.

### 5. Ingest corpus

```bash
# Smoke test — first 10 files:
python scripts/ingest.py --limit 10

# Full ingest:
python scripts/ingest.py
```

### 6. Validate

```bash
# Chunks indexed:
curl http://localhost:9200/epstein-wiki/_count

# BM25 search:
curl 'http://localhost:9200/epstein-wiki/_search?q=Ghislaine+Maxwell&size=3' | python3 -m json.tool
```

**Day 1 exit condition:** `_count > 0`, BM25 returns hits.

---

## Search UI (OpenSearch Direct — no Semiont required)

Standalone browser UI that queries OpenSearch and Ollama directly. No Semiont containers needed.

**Stack:** `search-ui/index.html` + `search-ui/server.py` (stdlib Python proxy, no deps)

### Start

```bash
cd search-ui
python3 server.py
# → http://localhost:8765/
```

Proxy routes:
- `GET /` → serves `index.html`
- `POST /api/*` → `http://localhost:9200/*` (OpenSearch)
- `POST /embed` → `http://localhost:11434/api/embeddings` (Ollama)

Override defaults:
```bash
PORT=9000 OPENSEARCH_URL=http://remote:9200 OLLAMA_URL=http://remote:11434 python3 server.py
```

### Search modes

| Mode | Algorithm | Score color | When to use |
|------|-----------|-------------|-------------|
| **Full-text** | BM25 multi_match, fuzziness AUTO | yellow | Names, case numbers, orgs, partial spellings |
| **Phrase** | match_phrase (exact adjacency) | orange | Exact legal phrases, word-order matters |
| **Semantic** | k-NN cosine on 768-dim vectors via nomic-embed-text | purple | Conceptual questions, paraphrases, unknown terminology |
| **Hybrid** | bool/should: BM25 + k-NN combined | green | Best overall — maximum recall |

Click `?` in the UI for per-mode docs, example queries, and OpenSearch DSL reference links.

### Prerequisites

OpenSearch index `epstein-wiki` must be populated (run ingest pipeline first).
Ollama must have `nomic-embed-text` pulled — verified at startup with:
```bash
curl http://localhost:11434/api/tags | python3 -c "import sys,json; print([m['name'] for m in json.load(sys.stdin)['models']])"
```

---

## Day 2 — Q&A, Routing, Tracing

*Coming next — Claude Desktop MCP integration + Langfuse traces*

---

## Ragas Evaluation

End-to-end RAG eval: OpenSearch retrieval → llama3:8b generation → Ragas scoring → CSV output + Langfuse traces.

**Prerequisites**

```bash
cd epstein-wiki
.venv/bin/pip install ragas langchain-ollama langchain-community datasets -q
```

Ollama must have `llama3:8b` and `nomic-embed-text` pulled:
```bash
curl http://localhost:11434/api/tags | python3 -c "import sys,json; print([m['name'] for m in json.load(sys.stdin)['models']])"
```

**Smoke test — 2 questions (~8 min)**

```bash
source .env && \
LANGFUSE_PUBLIC_KEY=$LANGFUSE_PUBLIC_KEY \
LANGFUSE_SECRET_KEY=$LANGFUSE_SECRET_KEY \
LANGFUSE_HOST=$LANGFUSE_HOST \
  .venv/bin/python3 eval/run_ragas.py --limit 2
```

Expected output:
```
  ✓ faithfulness              0.750  (target ≥ 0.7)
  ✓ answer_relevancy          0.880  (target ≥ 0.7)
  ✓ context_recall            0.750  (target ≥ 0.6)
  ✓ context_precision         0.794  (target ≥ 0.6)
Saved: eval/results/ragas_YYYYMMDD_HHMMSS.csv
```

**Full run — 15 questions (~45 min)**

```bash
source .env && \
LANGFUSE_PUBLIC_KEY=$LANGFUSE_PUBLIC_KEY \
LANGFUSE_SECRET_KEY=$LANGFUSE_SECRET_KEY \
LANGFUSE_HOST=$LANGFUSE_HOST \
  .venv/bin/python3 eval/run_ragas.py
```

Override search mode for all questions:
```bash
... .venv/bin/python3 eval/run_ragas.py --mode semantic
```

**Output**

- CSV saved to `eval/results/ragas_YYYYMMDD_HHMMSS.csv` (gitignored)
- Each question traced to Langfuse at `http://localhost:3000` (project: epstein-wiki)
- Traces include retrieval span + generation span per question

**Thresholds**

| Metric | Target | Meaning |
|---|---|---|
| `faithfulness` | ≥ 0.7 | Answer claims grounded in retrieved context |
| `answer_relevancy` | ≥ 0.7 | Answer addresses the question |
| `context_recall` | ≥ 0.6 | Retrieved chunks cover the ground truth |
| `context_precision` | ≥ 0.6 | Retrieved chunks are on-topic |

If any metric fails → check `eval/results/` CSV per-question breakdown, tune retrieval mode or system prompt.

**Dataset**

`eval/dataset.json` — 15 ground-truth Q&A pairs covering: person queries (Maxwell, Acosta), event queries (arrests, plea deal), property queries (Little Saint James), and network queries (flight logs, victim testimony).

---

## Day 3 — Eval, Prompts, Demo-Ready

*Coming next — Promptfoo prompt regression tests*

---

## Promptfoo Prompt Regression Tests

Deterministic prompt regression tests against llama3:8b (no OpenAI required).

**Three suites:**
- `suite-qa.yaml` — 5 tests: grounding, refusal, no hallucination, date extraction, no speculation
- `suite-entity.yaml` — 5 tests: person extraction, org/location, count, no invention, date entities
- `suite-routing.yaml` — 7 tests: document ID, person name, case number → fulltext; exact phrase → phrase; conceptual → semantic; investigation → hybrid

**Run all suites (~2 min):**
```bash
cd epstein-wiki/promptfoo
bash run.sh
```

**Run single suite:**
```bash
bash run.sh --suite qa        # Q&A grounding
bash run.sh --suite entity    # Entity extraction
bash run.sh --suite routing   # Query routing
```

**Expected baseline:** 17/17 tests pass with llama3:8b.

**Add a regression test:** edit the relevant `suite-*.yaml`, add a `tests:` entry, run the suite. CI should catch regressions when prompts change.

---

## Known Gaps

### Semiont — Pagination Bug (blocked)

Semiont v0.5.7 hangs during ingest on large corpora due to missing pagination in the document listing API. All ingestion and search runs directly against OpenSearch until this is patched upstream. Semiont containers remain in `docker-compose.yml` but are not used.

### OCR / Image Recognition

**Semiont v0.5.7 has no native OCR.** Scanned PDFs (image-only, no text layer) upload as opaque blobs — they produce zero chunks in OpenSearch and are invisible to both BM25 and k-NN search.

**Workaround (POC):** `scripts/ocr_preprocess.py` uses Tesseract to generate `.ocr.txt` sidecars. `ingest.py` auto-detects and routes to sidecars. `batch_download_epstein_files.py` flags scanned files to `logs/scanned_files.txt`.

**Full-build path:** Replace with AWS Textract, Google Document AI, or a self-hosted Surya/Marker pipeline as an Ingest Agent pre-processing stage. Semiont would need a `yield --ocr` flag or pre-processor plugin hook.

**Scope:** Out of scope for 3-day POC. Sufficient text-layer PDFs exist in the corpus to validate the pipeline end-to-end.

Full spec: `semiont-missing-features.md` — *Gap Note: OCR / Image Recognition*

---

## Upgrade Path (one config change each)

| POC | Full Build | Change |
|-----|-----------|--------|
| OpenSearch local | AWS OpenSearch Service | `search.host` in semiont.toml |
| Ollama nomic-embed-text (768) | Voyage AI voyage-3 (1024) | `embedding.type = "voyage"` + update index dimension |
| In-process memory graph | Neo4j | `make-meaning.graph.type = "neo4j"` |
| Docker Compose | Kubernetes/EKS + Terraform | `platform.type = "aws"` |
| Claude Haiku 4.5 | Claude Sonnet 4.6 | `workers.default.inference.model` |
| Langfuse cloud | Langfuse self-hosted | `tracing.host` |
| Manual prompt optimization | LangGraph automated optimizer | Add agent layer |

---

## Reproducibility

From a clean clone:

```bash
docker compose up -d
pip install requests beautifulsoup4 pymupdf pytesseract Pillow
python scripts/batch_download_epstein_files.py --datasets 1 3
python scripts/ocr_preprocess.py   # if scanned files found
python scripts/ingest.py
# < 30 min total
```

---

*OpenSearch (Apache-2.0) · Ollama · Ragas (Apache-2.0) · Langfuse (MIT) · Promptfoo (MIT)*
*Semiont v0.5.7 (Apache-2.0) — suspended pending pagination patch*