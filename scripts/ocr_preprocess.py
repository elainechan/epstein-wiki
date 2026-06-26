#!/usr/bin/env python3
"""
ocr_preprocess.py
Pre-processes scanned PDFs into text files before ingest.

Semiont v0.5.7 has no native OCR capability. Scanned PDFs (image-only)
pass through `semiont yield --upload` as opaque blobs — zero chunks, zero
search results. This script bridges the gap by running Tesseract OCR and
writing a .txt sidecar file that ingest.py picks up instead.

See: semiont-missing-features.md — Gap Note: OCR / Image Recognition

Usage:
    # Process files listed in logs/scanned_files.txt (from batch_download):
    python scripts/ocr_preprocess.py

    # Process a specific file:
    python scripts/ocr_preprocess.py --file raw/dataset_1/some_scanned.pdf

    # Process all PDFs in a directory (re-checks each for scanned status):
    python scripts/ocr_preprocess.py --dir raw/dataset_1/

Dependencies:
    pip install pymupdf pytesseract Pillow --break-system-packages
    # Also requires Tesseract binary: brew install tesseract / apt install tesseract-ocr

Full-build upgrade path:
    Replace this script with AWS Textract, Google Document AI, or a
    self-hosted Surya/Marker pipeline wired as a pre-processing stage
    in the Ingest Agent. Semiont itself would need a yield --ocr flag
    or a pre-processor plugin hook to handle OCR natively.
"""

import argparse
import sys
import io
from pathlib import Path

try:
    import fitz  # pymupdf
    import pytesseract
    from PIL import Image
except ImportError:
    print("Missing dependencies.")
    print("Run: pip install pymupdf pytesseract Pillow --break-system-packages")
    print("Also: brew install tesseract  OR  sudo apt install tesseract-ocr")
    sys.exit(1)

LOG_DIR = Path("logs")
SCANNED_LOG = LOG_DIR / "scanned_files.txt"
OCR_LOG = LOG_DIR / "ocr_processed.txt"
DPI = 300  # higher DPI = better OCR accuracy, slower processing


def pdf_to_text_via_ocr(pdf_path: Path) -> str:
    """
    Rasterize each page at DPI resolution and run Tesseract OCR.
    Returns concatenated plain text for the full document.
    """
    doc = fitz.open(str(pdf_path))
    pages = []
    for i, page in enumerate(doc):
        print(f"    page {i+1}/{len(doc)}", end="\r")
        pix = page.get_pixmap(dpi=DPI)
        img = Image.open(io.BytesIO(pix.tobytes("png")))
        page_text = pytesseract.image_to_string(img, lang="eng")
        pages.append(page_text)
    print()  # newline after progress
    return "\n\n".join(pages)


def process_file(pdf_path: Path, force: bool = False) -> Path | None:
    """
    OCR a single PDF and write a .txt sidecar.
    Returns the sidecar path, or None if skipped.
    """
    sidecar = pdf_path.with_suffix(".ocr.txt")

    if sidecar.exists() and not force:
        print(f"  [skip] {pdf_path.name} — sidecar already exists")
        return sidecar

    print(f"  [ocr]  {pdf_path.name}")
    try:
        text = pdf_to_text_via_ocr(pdf_path)
        if len(text.strip()) < 50:
            print(f"  [warn] OCR produced very little text for {pdf_path.name}")
            print(f"         File may be corrupt, heavily formatted, or non-English.")

        # Write sidecar with provenance header
        sidecar.write_text(
            f"# OCR output — {pdf_path.name}\n"
            f"# Source: {pdf_path}\n"
            f"# Processed by: ocr_preprocess.py (Tesseract, DPI={DPI})\n"
            f"# Semiont gap: no native OCR — sidecar ingested instead of original PDF\n\n"
            + text,
            encoding="utf-8"
        )
        print(f"         → {sidecar.name} ({len(text)} chars)")
        return sidecar
    except Exception as e:
        print(f"  [err]  {pdf_path.name}: {e}")
        return None


def main():
    parser = argparse.ArgumentParser(description="OCR pre-processor for scanned PDFs")
    parser.add_argument("--file", type=Path, help="Process a single PDF file")
    parser.add_argument("--dir", type=Path, help="Process all PDFs in a directory")
    parser.add_argument("--force", action="store_true", help="Re-process even if sidecar exists")
    args = parser.parse_args()

    LOG_DIR.mkdir(exist_ok=True)
    processed = []

    if args.file:
        files = [args.file]
    elif args.dir:
        files = list(args.dir.glob("*.pdf"))
        print(f"Found {len(files)} PDFs in {args.dir}")
    elif SCANNED_LOG.exists():
        files = [Path(p.strip()) for p in SCANNED_LOG.read_text().splitlines()
                 if p.strip() and not p.startswith("#")]
        print(f"Processing {len(files)} scanned files from {SCANNED_LOG}")
    else:
        print(f"No input specified and {SCANNED_LOG} not found.")
        print("Run batch_download_epstein_files.py first, or pass --file / --dir")
        sys.exit(1)

    for pdf_path in files:
        if not pdf_path.exists():
            print(f"  [miss] {pdf_path} not found, skipping")
            continue
        result = process_file(pdf_path, force=args.force)
        if result:
            processed.append(str(result))

    if processed:
        with open(OCR_LOG, "w") as f:
            f.write("# OCR-processed sidecar files — ingest these instead of original PDFs\n\n")
            f.write("\n".join(processed) + "\n")
        print(f"\n✓ {len(processed)} files OCR-processed. Sidecars listed in {OCR_LOG}")
    else:
        print("\nNo files processed.")

    print("\nNext: python scripts/ingest.py")
    print("      ingest.py will auto-detect .ocr.txt sidecars and prefer them over scanned PDFs")


if __name__ == "__main__":
    main()