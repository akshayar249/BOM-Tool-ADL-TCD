#!/usr/bin/env python3
"""
run_pipeline.py
================
Colab / no-database variant of the pipeline for sales-order info sheets:

    name.pdf  --[OCR]-->  name.md  --[LLM extraction]-->  name.json

This is run_pipeline.py with the Postgres/ChromaDB ingest stage removed.
Point it at a folder of PDFs with ANY filenames (e.g. "40069-VANTAGE 800 -
FANUC-Spu....pdf") and it auto-discovers every matching file and produces a
.md and a .json next to each one. Nothing needs to be renamed first.

Designed to run inside a Google Colab notebook, where a local Postgres/
Chroma server generally isn't available and the runtime is ephemeral anyway.
If you later want the DB stage back, use run_pipeline.py instead (same
OCR/extraction code, plus --skip-db to opt out per-run).

USAGE
-----
    python run_pipeline_colab.py --dir "/content/drive/MyDrive/path/to/pdfs"

Common options:
    --pattern "*.pdf"  glob pattern used to find input files (default: *.pdf)
    --force            redo every stage even if output files already exist

RESUMABILITY
------------
Each stage writes its output to disk (name.md, name.json, next to the source
PDF) before moving to the next stage. On every run, if an output file already
exists, that stage is SKIPPED for that file (so a disconnected/restarted
Colab runtime can just re-run the same command to pick up where it left off).
Use --force to ignore existing files and redo everything.

INSTALL (see the Colab cells in chat for the full sequence)
-------
    curl -fsSL https://ollama.com/install.sh | sh
    ollama pull hf.co/bartowski/allenai_olmOCR-2-7B-1025-GGUF:Q4_K_M
    ollama pull llama3.1:8b-instruct-q4_K_M
    pip install pypdfium2 ollama pydantic
"""

import argparse
import base64
import io
import json
import sys
import time
import traceback
from datetime import datetime
from pathlib import Path
from typing import Optional

import pypdfium2 as pdfium
import ollama
from pydantic import BaseModel, Field


# ============================================================
# CONFIG
# ============================================================
OCR_MODEL = "hf.co/bartowski/allenai_olmOCR-2-7B-1025-GGUF:Q4_K_M"
EXTRACT_MODEL = "llama3.1:8b-instruct-q4_K_M"
TARGET_LONGEST_DIM = 1024

OCR_PROMPT = (
    "Below is the image of one page of a document. Return a clean "
    "Markdown representation of this page: preserve headings, "
    "paragraphs, tables (as Markdown tables), lists, and equations. "
    "Do not hallucinate content that is not present."
)

EXTRACT_SYSTEM_PROMPT = """You extract structured data from an OCR-generated Markdown document.

Return only data that is present in the document.

HEADER:
Extract metadata from the initial header block/tables only (everything before the
"Sl.No / Material ID / Material Description" table). Do not use values from later
instruction rows to fill header fields. Dates must be normalized to YYYY-MM-DD.
Use null for missing or blank fields. Keep identifiers as strings, including leading
zeros and punctuation.

IMPORTANT - do not confuse the document masthead with the customer:
The very first line(s) of the document are the MASTHEAD: the issuing company's name
and the document title (e.g. "<Company Name>" followed by "CUSTOMER SALES ORDER INFO
SHEET"). This masthead repeats at the top of every page. It is NEVER the customer_name,
even if it looks similar to text elsewhere in the document.

The real customer is given in a separate block labeled "Customer (BP No: ...)" or
similar. Within that specific block:
- customer_bp_no = the number in parentheses after "BP No:".
- customer_name = the first line of text inside that block, immediately after the
  "Customer (BP No: ...)" label line. Copy it exactly as printed, even if it contains
  words like "Self" or looks similar to the masthead company name.
- customer_address = all remaining lines in that same block (street, area, city, PIN
  code), joined into one string separated by commas.

Example (generic, illustrative only):
  Masthead: "Acme Corp\\nCUSTOMER SALES ORDER INFO SHEET"
  Customer block: "Customer (BP No: 5555)\\nWidget Traders Pvt Ltd\\n12 Market Road\\nCity-100001"
  -> customer_bp_no: "5555"
  -> customer_name: "Widget Traders Pvt Ltd"
  -> customer_address: "12 Market Road, City-100001"
  -> customer_name is NOT "Acme Corp" (that is the masthead).

SPECIAL INSTRUCTIONS:
Find the table row titled "Special requirement/Instructions" or an equivalent title.
Extract every subsequent instruction row until the end of the document. The relevant
text is in the Scope of Supply column. Preserve the original printed serial number and
copy each instruction faithfully; do not summarize, correct spelling, or merge rows.
Exclude rows consisting only of repeated separator characters such as ~ or -.
Include EARLIER and AMENDED AS rows and set is_amendment=true for them. Set it to false
for all other instruction rows.

If OCR contains obvious table markup, use row and column labels to identify values.
Do not invent missing information. Return only JSON matching the supplied schema."""


# ============================================================
# Schema (from md_to_json.py)
# ============================================================
class Header(BaseModel):
    customer_name: Optional[str] = Field(None, description="Customer company name")
    customer_bp_no: Optional[str] = Field(None, description="Customer BP number")
    customer_address: Optional[str] = Field(None, description="Complete customer address")
    plant: Optional[str] = Field(None, description="Plant code")
    customer_so_no: Optional[str] = Field(None, description="Customer sales order number")
    crm_order_id: Optional[str] = Field(None, description="CRM order ID")
    so_type: Optional[str] = Field(None, description="Sales order type")
    so_approval_date: Optional[str] = Field(None, description="SO approval date, YYYY-MM-DD")
    internal_so_no: Optional[str] = Field(None, description="Internal sales order number")
    po_number: Optional[str] = Field(None, description="Purchase order number")
    po_date: Optional[str] = Field(None, description="PO date, YYYY-MM-DD")
    material_id: Optional[str] = Field(None, description="Material ID")
    material_description: Optional[str] = Field(None, description="Material description")
    required_delivery_date: Optional[str] = Field(None, description="Required delivery date, YYYY-MM-DD")
    quantity: Optional[int] = Field(None, description="Required quantity")
    machine_serial_no: Optional[str] = Field(None, description="Machine serial number")


class SpecialInstruction(BaseModel):
    sl_no: int = Field(..., description="Original printed serial number")
    instruction: str = Field(..., description="Instruction copied faithfully from Markdown")
    is_amendment: bool = Field(..., description="True for EARLIER or AMENDED AS rows")


class Extraction(BaseModel):
    header: Header
    special_instructions: list[SpecialInstruction]


# ============================================================
# Logging helper
# ============================================================
_LOG_FILE = None


def init_log(pdf_dir: Path) -> None:
    global _LOG_FILE
    _LOG_FILE = (pdf_dir / "pipeline.log").open("a", encoding="utf-8")


def log(message: str) -> None:
    stamped = f"[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] {message}"
    print(stamped, flush=True)
    if _LOG_FILE is not None:
        _LOG_FILE.write(stamped + "\n")
        _LOG_FILE.flush()


# ============================================================
# Stage 1: PDF -> Markdown  (from pdf_to_md.py)
# ============================================================
def render_page(pdf, page_index: int):
    page = pdf[page_index]
    width_pt, height_pt = page.get_size()
    scale = TARGET_LONGEST_DIM / max(width_pt, height_pt)
    bitmap = page.render(scale=scale)
    return bitmap.to_pil().convert("RGB")


def image_to_b64(img) -> str:
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return base64.b64encode(buf.getvalue()).decode("utf-8")


def ocr_page(pdf, page_index: int) -> str:
    img = render_page(pdf, page_index)
    response = ollama.generate(
        model=OCR_MODEL,
        prompt=OCR_PROMPT,
        images=[image_to_b64(img)],
        options={"num_ctx": 8192, "temperature": 0.1},
    )
    return response["response"].strip()


def pdf_to_markdown(pdf_path: Path, out_path: Path) -> None:
    pdf = pdfium.PdfDocument(str(pdf_path))
    try:
        num_pages = len(pdf)
        out_path.write_text("", encoding="utf-8")  # start fresh
        with out_path.open("a", encoding="utf-8") as f:
            for i in range(num_pages):
                log(f"    OCR page {i + 1}/{num_pages}")
                text = ocr_page(pdf, i)
                if i > 0:
                    f.write("\n\n---\n\n")
                f.write(f"<!-- page {i + 1} -->\n\n{text}\n")
                f.flush()
    finally:
        pdf.close()


# ============================================================
# Stage 2: Markdown -> JSON  (from md_to_json.py)
# ============================================================
def extract_json(markdown: str) -> Extraction:
    schema = Extraction.model_json_schema()
    schema_text = json.dumps(schema, ensure_ascii=False)
    user_prompt = (
        "Extract the requested fields from this Markdown document.\n\n"
        "JSON schema:\n" + schema_text + "\n\n"
        "DOCUMENT START\n" + markdown + "\nDOCUMENT END"
    )

    response = ollama.chat(
        model=EXTRACT_MODEL,
        messages=[
            {"role": "system", "content": EXTRACT_SYSTEM_PROMPT},
            {"role": "user", "content": user_prompt},
        ],
        format=schema,
        options={"temperature": 0, "num_ctx": 16384},
        stream=False,
    )
    return Extraction.model_validate_json(response.message.content)


# ============================================================
# Orchestration: one PDF through both stages
# ============================================================
def process_one(idx: int, total: int, pdf_path: Path, md_path: Path, json_path: Path,
                 force: bool) -> str:
    """Returns 'ok', 'skipped', or 'failed'."""
    tag = f"[{idx}/{total}] {pdf_path.name}"

    if not pdf_path.is_file():
        log(f"{tag} SKIP: file not found")
        return "skipped"

    # Stage 1: PDF -> Markdown
    try:
        if md_path.exists() and not force:
            log(f"{tag} MD already exists, skipping OCR ({md_path.name})")
        else:
            log(f"{tag} Stage 1/2: OCR -> {md_path.name}")
            t0 = time.monotonic()
            pdf_to_markdown(pdf_path, md_path)
            log(f"{tag}   done in {time.monotonic() - t0:.1f}s")
    except Exception as e:
        log(f"{tag} FAILED at Stage 1 (OCR): {e}")
        log(traceback.format_exc())
        return "failed"

    # Stage 2: Markdown -> JSON
    try:
        if json_path.exists() and not force:
            log(f"{tag} JSON already exists, skipping extraction ({json_path.name})")
        else:
            log(f"{tag} Stage 2/2: extracting -> {json_path.name}")
            t0 = time.monotonic()
            markdown = md_path.read_text(encoding="utf-8")
            result = extract_json(markdown)
            json_path.write_text(
                json.dumps(result.model_dump(), indent=2, ensure_ascii=False) + "\n",
                encoding="utf-8",
            )
            log(f"{tag}   done in {time.monotonic() - t0:.1f}s "
                f"({len(result.special_instructions)} instructions)")
    except Exception as e:
        log(f"{tag} FAILED at Stage 2 (extraction): {e}")
        log(traceback.format_exc())
        return "failed"

    return "ok"


# ============================================================
# Main
# ============================================================
def main() -> None:
    parser = argparse.ArgumentParser(
        description="Batch pipeline: PDFs -> Markdown -> JSON (no database stage)"
    )
    parser.add_argument("--dir", required=True, help="Folder containing PDF files")
    parser.add_argument("--pattern", default="*.pdf",
                         help="Glob pattern (relative to --dir) used to find PDFs. "
                              "Default: *.pdf (top-level only, not subfolders).")
    parser.add_argument("--force", action="store_true",
                         help="Redo every stage even if output files already exist")
    args = parser.parse_args()

    pdf_dir = Path(args.dir).expanduser().resolve()
    if not pdf_dir.is_dir():
        raise SystemExit(f"--dir does not exist: {pdf_dir}")

    pdf_paths = sorted(
        p for p in pdf_dir.glob(args.pattern)
        if p.is_file() and p.suffix.lower() == ".pdf"
    )
    if not pdf_paths:
        raise SystemExit(f"No PDFs matching {args.pattern!r} found in {pdf_dir}")

    init_log(pdf_dir)
    log(f"=== Run started: {pdf_dir}, {len(pdf_paths)} PDF(s) matching {args.pattern!r}, "
        f"force={args.force} ===")

    counts = {"ok": 0, "skipped": 0, "failed": 0}
    failed_names = []
    total = len(pdf_paths)

    try:
        for idx, pdf_path in enumerate(pdf_paths, start=1):
            md_path = pdf_path.with_suffix(".md")
            json_path = pdf_path.with_suffix(".json")

            status = process_one(idx, total, pdf_path, md_path, json_path, args.force)
            counts[status] += 1
            if status == "failed":
                failed_names.append(pdf_path.name)
    except KeyboardInterrupt:
        log("Interrupted by user — re-run the same command to resume.")

    log(f"=== Done. ok={counts['ok']} skipped={counts['skipped']} "
        f"failed={counts['failed']} ===")
    if failed_names:
        log(f"Failed files: {', '.join(failed_names)}")


if __name__ == "__main__":
    main()
