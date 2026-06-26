#!/usr/bin/env python3
"""
ingest.py
Uploads DOJ Epstein disclosure corpus to Semiont and dual-indexes
all chunks + embeddings into OpenSearch.

Pipeline per document:
    1. Detect scanned PDFs → route to .ocr.txt sidecar if available
    2. semiont yield --upload  → registers resource, runs annotation workers
    3. Extract text chunks with pymupdf
    4. Embed each chunk via Ollama nomic-embed-text (768-dim)
    5. Bulk-index chunk + embedding to OpenSearch epstein-wiki index

Usage:
    python scripts/ingest.py                        # all raw/**/*.pdf
    python scripts/ingest.py --dir raw/dataset_1   # specific dataset
    python scripts/ingest.py --limit 10            # first 10 files (smoke test)

Day 1 exit condition (from implementation plan):
    curl http://localhost:9200/epstein-wiki/_count   → {"count": N, ...}  N > 0
    BM25 search for "Ghislaine Maxwell" returns hits
    k-NN search for flight-related query returns hits
    Entity annotations visible in Semiont UI
"""

import argparse
import base64
import json
import subprocess
import sys
import time
from pathlib import Path

try:
    import fitz       # pymupdf
    import requests
except ImportError:
    print("Missing dependencies.")
    print("Run: pip install pymupdf requests --break-system-packages")
    sys.exit(1)

# ── Config ────────────────────────────────────────────────────────────────────
OPENSEARCH_URL  = "http://localhost:9200"
OPENSEARCH_INDEX = "epstein-wiki"
OLLAMA_URL       = "http://localhost:11434"
EMBED_MODEL      = "nomic-embed-text"
VISION_MODEL     = "qwen2.5vl:3b"
CHUNK_SIZE       = 500   # characters per chunk
CHUNK_OVERLAP    = 50
BATCH_SIZE       = 20    # chunks per Ollama embed() call
INGEST_DELAY_S   = 0.1   # between files
MIN_IMAGE_BYTES  = 5000  # skip logos/decorative images

RAW_DIR = Path("/Volumes/Bones/epstein-files")
LOG_DIR = Path("logs")

# Scanned PDF heuristic threshold (chars on page 0)
SCANNED_THRESHOLD = 100


# ── OpenSearch index setup ────────────────────────────────────────────────────

INDEX_MAPPING = {
    "settings": {
        "index": {
            "knn": True,
            "knn.algo_param.ef_search": 100
        }
    },
    "mappings": {
        "properties": {
            "resource_id":   {"type": "keyword"},
            "chunk_index":   {"type": "integer"},
            "source_file":   {"type": "keyword"},
            "dataset":       {"type": "keyword"},
            "text":          {"type": "text", "analyzer": "english"},
            "embedding": {
                "type":      "knn_vector",
                "dimension": 768,  # nomic-embed-text; update to 1024 for voyage-3
                "method": {
                    "name":       "hnsw",
                    "space_type": "cosinesimil",
                    "engine":     "nmslib"
                }
            },
            "ingested_at":  {"type": "date"}
        }
    }
}


def ensure_index(session: requests.Session):
    """Create OpenSearch index if it doesn't exist."""
    r = session.head(f"{OPENSEARCH_URL}/{OPENSEARCH_INDEX}")
    if r.status_code == 404:
        r = session.put(
            f"{OPENSEARCH_URL}/{OPENSEARCH_INDEX}",
            json=INDEX_MAPPING
        )
        r.raise_for_status()
        print(f"[index] Created {OPENSEARCH_INDEX}")
    else:
        print(f"[index] {OPENSEARCH_INDEX} already exists")


# ── PDF / text helpers ────────────────────────────────────────────────────────

def is_valid_pdf(pdf_path: Path) -> bool:
    """Check magic bytes — rejects HTML age-gate files saved as .pdf."""
    try:
        with open(pdf_path, "rb") as f:
            return f.read(5) == b"%PDF-"
    except Exception:
        return False


def is_scanned(pdf_path: Path) -> bool:
    """Heuristic: scanned if page 0 has < SCANNED_THRESHOLD extractable chars."""
    try:
        doc = fitz.open(str(pdf_path))
        text = doc[0].get_text() if len(doc) > 0 else ""
        return len(text.strip()) < SCANNED_THRESHOLD
    except Exception:
        return False


def resolve_source(pdf_path: Path) -> tuple[Path, bool]:
    """
    Return the file to actually read text from, and whether OCR was used.

    If a .ocr.txt sidecar exists (created by ocr_preprocess.py), use it.
    Otherwise, if the PDF is scanned and no sidecar exists, log and skip.

    Semiont OCR gap: see semiont-missing-features.md
    """
    sidecar = pdf_path.with_suffix(".ocr.txt")
    if sidecar.exists():
        return sidecar, True

    if is_scanned(pdf_path):
        return None, True  # scanned, no sidecar — must skip

    return pdf_path, False


def extract_text(source: Path) -> str:
    """Extract full text from a PDF or .ocr.txt file."""
    if source.suffix == ".txt":
        return source.read_text(encoding="utf-8", errors="replace")

    doc = fitz.open(str(source))
    return "\n\n".join(page.get_text() for page in doc)


def chunk_text(text: str, size: int = CHUNK_SIZE, overlap: int = CHUNK_OVERLAP) -> list[str]:
    """Simple sliding-window character chunker."""
    chunks = []
    start = 0
    while start < len(text):
        end = start + size
        chunks.append(text[start:end].strip())
        start += size - overlap
    return [c for c in chunks if len(c) > 30]  # drop tiny tail chunks


# ── Embedding ─────────────────────────────────────────────────────────────────

def embed_batch(texts: list[str]) -> list[list[float]]:
    """Embed a batch of texts via Ollama. Returns list of 768-dim vectors."""
    r = requests.post(
        f"{OLLAMA_URL}/api/embed",
        json={"model": EMBED_MODEL, "input": texts},
        timeout=120
    )
    r.raise_for_status()
    data = r.json()
    return data.get("embeddings", [])


# ── Vision captioning ─────────────────────────────────────────────────────────

def caption_image(image_bytes: bytes) -> str:
    b64 = base64.b64encode(image_bytes).decode()
    r = requests.post(
        f"{OLLAMA_URL}/api/generate",
        json={
            "model": VISION_MODEL,
            "prompt": "Describe this image. If it contains text, transcribe it exactly. Note any people, locations, dates, or identifying details.",
            "images": [b64],
            "stream": False
        },
        timeout=120
    )
    r.raise_for_status()
    return r.json().get("response", "").strip()


def extract_image_captions(pdf_path: Path) -> list[str]:
    captions = []
    doc = fitz.open(str(pdf_path))
    for page_num, page in enumerate(doc):
        for img_tuple in page.get_images(full=True):
            xref = img_tuple[0]
            try:
                base_image = doc.extract_image(xref)
                image_bytes = base_image["image"]
                if len(image_bytes) < MIN_IMAGE_BYTES:
                    continue
                caption = caption_image(image_bytes)
                if caption:
                    captions.append(f"[Image, page {page_num + 1}]: {caption}")
            except Exception as e:
                print(f"  [img warn] page {page_num + 1}: {e}")
    return captions


# ── Semiont upload ────────────────────────────────────────────────────────────

def semiont_upload(file_path: Path) -> str | None:
    """
    Upload a file to Semiont via CLI.
    Returns the resource_id on success, None on failure.
    """
    result = subprocess.run(
        ["semiont", "yield", "--upload", str(file_path), "-o", "json"],
        capture_output=True, text=True, timeout=60
    )
    if result.returncode != 0:
        print(f"  [semiont err] {result.stderr.strip()}")
        return None

    try:
        data = json.loads(result.stdout)
        return data.get("resourceId") or data.get("resource_id")
    except json.JSONDecodeError:
        # Some Semiont versions print resource_id on a line by itself
        for line in result.stdout.splitlines():
            if line.startswith("resource_id:") or line.startswith("resourceId:"):
                return line.split(":", 1)[1].strip()
        return None


# ── OpenSearch bulk index ─────────────────────────────────────────────────────

def index_chunks(
    session: requests.Session,
    resource_id: str,
    source_file: str,
    dataset: str,
    chunks: list[str],
    embeddings: list[list[float]],
    ingested_at: str
):
    """Bulk-index chunks + embeddings to OpenSearch."""
    bulk_body = []
    for i, (chunk, emb) in enumerate(zip(chunks, embeddings)):
        doc_id = f"{resource_id}_{i}"
        bulk_body.append(json.dumps({"index": {"_index": OPENSEARCH_INDEX, "_id": doc_id}}))
        bulk_body.append(json.dumps({
            "resource_id":  resource_id,
            "chunk_index":  i,
            "source_file":  source_file,
            "dataset":      dataset,
            "text":         chunk,
            "embedding":    emb,
            "ingested_at":  ingested_at
        }))

    body = "\n".join(bulk_body) + "\n"
    r = session.post(
        f"{OPENSEARCH_URL}/_bulk",
        data=body,
        headers={"Content-Type": "application/x-ndjson"},
        timeout=60
    )
    r.raise_for_status()
    resp = r.json()
    if resp.get("errors"):
        error_items = [item for item in resp["items"] if item.get("index", {}).get("error")]
        print(f"  [os warn] {len(error_items)} bulk errors")


# ── Vision-only pass ──────────────────────────────────────────────────────────

def _run_vision_only(args):
    """
    Add image caption chunks to already-indexed documents without re-ingesting text.
    Queries OpenSearch for indexed source_file names, finds matching PDFs on disk,
    runs vision captioning, and appends chunks with doc IDs prefixed 'img_'.
    """
    from datetime import datetime, timezone

    session = requests.Session()
    ensure_index(session)

    # Fetch all unique source_file + resource_id pairs from the index
    r = session.post(
        f"{OPENSEARCH_URL}/{OPENSEARCH_INDEX}/_search",
        json={
            "size": 0,
            "aggs": {
                "by_file": {
                    "terms": {"field": "source_file", "size": 100000},
                    "aggs": {
                        "resource_id": {"terms": {"field": "resource_id", "size": 1}}
                    }
                }
            }
        }
    )
    r.raise_for_status()
    buckets = r.json()["aggregations"]["by_file"]["buckets"]
    print(f"[vision-only] {len(buckets)} indexed files found")

    # Build lookup: source_file → resource_id
    indexed: dict[str, str] = {}
    for b in buckets:
        rid = b["resource_id"]["buckets"][0]["key"] if b["resource_id"]["buckets"] else b["key"]
        indexed[b["key"]] = rid

    # Find PDFs on disk matching indexed files
    pdfs = [p for p in sorted(args.dir.rglob("*.pdf")) if not p.name.startswith("._") and is_valid_pdf(p)]
    if args.limit:
        pdfs = pdfs[:args.limit]

    total_files = 0
    total_img_chunks = 0

    for pdf_path in pdfs:
        if pdf_path.name not in indexed:
            continue  # not yet ingested — skip

        resource_id = indexed[pdf_path.name]
        dataset = pdf_path.parent.name

        # Skip if vision chunks already exist for this doc
        check = session.post(
            f"{OPENSEARCH_URL}/{OPENSEARCH_INDEX}/_search",
            json={"size": 1, "query": {"bool": {"must": [
                {"term": {"resource_id": resource_id}},
                {"prefix": {"_id": "img_"}}
            ]}}}
        )
        if check.ok and check.json()["hits"]["total"]["value"] > 0:
            print(f"[skip] {pdf_path.name} — vision chunks already indexed")
            continue

        print(f"[vision] {pdf_path.name}")
        img_captions = extract_image_captions(pdf_path)
        if not img_captions:
            print(f"  no images found")
            continue

        print(f"  {len(img_captions)} images → embedding")
        all_embeddings = []
        for i in range(0, len(img_captions), BATCH_SIZE):
            batch = img_captions[i:i + BATCH_SIZE]
            try:
                all_embeddings.extend(embed_batch(batch))
            except Exception as e:
                print(f"  [embed err] {e}")
                all_embeddings.extend([[0.0] * 768] * len(batch))

        ingested_at = datetime.now(timezone.utc).isoformat()
        bulk_body = []
        for i, (chunk, emb) in enumerate(zip(img_captions, all_embeddings)):
            doc_id = f"img_{resource_id}_{i}"
            bulk_body.append(json.dumps({"index": {"_index": OPENSEARCH_INDEX, "_id": doc_id}}))
            bulk_body.append(json.dumps({
                "resource_id":  resource_id,
                "chunk_index":  i,
                "source_file":  pdf_path.name,
                "dataset":      dataset,
                "text":         chunk,
                "embedding":    emb,
                "ingested_at":  ingested_at,
                "chunk_type":   "image_caption"
            }))

        body = "\n".join(bulk_body) + "\n"
        resp = session.post(
            f"{OPENSEARCH_URL}/_bulk",
            data=body,
            headers={"Content-Type": "application/x-ndjson"},
            timeout=60
        )
        resp.raise_for_status()
        print(f"  [os] {len(img_captions)} image chunks indexed")
        total_img_chunks += len(img_captions)
        total_files += 1
        time.sleep(INGEST_DELAY_S)

    print(f"\n✓ Vision pass complete: {total_files} files, {total_img_chunks} image chunks added")


# ── Checkpoint ────────────────────────────────────────────────────────────────

CHECKPOINT_FILE = LOG_DIR / "ingest_checkpoint.json"
BOILERPLATE = ["skip to main content", "official website of the united states",
               "are you 18 years of age", "here's how you know"]


def load_checkpoint() -> set[str]:
    if CHECKPOINT_FILE.exists():
        data = json.loads(CHECKPOINT_FILE.read_text())
        completed = set(data.get("completed", []))
        print(f"[checkpoint] Resuming — {len(completed)} files already ingested")
        return completed
    return set()


def save_checkpoint(completed: set[str]):
    from datetime import datetime
    CHECKPOINT_FILE.write_text(json.dumps(
        {"completed": list(completed), "updated_at": datetime.now().isoformat()},
        indent=2
    ))


INVALID_LOG = LOG_DIR / "invalid_files.log"


def validate_chunks(chunks: list[str], pdf_path: Path) -> tuple[bool, str]:
    """Check extracted chunks for boilerplate or suspiciously short content."""
    if not chunks:
        return False, "no chunks extracted"
    avg_len = sum(len(c) for c in chunks) / len(chunks)
    if avg_len < 40:
        return False, f"avg chunk length {avg_len:.0f} chars — likely empty/corrupt"
    boilerplate_hits = sum(
        1 for c in chunks[:5] if any(b in c.lower() for b in BOILERPLATE)
    )
    if boilerplate_hits >= 2:
        sample = chunks[0][:120]
        return False, f"boilerplate in {boilerplate_hits}/5 leading chunks: {sample!r}"
    return True, "ok"


def log_invalid(pdf_path: Path, reason: str):
    from datetime import datetime
    LOG_DIR.mkdir(exist_ok=True)
    with open(INVALID_LOG, "a") as f:
        f.write(f"{datetime.now().isoformat()} | {pdf_path.parent} | {pdf_path.name} | {reason}\n")


# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description="Ingest Epstein corpus to Semiont + OpenSearch")
    parser.add_argument("--dir",           type=Path, default=RAW_DIR, help="Root directory to scan for PDFs")
    parser.add_argument("--limit",         type=int,  default=None,    help="Max files to process")
    parser.add_argument("--batch-size",    type=int,  default=100,     help="Files per checkpoint save (default 100)")
    parser.add_argument("--skip-semiont",  action="store_true", help="Skip Semiont upload (OS-only mode)")
    parser.add_argument("--skip-vision",   action="store_true", help="Skip image captioning")
    parser.add_argument("--vision-only",   action="store_true", help="Add image captions to already-indexed docs")
    parser.add_argument("--reset",         action="store_true", help="Ignore checkpoint, start from scratch")
    args = parser.parse_args()

    if args.vision_only:
        _run_vision_only(args)
        return

    from datetime import datetime, timezone

    LOG_DIR.mkdir(exist_ok=True)
    section = args.dir.name
    skipped_scanned_log = LOG_DIR / f"skipped_{section}.txt"

    # Per-section files so parallel workers don't collide
    global CHECKPOINT_FILE, INVALID_LOG
    CHECKPOINT_FILE = LOG_DIR / f"checkpoint_{section}.json"
    INVALID_LOG     = LOG_DIR / f"invalid_{section}.log"

    # ── Collect PDFs ──────────────────────────────────────────────────────────
    print("Scanning for PDFs...")
    all_pdfs = [p for p in sorted(args.dir.rglob("*.pdf")) if not p.name.startswith("._")]
    print(f"Found {len(all_pdfs)} PDFs (validity check deferred to per-file)")

    # ── Checkpoint resume ─────────────────────────────────────────────────────
    completed = set() if args.reset else load_checkpoint()
    if args.reset and CHECKPOINT_FILE.exists():
        CHECKPOINT_FILE.unlink()
        print("[checkpoint] Reset — starting from scratch")

    pdfs = [p for p in all_pdfs if p.name not in completed]
    if args.limit:
        pdfs = pdfs[:args.limit]

    if not pdfs:
        print("All PDFs already ingested. Use --reset to re-ingest.")
        sys.exit(0)

    print(f"Processing {len(pdfs)} PDFs ({len(completed)} already done)\n")

    session = requests.Session()
    ensure_index(session)

    skipped_scanned = []
    total_chunks = 0
    total_files = 0
    total_invalid = 0

    for pdf_path in pdfs:
        dataset = pdf_path.parent.name
        print(f"[{total_files+1}/{len(pdfs)}] {pdf_path.name}")

        # ── Magic byte validity check ─────────────────────────────────────────
        if not is_valid_pdf(pdf_path):
            print(f"  [invalid] not a PDF (age-gate HTML?) — skipping")
            log_invalid(pdf_path, "magic bytes check failed — not %PDF-")
            completed.add(pdf_path.name)
            total_invalid += 1
            continue

        # ── OCR gap routing ───────────────────────────────────────────────────
        source, used_ocr = resolve_source(pdf_path)
        if source is None:
            print(f"  [skip] scanned PDF — no OCR sidecar")
            skipped_scanned.append(str(pdf_path))
            completed.add(pdf_path.name)  # don't retry unreadable files
            continue
        if used_ocr:
            print(f"  [ocr]  using sidecar {source.name}")

        # ── Semiont upload ────────────────────────────────────────────────────
        if not args.skip_semiont:
            resource_id = semiont_upload(source)
            if resource_id:
                print(f"  [semiont] resource_id={resource_id}")
            else:
                print(f"  [semiont warn] upload failed; using filename as id")
                resource_id = pdf_path.stem.replace(" ", "_")
        else:
            resource_id = pdf_path.stem.replace(" ", "_")

        # ── Text extraction & chunking ────────────────────────────────────────
        try:
            text = extract_text(source)
        except Exception as e:
            print(f"  [err] text extraction failed: {e}")
            continue

        chunks = chunk_text(text)

        if source.suffix != ".txt" and not args.skip_vision:
            img_captions = extract_image_captions(pdf_path)
            if img_captions:
                print(f"  [vision] {len(img_captions)} images captioned")
            chunks.extend(img_captions)

        ok, reason = validate_chunks(chunks, pdf_path)
        if not ok:
            print(f"  [invalid] {reason} — skipping")
            log_invalid(pdf_path, reason)
            completed.add(pdf_path.name)
            total_invalid += 1
            continue

        print(f"  {len(chunks)} chunks", end="")

        # ── Embedding ─────────────────────────────────────────────────────────
        all_embeddings = []
        for i in range(0, len(chunks), BATCH_SIZE):
            batch = chunks[i:i + BATCH_SIZE]
            try:
                all_embeddings.extend(embed_batch(batch))
            except Exception as e:
                print(f"\n  [embed err] batch {i//BATCH_SIZE}: {e}")
                all_embeddings.extend([[0.0] * 768] * len(batch))

        print(f" → {len(all_embeddings)} embeddings")

        # ── OpenSearch index ──────────────────────────────────────────────────
        ingested_at = datetime.now(timezone.utc).isoformat()
        try:
            index_chunks(session, resource_id, pdf_path.name, dataset,
                         chunks, all_embeddings, ingested_at)
            print(f"  [os] indexed")
        except Exception as e:
            print(f"  [os err] {e}")
            continue

        total_chunks += len(chunks)
        total_files += 1
        completed.add(pdf_path.name)
        time.sleep(INGEST_DELAY_S)

        # ── Checkpoint save every batch_size files ────────────────────────────
        if total_files % args.batch_size == 0:
            save_checkpoint(completed)
            print(f"[checkpoint] Saved at {total_files} files")

    # ── Final checkpoint + summary ────────────────────────────────────────────
    save_checkpoint(completed)

    if skipped_scanned:
        with open(skipped_scanned_log, "w") as f:
            f.write("# Scanned PDFs skipped\n# Run: python scripts/ocr_preprocess.py\n\n")
            f.write("\n".join(skipped_scanned) + "\n")
        print(f"\n[warn] {len(skipped_scanned)} scanned PDFs skipped → {skipped_scanned_log}")

    print(f"\n✓ Ingest complete: {total_files} files, {total_chunks} chunks")
    if total_invalid:
        print(f"  {total_invalid} invalid files logged → {INVALID_LOG}")
    print(f"  curl http://localhost:9200/{OPENSEARCH_INDEX}/_count")
    print(f"  curl 'http://localhost:9200/{OPENSEARCH_INDEX}/_search?q=Ghislaine+Maxwell&size=3'")


if __name__ == "__main__":
    main()