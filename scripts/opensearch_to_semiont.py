#!/usr/bin/env python3
"""Bulk upload OpenSearch documents to Semiont for annotation.

Reads all unique resource_ids from OpenSearch, reconstructs full text
from ordered chunks, and uploads each as text/plain to Semiont.

Skips resources already present in Semiont (by checking storageUri).

Usage:
  python3 opensearch_to_semiont.py [--limit N] [--dataset DATASET] [--dry-run]
"""

import argparse
import json
import sys
import time
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from io import BytesIO
from pathlib import Path

try:
    import requests
except ImportError:
    print("pip install requests")
    sys.exit(1)

OPENSEARCH_URL = "http://localhost:9200"
OPENSEARCH_INDEX = "epstein-wiki"
SEMIONT_URL = "http://localhost:4000"
AUTH_CACHE = Path.home() / ".local/state/semiont/auth/localhost-4000.json"
BATCH_SIZE = 500  # chunks per scroll page


def get_token() -> str:
    if not AUTH_CACHE.exists():
        print("Not logged in to Semiont.")
        sys.exit(1)
    data = json.loads(AUTH_CACHE.read_text())
    return data["token"]


def get_semiont_existing(session: requests.Session, token: str) -> set[str]:
    """Return set of storageUri values already in Semiont."""
    existing = set()
    page = 0
    while True:
        resp = session.get(
            f"{SEMIONT_URL}/resources",
            headers={"Authorization": f"Bearer {token}"},
            params={"limit": 200, "offset": page * 200},
            timeout=30,
        )
        if resp.status_code != 200:
            break
        data = resp.json()
        items = data if isinstance(data, list) else data.get("resources", [])
        if not items:
            break
        for r in items:
            uri = r.get("storageUri") or r.get("storage_uri") or ""
            if uri:
                existing.add(uri)
        if len(items) < 200:
            break
        page += 1
    return existing


def list_datasets(dataset_filter: str | None = None) -> list[dict]:
    """Return list of {dataset, resource_id, count} sorted by dataset, resource_id."""
    agg_query = {
        "size": 0,
        "aggs": {
            "by_dataset": {
                "terms": {"field": "dataset", "size": 100},
                "aggs": {
                    "by_resource": {
                        "terms": {"field": "resource_id", "size": 50000}
                    }
                }
            }
        }
    }
    if dataset_filter:
        agg_query["query"] = {"term": {"dataset": dataset_filter}}

    resp = requests.post(
        f"{OPENSEARCH_URL}/{OPENSEARCH_INDEX}/_search",
        json=agg_query,
        timeout=60,
    )
    resp.raise_for_status()
    data = resp.json()

    resources = []
    for ds_bucket in data["aggregations"]["by_dataset"]["buckets"]:
        dataset = ds_bucket["key"]
        for res_bucket in ds_bucket["by_resource"]["buckets"]:
            resources.append({
                "dataset": dataset,
                "resource_id": res_bucket["key"],
                "chunk_count": res_bucket["doc_count"],
            })
    return sorted(resources, key=lambda x: (x["dataset"], x["resource_id"]))


def fetch_full_text(resource_id: str) -> tuple[str, str]:
    """Fetch all chunks for a resource_id, ordered by chunk_index. Returns (text, dataset)."""
    query = {
        "query": {"term": {"resource_id": resource_id}},
        "sort": [{"chunk_index": "asc"}],
        "_source": ["text", "dataset", "chunk_index"],
        "size": 10000,
    }
    resp = requests.post(
        f"{OPENSEARCH_URL}/{OPENSEARCH_INDEX}/_search",
        json=query,
        timeout=30,
    )
    resp.raise_for_status()
    hits = resp.json()["hits"]["hits"]
    if not hits:
        return "", ""
    dataset = hits[0]["_source"].get("dataset", "")
    text = "\n\n".join(h["_source"]["text"] for h in hits if h["_source"].get("text"))
    return text, dataset


def upload_to_semiont(
    session: requests.Session,
    token: str,
    resource_id: str,
    dataset: str,
    text: str,
) -> str | None:
    name = f"{resource_id}.txt"
    storage_uri = f"file://epstein-files/{dataset}/{resource_id}"
    try:
        resp = session.post(
            f"{SEMIONT_URL}/resources",
            headers={"Authorization": f"Bearer {token}"},
            files={"file": (name, BytesIO(text.encode("utf-8")), "text/plain")},
            data={
                "name": name,
                "format": "text/plain",
                "storageUri": storage_uri,
            },
            timeout=60,
        )
        if resp.status_code in (200, 202):
            return resp.json().get("resourceId")
        print(f"  [http {resp.status_code}] {resp.text[:120]}")
        return None
    except Exception as e:
        print(f"  [err] {e}")
        return None


def process_resource(
    r: dict,
    token: str,
    existing_uris: set[str],
    counter: dict,
    lock: threading.Lock,
    upload_sem: threading.Semaphore,
    total: int,
) -> None:
    rid = r["resource_id"]
    dataset = r["dataset"]
    storage_uri = f"file://epstein-files/{dataset}/{rid}"

    if storage_uri in existing_uris:
        with lock:
            counter["skip"] += 1
            counter["done"] += 1
        return

    # Parallel OpenSearch fetch (no semaphore needed)
    text, _ = fetch_full_text(rid)
    if not text.strip():
        with lock:
            counter["skip"] += 1
            counter["done"] += 1
        return

    # Serialized Semiont upload (semaphore limits concurrency to avoid race conditions)
    session = requests.Session()
    with upload_sem:
        semiont_id = upload_to_semiont(session, token, rid, dataset, text)

    with lock:
        counter["done"] += 1
        elapsed = time.time() - counter["start"]
        rate = counter["ok"] / elapsed if elapsed > 0 and counter["ok"] > 0 else 0
        if semiont_id:
            counter["ok"] += 1
            eta_s = (total - counter["done"]) / rate if rate > 0 else 0
            print(f"[{counter['done']}/{total}] {dataset}/{rid} → {semiont_id} "
                  f"({len(text):,}c) [{rate:.1f}/s ETA {eta_s/3600:.1f}h]", flush=True)
        else:
            counter["fail"] += 1
            print(f"[{counter['done']}/{total}] {dataset}/{rid} FAILED", flush=True)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--limit", type=int, help="Max resources to upload")
    parser.add_argument("--dataset", help="Filter to specific dataset")
    parser.add_argument("--dry-run", action="store_true", help="List only, no upload")
    parser.add_argument("--skip-existing-check", action="store_true",
                        help="Skip querying Semiont for already-uploaded resources")
    parser.add_argument("--workers", type=int, default=8,
                        help="Parallel fetch threads (default: 8)")
    parser.add_argument("--upload-concurrency", type=int, default=2,
                        help="Max concurrent Semiont uploads (default: 2)")
    args = parser.parse_args()

    token = get_token()
    session = requests.Session()

    print("Fetching resource list from OpenSearch...")
    resources = list_datasets(args.dataset)
    print(f"Found {len(resources)} resources across "
          f"{len(set(r['dataset'] for r in resources))} datasets")

    if args.limit:
        resources = resources[:args.limit]
        print(f"Capped at {args.limit}")

    if args.dry_run:
        for r in resources:
            print(f"  {r['dataset']}/{r['resource_id']} ({r['chunk_count']} chunks)")
        return

    existing_uris: set[str] = set()
    if not args.skip_existing_check:
        print("Checking Semiont for already-uploaded resources...")
        existing_uris = get_semiont_existing(session, token)
        print(f"  {len(existing_uris)} already in Semiont")

    counter = {"ok": 0, "skip": 0, "fail": 0, "done": 0, "start": time.time()}
    lock = threading.Lock()
    upload_sem = threading.Semaphore(args.upload_concurrency)
    total = len(resources)

    print(f"Starting upload: {args.workers} fetch workers, {args.upload_concurrency} concurrent uploads...")
    with ThreadPoolExecutor(max_workers=args.workers) as executor:
        futures = [
            executor.submit(process_resource, r, token, existing_uris, counter, lock, upload_sem, total)
            for r in resources
        ]
        try:
            for f in as_completed(futures):
                f.result()  # re-raise exceptions
        except KeyboardInterrupt:
            print("\nInterrupted.")

    elapsed = time.time() - counter["start"]
    print(f"\nDone in {elapsed/60:.1f}min: {counter['ok']} uploaded, "
          f"{counter['skip']} skipped, {counter['fail']} failed")


if __name__ == "__main__":
    main()
