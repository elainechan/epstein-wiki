#!/usr/bin/env python3
"""
Ragas evaluation for epstein-wiki RAG pipeline.

Retriever : OpenSearch (hybrid BM25 + kNN via nomic-embed-text)
Generator : Ollama llama3:8b
Judge LLM : Ollama llama3:8b
Embeddings: Ollama nomic-embed-text
Tracing   : Langfuse (optional, via env vars)

Usage:
    python3 eval/run_ragas.py [--limit N] [--mode fulltext|phrase|semantic|hybrid]
"""

import argparse, json, os, sys, time, urllib.request
from pathlib import Path

# ── Config ────────────────────────────────────────────────────────────────────
OPENSEARCH_URL   = os.environ.get("OPENSEARCH_URL", "http://localhost:9200")
OPENSEARCH_INDEX = "epstein-wiki"
OLLAMA_URL       = os.environ.get("OLLAMA_URL", "http://localhost:11434")
GENERATOR_MODEL  = "llama3:8b"
EMBED_MODEL      = "nomic-embed-text"
TOP_K            = 5
EVAL_DIR         = Path(__file__).parent
RESULTS_DIR      = EVAL_DIR / "results"
RESULTS_DIR.mkdir(exist_ok=True)

# ── Langfuse (optional) ───────────────────────────────────────────────────────
try:
    from langfuse import Langfuse
    _lf = Langfuse(
        public_key=os.environ["LANGFUSE_PUBLIC_KEY"],
        secret_key=os.environ["LANGFUSE_SECRET_KEY"],
        host=os.environ.get("LANGFUSE_HOST", "http://localhost:3000"),
    )
    _TRACING = True
except Exception:
    _lf = None
    _TRACING = False


def _os_post(path: str, body: dict) -> dict:
    data = json.dumps(body).encode()
    req  = urllib.request.Request(
        f"{OPENSEARCH_URL}/{path}",
        data=data, headers={"Content-Type": "application/json"}, method="POST",
    )
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.load(r)


def _ollama_embed(text: str) -> list[float]:
    data = json.dumps({"model": EMBED_MODEL, "prompt": text}).encode()
    req  = urllib.request.Request(
        f"{OLLAMA_URL}/api/embeddings",
        data=data, headers={"Content-Type": "application/json"}, method="POST",
    )
    with urllib.request.urlopen(req, timeout=60) as r:
        return json.load(r)["embedding"]


def _ollama_generate(prompt: str) -> str:
    data = json.dumps({
        "model": GENERATOR_MODEL,
        "messages": [{"role": "user", "content": prompt}],
        "stream": False,
        "options": {"temperature": 0.1},
    }).encode()
    req = urllib.request.Request(
        f"{OLLAMA_URL}/api/chat",
        data=data, headers={"Content-Type": "application/json"}, method="POST",
    )
    with urllib.request.urlopen(req, timeout=120) as r:
        return json.load(r)["message"]["content"].strip()


# ── Retriever ─────────────────────────────────────────────────────────────────
def retrieve(question: str, mode: str = "hybrid", k: int = TOP_K) -> list[str]:
    if mode in ("semantic", "hybrid"):
        vector = _ollama_embed(question)

    if mode == "fulltext":
        query = {"multi_match": {"query": question, "fields": ["text"], "fuzziness": "AUTO"}}
    elif mode == "phrase":
        query = {"match_phrase": {"text": question}}
    elif mode == "semantic":
        query = {"knn": {"embedding": {"vector": vector, "k": k}}}
    else:  # hybrid
        query = {"bool": {"should": [
            {"multi_match": {"query": question, "fields": ["text"], "type": "best_fields"}},
            {"knn": {"embedding": {"vector": vector, "k": k}}},
        ], "minimum_should_match": 1}}

    resp  = _os_post(f"{OPENSEARCH_INDEX}/_search", {
        "size": k,
        "query": query,
        "_source": ["text", "resource_id", "chunk_index"],
        "collapse": {"field": "resource_id"},
    })
    return [h["_source"]["text"] for h in resp["hits"]["hits"] if "_source" in h]


# ── Generator ─────────────────────────────────────────────────────────────────
SYSTEM_PROMPT = """You are a research assistant analyzing declassified legal documents about Jeffrey Epstein.
Answer the question using ONLY the provided context. Be specific and cite relevant details.
If the context does not contain enough information to answer, say so clearly.
Do not add information not present in the context."""

def generate(question: str, contexts: list[str]) -> str:
    context_block = "\n\n---\n\n".join(contexts)
    prompt = f"{SYSTEM_PROMPT}\n\nContext:\n{context_block}\n\nQuestion: {question}\n\nAnswer:"
    return _ollama_generate(prompt)


# ── Eval pipeline ─────────────────────────────────────────────────────────────
def run_eval(items: list[dict]) -> list[dict]:
    rows = []
    for i, item in enumerate(items, 1):
        q    = item["question"]
        gt   = item["ground_truth"]
        mode = item.get("mode", "hybrid")
        print(f"[{i}/{len(items)}] {mode:8s} | {q[:60]}…", flush=True)

        t0       = time.monotonic()
        contexts = retrieve(q, mode=mode)
        t_ret    = time.monotonic() - t0

        t0      = time.monotonic()
        answer  = generate(q, contexts)
        t_gen   = time.monotonic() - t0

        print(f"           retrieval {t_ret:.1f}s | generation {t_gen:.1f}s | contexts: {len(contexts)}")

        if _TRACING and _lf:
            trace = _lf.trace(name="ragas-eval", input={"question": q, "mode": mode})
            trace.span(name="retrieve", input={"mode": mode, "k": TOP_K},
                       output={"contexts": len(contexts)},
                       metadata={"latency_ms": round(t_ret * 1000)})
            trace.generation(name="generate", model=GENERATOR_MODEL,
                             input=[{"role": "user", "content": q}],
                             output=answer,
                             metadata={"latency_ms": round(t_gen * 1000)})
            _lf.flush()

        rows.append({
            "question":     q,
            "answer":       answer,
            "contexts":     contexts,
            "ground_truth": gt,
        })
    return rows


# ── Main ──────────────────────────────────────────────────────────────────────
def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--limit", type=int, help="Limit number of eval items")
    parser.add_argument("--mode",  help="Override search mode for all items")
    args = parser.parse_args()

    dataset_path = EVAL_DIR / "dataset.json"
    items = json.loads(dataset_path.read_text())
    if args.limit:
        items = items[:args.limit]
    if args.mode:
        for it in items:
            it["mode"] = args.mode

    print(f"Ragas eval — {len(items)} questions")
    print(f"Retriever : OpenSearch {OPENSEARCH_URL}/{OPENSEARCH_INDEX}")
    print(f"Generator : Ollama {GENERATOR_MODEL}")
    print(f"Judge LLM : Ollama {GENERATOR_MODEL}")
    print(f"Tracing   : {'Langfuse enabled' if _TRACING else 'disabled'}")
    print()

    rows = run_eval(items)

    print("\nRunning Ragas metrics…")
    try:
        from datasets import Dataset
        from ragas import evaluate
        from ragas.metrics import faithfulness, answer_relevancy, context_recall, context_precision
        from langchain_ollama import ChatOllama, OllamaEmbeddings
        from ragas.llms import LangchainLLMWrapper
        from ragas.embeddings import LangchainEmbeddingsWrapper

        llm = LangchainLLMWrapper(ChatOllama(
            model=GENERATOR_MODEL,
            base_url=OLLAMA_URL,
            temperature=0.1,
            timeout=120,
        ))
        embeddings = LangchainEmbeddingsWrapper(OllamaEmbeddings(
            model=EMBED_MODEL,
            base_url=OLLAMA_URL,
        ))

        from ragas.run_config import RunConfig
        ds     = Dataset.from_list(rows)
        result = evaluate(
            ds,
            metrics=[faithfulness, answer_relevancy, context_recall, context_precision],
            llm=llm,
            embeddings=embeddings,
            # ollama can't handle concurrent requests — run sequentially
            run_config=RunConfig(timeout=180, max_retries=2, max_workers=1),
        )

        print("\n── Ragas scores ─────────────────────────────")
        scores = result.scores if hasattr(result, "scores") else {}
        df     = result.to_pandas()

        thresholds = {
            "faithfulness":        0.7,
            "answer_relevancy":    0.7,
            "context_recall":      0.6,
            "context_precision":   0.6,
        }

        for metric, threshold in thresholds.items():
            if metric in df.columns:
                val = df[metric].mean()
                flag = "✓" if val >= threshold else "✗"
                print(f"  {flag} {metric:25s} {val:.3f}  (target ≥ {threshold})")

        ts  = time.strftime("%Y%m%d_%H%M%S")
        csv = RESULTS_DIR / f"ragas_{ts}.csv"
        df.to_csv(csv, index=False)
        print(f"\nSaved: {csv}")

        # Log aggregate scores to Langfuse
        if _TRACING and _lf:
            for metric in thresholds:
                if metric in df.columns:
                    _lf.score(
                        name=metric,
                        value=float(df[metric].mean()),
                        comment=f"ragas eval {ts}",
                    )
            _lf.flush()

    except ImportError as e:
        print(f"Ragas scoring skipped: {e}")
        print("Run: pip install ragas langchain-ollama datasets")

        # Still save raw answers
        import csv as csv_mod
        ts  = time.strftime("%Y%m%d_%H%M%S")
        csv = RESULTS_DIR / f"answers_{ts}.csv"
        with open(csv, "w", newline="") as f:
            w = csv_mod.DictWriter(f, fieldnames=["question", "answer", "ground_truth", "n_contexts"])
            w.writeheader()
            for r in rows:
                w.writerow({**r, "contexts": None, "n_contexts": len(r["contexts"])})
        print(f"Raw answers saved: {csv}")


if __name__ == "__main__":
    main()
