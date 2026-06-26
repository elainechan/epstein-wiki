#!/usr/bin/env python3
"""Process VLM-deferred PDFs using qwen2.5vl:3b via Ollama.

Usage:
  python3 vlm_ocr.py --queue-file vlm_queue.txt [--limit N]

For each PDF: rasterize pages → send to qwen2.5vl:3b → upload extracted text.
Slow (~5-15s/page) but handles handwriting, blurry tables, forms, flight plans.
"""

import argparse
import base64
import json
import sys
import time
from io import BytesIO
from pathlib import Path

try:
    import requests
except ImportError:
    print("pip install requests")
    sys.exit(1)

try:
    from pdf2image import convert_from_path
except ImportError:
    print("pip install pdf2image")
    sys.exit(1)

try:
    from PIL import Image
except ImportError:
    print("pip install Pillow")
    sys.exit(1)

SEMIONT_URL = "http://localhost:4000"
OLLAMA_URL = "http://localhost:11434"
VLM_MODEL = "qwen2.5vl:3b"
AUTH_CACHE = Path.home() / ".local/state/semiont/auth/localhost-4000.json"


def get_token() -> str:
    if not AUTH_CACHE.exists():
        print("Not logged in.")
        sys.exit(1)
    data = json.loads(AUTH_CACHE.read_text())
    return data["token"]


def image_to_b64(img: Image.Image, max_dim: int = 1024) -> str:
    """Resize and base64-encode image for VLM."""
    w, h = img.size
    if max(w, h) > max_dim:
        scale = max_dim / max(w, h)
        img = img.resize((int(w * scale), int(h * scale)), Image.LANCZOS)
    buf = BytesIO()
    img.save(buf, format="JPEG", quality=85)
    return base64.b64encode(buf.getvalue()).decode()


def vlm_ocr_page(b64_image: str) -> str:
    """Ask qwen2.5vl to extract all text from a page image."""
    payload = {
        "model": VLM_MODEL,
        "prompt": (
            "Extract ALL text from this document page exactly as it appears. "
            "Preserve structure: headers, columns, tables, form fields, dates, names. "
            "For handwriting, transcribe best effort. "
            "Output only the extracted text, no commentary."
        ),
        "images": [b64_image],
        "stream": False,
        "options": {"temperature": 0.0},
    }
    try:
        resp = requests.post(f"{OLLAMA_URL}/api/generate", json=payload, timeout=120)
        if resp.status_code == 200:
            return resp.json().get("response", "")
        return ""
    except Exception as e:
        print(f"  [vlm err] {e}")
        return ""


def extract_via_vlm(pdf: Path) -> str | None:
    try:
        pages = convert_from_path(str(pdf), dpi=200)
    except Exception as e:
        print(f"  [pdf2image err] {e}")
        return None

    parts = []
    for i, page_img in enumerate(pages, 1):
        print(f"  page {i}/{len(pages)}", end=" ", flush=True)
        b64 = image_to_b64(page_img)
        text = vlm_ocr_page(b64)
        if text.strip():
            parts.append(text.strip())
    return "\n\n---\n\n".join(parts) if parts else None


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


def main():
    parser = argparse.ArgumentParser(description="VLM OCR for complex/scanned PDFs")
    parser.add_argument("--queue-file", type=Path, required=True)
    parser.add_argument("--limit", type=int)
    args = parser.parse_args()

    if not args.queue_file.exists():
        print(f"Queue file not found: {args.queue_file}")
        sys.exit(1)

    pdfs = [Path(p.strip()) for p in args.queue_file.read_text().splitlines() if p.strip()]
    if args.limit:
        pdfs = pdfs[:args.limit]

    token = get_token()
    session = requests.Session()
    ok = fail = 0

    print(f"VLM OCR: processing {len(pdfs)} PDFs with {VLM_MODEL}\n")
    for i, pdf in enumerate(pdfs, 1):
        print(f"[{i}/{len(pdfs)}] {pdf.name}")
        text = extract_via_vlm(pdf)
        if not text:
            print("  → SKIP (no text extracted)")
            fail += 1
            continue
        name = pdf.stem + ".txt"
        rid = upload_text(session, token, name, text)
        if rid:
            print(f"  → {rid} ({len(text):,} chars)")
            ok += 1
        else:
            print("  → UPLOAD FAILED")
            fail += 1
        time.sleep(0.1)

    print(f"\nDone: {ok} uploaded, {fail} failed")


if __name__ == "__main__":
    main()
