#!/usr/bin/env python3
"""
query_router.py
Classifies journalist queries and executes the appropriate OpenSearch route.

Routes:
    EXACT   → BM25 match query (specific names, dates, flight numbers)
    SEMANTIC → k-NN only (conceptual / relational questions)
    HYBRID  → BM25 + k-NN combined (default)

Usage:
    from query_router import search, classify_route
    results = search("Ghislaine Maxwell", top_k=5)
"""

import os
import re
import json
import requests

OPENSEARCH_URL   = os.getenv("OPENSEARCH_URL", "http://localhost:9200")
OPENSEARCH_INDEX = "epstein-wiki"
OLLAMA_URL       = os.getenv("OLLAMA_URL", "http://localhost:11434")
EMBED_MODEL      = "nomic-embed-text"

# Regex patterns that force EXACT route without an LLM call
_EXACT_RE = re.compile(
    r'"[^"]+"'                       # quoted string
    r'|\b[A-Z][a-z]+ [A-Z][a-z]+\b' # Firstname Lastname
    r'|\b\d{4}-\d{2}-\d{2}\b'       # ISO date
    r'|\b(19|20)\d{2}\b'            # bare year
    r'|\b[A-Z]{2,3}\d{3,}\b'        # flight number (AA123, LX4711)
)


def classify_route(query: str) -> str:
    """Return EXACT, SEMANTIC, or HYBRID for the given query string."""
    if _EXACT_RE.search(query):
        return "EXACT"
    return _haiku_classify(query)


def _haiku_classify(query: str) -> str:
    try:
        import anthropic
        client = anthropic.Anthropic()
        msg = client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=10,
            messages=[{"role": "user", "content": (
                "Classify this search query as EXACT, SEMANTIC, or HYBRID.\n"
                "EXACT: specific names, dates, places, flight numbers, quoted strings.\n"
                "SEMANTIC: conceptual, relational, or thematic questions.\n"
                "HYBRID: combines specific entities with conceptual context.\n\n"
                f"Query: {query}\n\n"
                "Respond with one word only: EXACT, SEMANTIC, or HYBRID"
            )}]
        )
        result = msg.content[0].text.strip().upper()
        return result if result in ("EXACT", "SEMANTIC", "HYBRID") else "HYBRID"
    except Exception:
        return "HYBRID"


def _embed(text: str) -> list[float]:
    r = requests.post(
        f"{OLLAMA_URL}/api/embed",
        json={"model": EMBED_MODEL, "input": [text]},
        timeout=30
    )
    r.raise_for_status()
    return r.json()["embeddings"][0]


def _bm25_query(query: str, top_k: int) -> dict:
    return {
        "size": top_k,
        "query": {"match": {"text": {"query": query, "analyzer": "english"}}}
    }


def _knn_query(query: str, top_k: int) -> dict:
    vec = _embed(query)
    return {
        "size": top_k,
        "query": {"knn": {"embedding": {"vector": vec, "k": top_k}}}
    }


def _hybrid_query(query: str, top_k: int) -> dict:
    vec = _embed(query)
    return {
        "size": top_k,
        "query": {
            "bool": {
                "should": [
                    {"match": {"text": {"query": query, "analyzer": "english", "boost": 1.0}}},
                    {"knn": {"embedding": {"vector": vec, "k": top_k, "boost": 2.0}}}
                ]
            }
        }
    }


def search(query: str, top_k: int = 5, route: str = None) -> list[dict]:
    """
    Classify and execute OpenSearch query.
    Returns list of hit dicts with route_used injected.
    """
    if route is None:
        route = classify_route(query)

    if route == "EXACT":
        os_query = _bm25_query(query, top_k)
    elif route == "SEMANTIC":
        os_query = _knn_query(query, top_k)
    else:
        os_query = _hybrid_query(query, top_k)

    r = requests.post(
        f"{OPENSEARCH_URL}/{OPENSEARCH_INDEX}/_search",
        json=os_query,
        timeout=30
    )
    r.raise_for_status()
    hits = r.json()["hits"]["hits"]

    return [
        {"route_used": route, "relevance_score": h["_score"], **h["_source"]}
        for h in hits
    ]


if __name__ == "__main__":
    import sys
    q = " ".join(sys.argv[1:]) or "Ghislaine Maxwell"
    print(f"Query: {q}")
    route = classify_route(q)
    print(f"Route:  {route}")
    results = search(q, top_k=3, route=route)
    for i, r in enumerate(results, 1):
        print(f"\n[{i}] {r.get('source_file')} (score={r.get('relevance_score', 0):.3f})")
        print(r.get("text", "")[:300])
