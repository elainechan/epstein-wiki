#!/usr/bin/env python3
"""Upload PDFs to Semiont as extracted plain text.

Extraction pipeline per PDF page:
  1. pdftotext (fast, perfect for text-layer PDFs)
  2. If text is sparse (<50 chars/page avg) → Tesseract OCR via pdf2image
  3. If Tesseract confidence is low (<60%) → mark for VLM queue (deferred)

VLM-queued files are written to --vlm-queue-file (default: vlm_queue.txt)
for later processing with the VLM script.
"""

import argparse
import json
import os
import subprocess
import sys
import tempfile
import time
from io import BytesIO
from pathlib import Path

try:
    import requests
except ImportError:
    print("pip install requests")
    sys.exit(1)

try:
    import pypdf
except ImportError:
    print("pip install pypdf")
    sys.exit(1)

SEMIONT_URL = "http://localhost:4000"
AUTH_CACHE = Path.home() / ".local/state/semiont/auth/localhost-4000.json"

# Minimum average chars per page to consider text extraction sufficient.
MIN_CHARS_PER_PAGE = 50
# Tesseract confidence threshold — below this, queue for VLM.
MIN_OCR_CONFIDENCE = 60.0


def get_token() -> str:
    if not AUTH_CACHE.exists():
        print("Not logged in.")
        sys.exit(1)
    data = json.loads(AUTH_CACHE.read_text())
    return data["token"]


# ── Extraction methods ────────────────────────────────────────────────

def extract_via_pdftotext(pdf: Path) -> str:
    """Use poppler pdftotext — preserves text layout from text-layer PDFs."""
    try:
        result = subprocess.run(
            ["pdftotext", "-layout", str(pdf), "-"],
            capture_output=True, text=True, timeout=30,
        )
        return result.stdout
    except Exception:
        return ""


def extract_via_tesseract(pdf: Path) -> tuple[str, float]:
    """Rasterize with pdftoppm then OCR with Tesseract. Returns (text, confidence)."""
    try:
        from pdf2image import convert_from_path
        import pytesseract

        pages = convert_from_path(str(pdf), dpi=300)
        texts, confidences = [], []
        for page_img in pages:
            data = pytesseract.image_to_data(
                page_img, output_type=pytesseract.Output.DICT,
                config="--psm 1",  # automatic page segmentation
            )
            words = [w for w, c in zip(data["text"], data["conf"]) if str(w).strip() and c != -1]
            confs = [float(c) for c in data["conf"] if c != -1 and float(c) >= 0]
            texts.append(" ".join(words))
            if confs:
                confidences.append(sum(confs) / len(confs))

        avg_conf = sum(confidences) / len(confidences) if confidences else 0.0
        return "\n\n".join(texts), avg_conf
    except Exception as e:
        print(f"  [tesseract err] {e}")
        return "", 0.0


def count_pages(pdf: Path) -> int:
    try:
        reader = pypdf.PdfReader(str(pdf))
        return len(reader.pages)
    except Exception:
        return 1


def extract_text(pdf: Path, vlm_queue: list[Path]) -> str | None:
    """Return extracted text, or None if the file should be skipped entirely."""
    pages = max(count_pages(pdf), 1)

    # Step 1: Try pdftotext
    text = extract_via_pdftotext(pdf)
    avg_chars = len(text.strip()) / pages
    if avg_chars >= MIN_CHARS_PER_PAGE:
        print(f"[pdftotext {len(text):,}c]", end=" ")
        return text.strip()

    # Step 2: Tesseract OCR
    print(f"[sparse pdftotext {len(text)}c → tesseract]", end=" ", flush=True)
    ocr_text, confidence = extract_via_tesseract(pdf)
    if confidence >= MIN_OCR_CONFIDENCE and len(ocr_text.strip()) > 100:
        print(f"[ocr conf={confidence:.0f}% {len(ocr_text):,}c]", end=" ")
        return ocr_text.strip()

    # Step 3: Queue for VLM
    print(f"[ocr conf={confidence:.0f}% → VLM queue]", end=" ")
    vlm_queue.append(pdf)
    # Return best-effort OCR text if any, with a header noting VLM pass needed
    if ocr_text.strip():
        return f"[OCR-DRAFT — VLM PASS PENDING]\n\n{ocr_text.strip()}"
    return None


# ── Semiont upload ────────────────────────────────────────────────────

def upload_text(session: requests.Session, token: str, name: str, text: str) -> str | None:
    try:
        resp = session.post(
            f"{SEMIONT_URL}/resources",
            headers={"Authorization": f"Bearer {token}"},
            files={"file": (name, BytesIO(text.encode("utf-8")), "text/plain")},
            data={
                "name": name,
                "format": "text/plain",
                "storageUri": f"file://epstein-files/{name}",
            },
            timeout=30,
        )
        if resp.status_code in (200, 202):
            return resp.json().get("resourceId")
        print(f"  [http err] {resp.status_code}: {resp.text[:120]}")
        return None
    except Exception as e:
        print(f"  [err] {e}")
        return None


# ── Main ──────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description="Upload PDFs to Semiont as plain text")
    parser.add_argument("--dir", type=Path, required=True, help="Directory of PDFs")
    parser.add_argument("--limit", type=int, help="Max files")
    parser.add_argument("--vlm-queue-file", type=Path,
                        default=Path("vlm_queue.txt"),
                        help="File to write VLM-deferred PDF paths to")
    args = parser.parse_args()

    token = get_token()
    pdfs = sorted(p for p in args.dir.rglob("*.pdf") if not p.name.startswith("._"))
    if args.limit:
        pdfs = pdfs[:args.limit]

    print(f"Uploading {len(pdfs)} PDFs from {args.dir}\n")
    session = requests.Session()
    ok = fail = skip = 0
    vlm_queue: list[Path] = []

    for i, pdf in enumerate(pdfs, 1):
        print(f"[{i}/{len(pdfs)}] {pdf.name} ", end="", flush=True)
        text = extract_text(pdf, vlm_queue)
        if not text:
            print("SKIP")
            skip += 1
            continue
        name = pdf.stem + ".txt"
        rid = upload_text(session, token, name, text)
        if rid:
            print(f"→ {rid}")
            ok += 1
        else:
            print("FAILED")
            fail += 1
        time.sleep(0.05)

    print(f"\nDone: {ok} uploaded, {skip} skipped, {fail} failed")
    if vlm_queue:
        args.vlm_queue_file.write_text("\n".join(str(p) for p in vlm_queue) + "\n")
        print(f"VLM queue: {len(vlm_queue)} files written to {args.vlm_queue_file}")
        print("Run scripts/vlm_ocr.py --queue-file vlm_queue.txt to process them.")


if __name__ == "__main__":
    main()
