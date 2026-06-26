#!/usr/bin/env python3
"""
mcp_tools.py
MCP server exposing 4 research tools for the Epstein wiki knowledge base.

Run:
    python scripts/mcp_tools.py

Connect Claude Desktop:
    Settings → MCP Servers → Add → http://localhost:8080/sse

Tools:
    query-kb          Search KB, auto-selects BM25 / k-NN / hybrid route
    fetch-resource    Get full document text by resource ID
    filter-by-entity  All chunks mentioning a named entity
    filter-by-date    Chunks within a document date range
"""

import json
import os
import sys
import time
from datetime import datetime
from pathlib import Path

import requests

# Allow `from query_router import ...` when run as scripts/mcp_tools.py
sys.path.insert(0, str(Path(__file__).parent))
from query_router import classify_route, search

try:
    from mcp.server.fastmcp import FastMCP
except ImportError:
    print("Missing mcp package. Run: pip install mcp --break-system-packages")
    sys.exit(1)

# ── Langfuse (optional) ───────────────────────────────────────────────────────

try:
    from langfuse import Langfuse
    _lf = Langfuse(
        public_key=os.getenv("LANGFUSE_PUBLIC_KEY", ""),
        secret_key=os.getenv("LANGFUSE_SECRET_KEY", ""),
        host=os.getenv("LANGFUSE_HOST", "http://localhost:3000"),
    )
    _TRACING = bool(os.getenv("LANGFUSE_PUBLIC_KEY"))
except Exception:
    _lf = None
    _TRACING = False


def _trace(name: str, inputs: dict, outputs: dict, metadata: dict | None = None):
    if not _TRACING or _lf is None:
        return
    try:
        _lf.trace(name=name, input=inputs, output=outputs, metadata=metadata or {})
        _lf.flush()
    except Exception:
        pass


# ── Config ────────────────────────────────────────────────────────────────────

OPENSEARCH_URL   = os.getenv("OPENSEARCH_URL", "http://localhost:9200")
OPENSEARCH_INDEX = "epstein-wiki"

mcp = FastMCP("epstein-wiki", port=8080)


# ── Tools ─────────────────────────────────────────────────────────────────────

@mcp.tool(name="query-kb")
def query_kb(
    query: str,
    top_k: int = 5,
    exact_match: bool = False,
    doc_type: str | None = None,
    date_range: str | None = None,
) -> str:
    """
    Search the Epstein wiki knowledge base.

    Route is auto-selected based on query type. Pass exact_match=true for BM25
    precision search — use this for specific names (e.g. "Ghislaine Maxwell"),
    ISO dates (e.g. "2019-07-06"), flight numbers, or quoted strings.
    Leave exact_match=false (default) for semantic or hybrid search on conceptual
    questions ("Who funded travel arrangements?", "connections to financial institutions").

    Optionally filter results by doc_type (e.g. "court-filing", "flight-log") or
    date_range in "YYYY-MM-DD/YYYY-MM-DD" format.

    Returns ranked chunks with source document, chunk index, resource ID, and relevance score.
    Use fetch_resource with a resource_id to retrieve the full surrounding document.
    """
    t0 = time.monotonic()
    route = "EXACT" if exact_match else classify_route(query)
    results = search(query, top_k=top_k, route=route)

    if doc_type:
        results = [r for r in results if r.get("doc_type") == doc_type]

    if date_range:
        try:
            start, end = date_range.split("/")
            results = [r for r in results if start <= r.get("ingested_at", "") <= end]
        except ValueError:
            pass

    latency_ms = int((time.monotonic() - t0) * 1000)

    _trace(
        "query-kb",
        {"query": query, "top_k": top_k, "exact_match": exact_match},
        {"count": len(results), "route": route},
        {"latency_ms": latency_ms, "route_used": route},
    )

    return json.dumps(
        {
            "route_used": route,
            "count": len(results),
            "results": [
                {
                    "chunk_text": r.get("text", ""),
                    "source_doc": r.get("source_file", ""),
                    "dataset": r.get("dataset", ""),
                    "chunk_index": r.get("chunk_index", 0),
                    "resource_id": r.get("resource_id", ""),
                    "ingested_at": r.get("ingested_at", ""),
                    "relevance_score": round(r.get("relevance_score") or 0, 4),
                    "route_used": route,
                }
                for r in results
            ],
        },
        indent=2,
    )


@mcp.tool(name="fetch-resource")
def fetch_resource(resource_id: str) -> str:
    """
    Retrieve the full text of a source document by resource ID.

    Use this after query_kb returns a promising chunk and you need the surrounding
    context. resource_id is included in every query_kb result. Chunks are returned
    in reading order (chunk_index ascending).

    Returns full document text, metadata, and chunk count.
    """
    r = requests.post(
        f"{OPENSEARCH_URL}/{OPENSEARCH_INDEX}/_search",
        json={
            "size": 1000,
            "query": {"term": {"resource_id": resource_id}},
            "sort": [{"chunk_index": "asc"}],
        },
        timeout=30,
    )
    r.raise_for_status()
    hits = r.json()["hits"]["hits"]

    if not hits:
        return json.dumps({"error": f"No document found: resource_id={resource_id}"})

    full_text = "\n\n".join(h["_source"].get("text", "") for h in hits)
    meta = hits[0]["_source"]

    _trace("fetch-resource", {"resource_id": resource_id}, {"chunk_count": len(hits)})

    return json.dumps(
        {
            "resource_id": resource_id,
            "source_file": meta.get("source_file", ""),
            "dataset": meta.get("dataset", ""),
            "chunk_count": len(hits),
            "ingested_at": meta.get("ingested_at", ""),
            "full_text": full_text,
        },
        indent=2,
    )


@mcp.tool(name="filter-by-entity")
def filter_by_entity(entity_name: str, entity_type: str | None = None) -> str:
    """
    Return all documents containing a specific named entity.

    Use for person-centric or location-centric research:
    - "All documents mentioning Ghislaine Maxwell"
    - "All documents referencing Little Saint James"
    - "All references to Deutsche Bank"

    Optionally narrow by entity_type: Person, Org, Location, or Event.
    Results are grouped by source document with annotation count and context spans.

    Returns list of documents with entity span excerpts (±60 chars around each mention).
    """
    os_query = {
        "size": 100,
        "query": {"match": {"text": {"query": entity_name, "analyzer": "english"}}},
        "_source": ["resource_id", "source_file", "dataset", "text", "chunk_index"],
    }

    r = requests.post(
        f"{OPENSEARCH_URL}/{OPENSEARCH_INDEX}/_search",
        json=os_query,
        timeout=30,
    )
    r.raise_for_status()
    hits = r.json()["hits"]["hits"]

    by_resource: dict = {}
    for h in hits:
        src = h["_source"]
        rid = src.get("resource_id", "")
        if rid not in by_resource:
            by_resource[rid] = {
                "resource_id": rid,
                "source_doc": src.get("source_file", ""),
                "dataset": src.get("dataset", ""),
                "annotation_count": 0,
                "entity_spans": [],
            }
        by_resource[rid]["annotation_count"] += 1
        text = src.get("text", "")
        idx = text.lower().find(entity_name.lower())
        if idx >= 0:
            span = text[max(0, idx - 60) : idx + len(entity_name) + 60].strip()
            by_resource[rid]["entity_spans"].append(span)

    results = list(by_resource.values())
    _trace(
        "filter-by-entity",
        {"entity_name": entity_name, "entity_type": entity_type},
        {"doc_count": len(results)},
    )

    return json.dumps(
        {
            "entity_name": entity_name,
            "entity_type": entity_type,
            "document_count": len(results),
            "documents": results,
        },
        indent=2,
    )


@mcp.tool(name="filter-by-date")
def filter_by_date(start_date: str, end_date: str, query: str | None = None) -> str:
    """
    Filter knowledge base chunks by date range for timeline analysis.

    Dates must be ISO format: YYYY-MM-DD.
    Optionally combine with a text query to find topic-specific content within
    the date window — e.g. query="flight logs" with 2019-01-01 / 2019-12-31.

    Note: date filter applies to ingested_at (index date), which approximates
    document date for this corpus. Results sorted oldest first.

    Returns up to 20 matching chunks with date metadata.
    """
    must: list = [{"range": {"ingested_at": {"gte": start_date, "lte": end_date}}}]
    if query:
        must.append({"match": {"text": {"query": query, "analyzer": "english"}}})

    r = requests.post(
        f"{OPENSEARCH_URL}/{OPENSEARCH_INDEX}/_search",
        json={
            "size": 20,
            "query": {"bool": {"must": must}},
            "sort": [{"ingested_at": "asc"}],
        },
        timeout=30,
    )
    r.raise_for_status()
    hits = r.json()["hits"]["hits"]

    _trace(
        "filter-by-date",
        {"start_date": start_date, "end_date": end_date, "query": query},
        {"count": len(hits)},
    )

    return json.dumps(
        {
            "date_range": f"{start_date}/{end_date}",
            "query": query,
            "count": len(hits),
            "results": [
                {
                    "source_doc": h["_source"].get("source_file", ""),
                    "dataset": h["_source"].get("dataset", ""),
                    "ingested_at": h["_source"].get("ingested_at", ""),
                    "resource_id": h["_source"].get("resource_id", ""),
                    "chunk_text": h["_source"].get("text", "")[:500],
                }
                for h in hits
            ],
        },
        indent=2,
    )


# ── Entry point ───────────────────────────────────────────────────────────────

if __name__ == "__main__":
    import argparse as _ap
    _p = _ap.ArgumentParser()
    _p.add_argument("--transport", default="stdio", choices=["stdio", "sse"])
    _a = _p.parse_args()
    if _a.transport == "sse":
        import sys as _sys
        print("Epstein Wiki MCP server starting on http://localhost:8080", file=_sys.stderr)
        print(f"Tracing: {'enabled (Langfuse)' if _TRACING else 'disabled'}", file=_sys.stderr)
    mcp.run(transport=_a.transport)
