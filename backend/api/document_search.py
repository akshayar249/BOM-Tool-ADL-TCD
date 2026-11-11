import asyncio
import json
import os
import shutil
import tempfile
import time
from datetime import date
from pathlib import Path
from typing import Any, AsyncGenerator, Optional

from fastapi import APIRouter, HTTPException, Depends, Form, Request, UploadFile, File
from fastapi.responses import StreamingResponse

from backend.core import dependencies
from backend.core.dependencies import Resources, get_resources, get_or_create_lift_extractor
from backend.schemas.search import SheetFilter, DocumentSearchRequest
import search.info_sheet_search as iss
from search.info_sheet_search import SheetResult, search_info_sheets_batch

import pypdfium2 as pdfium

router = APIRouter()


def _sse_pack(event: str, data: dict) -> str:
    return f"event: {event}\ndata: {json.dumps(data, default=str)}\n\n"


def aggregate_sheet_results(batch_results: list[list[SheetResult]]) -> list[SheetResult]:
    sheet_scores: dict[str, list[float]] = {}
    sheet_hits: dict[str, dict[str, Any]] = {}
    sheet_headers: dict[str, tuple[dict | None, int | None]] = {}

    for sheets_for_q in batch_results:
        for sheet in sheets_for_q:
            path = sheet.source_path
            best_score = sheet.best_fused_score
            sheet_scores.setdefault(path, []).append(best_score)
            sheet_headers.setdefault(path, (sheet.header, sheet.total_instructions))
            sheet_hits.setdefault(path, {})
            for hit in sheet.matched:
                if hit.id not in sheet_hits[path] or hit.fused_score > sheet_hits[path][hit.id].fused_score:
                    sheet_hits[path][hit.id] = hit

    aggregated = [(sum(scores), path) for path, scores in sheet_scores.items()]
    aggregated.sort(key=lambda x: x[0], reverse=True)

    final_sheets: list[SheetResult] = []
    for _, path in aggregated:
        sheet = SheetResult(source_path=path)
        sheet.matched = list(sheet_hits[path].values())
        sheet.matched.sort(key=lambda h: h.fused_score, reverse=True)
        sheet.header, sheet.total_instructions = sheet_headers.get(path, (None, None))
        final_sheets.append(sheet)

    return final_sheets


@router.post("/search/document/pdf")
async def search_document_pdf(
    request: Request,
    file: UploadFile = File(...),
    material: Optional[str] = Form(None),
    material_id: Optional[str] = Form(None),
    date_from: Optional[date] = Form(None),
    date_to: Optional[date] = Form(None),
    exact_match: bool = Form(False),
    pool: int = Form(30),
):
    """
    Accepts an uploaded PDF, extracts structured header & instructions via Datalab Lift VLM,
    and executes precedent retrieval, streaming real-time 5-stage progress over SSE.
    """
    resources = request.app.state.resources
    if not resources:
        async def err_gen():
            yield _sse_pack("error", {"error": "Search resources not loaded on server."})
        return StreamingResponse(err_gen(), media_type="text/event-stream")

    sheet_filter = SheetFilter(
        material=material.strip() if material and material.strip() else None,
        material_id=material_id.strip() if material_id and material_id.strip() else None,
        date_from=date_from,
        date_to=date_to,
    )

    async def event_generator() -> AsyncGenerator[str, None]:
        t0 = time.time()
        temp_dir = tempfile.mkdtemp(prefix="ace_lift_pdf_")
        temp_pdf_path = Path(temp_dir) / (file.filename or "uploaded.pdf")

        try:
            # Stage 1: Document Ingestion & Page Analysis
            content = await file.read()
            with open(temp_pdf_path, "wb") as f:
                f.write(content)

            def _get_page_count(path: Path) -> int:
                doc = pdfium.PdfDocument(path)
                try:
                    return len(doc)
                finally:
                    doc.close()

            n_pages = await asyncio.to_thread(_get_page_count, temp_pdf_path)

            yield _sse_pack("progress", {
                "stage": 1,
                "total_stages": 5,
                "label": "Document Ingestion & Page Analysis",
                "detail": f"Parsed PDF document structure ({n_pages} pages) for layout analysis.",
                "elapsed_seconds": round(time.time() - t0, 1)
            })

            # Stage 2: VLM Model Readiness
            yield _sse_pack("progress", {
                "stage": 2,
                "total_stages": 5,
                "label": "VLM Pipeline Readiness",
                "detail": "Verifying Datalab Lift VLM weights and GPU memory allocation...",
                "elapsed_seconds": round(time.time() - t0, 1)
            })

            load_task = asyncio.create_task(
                asyncio.to_thread(dependencies.get_or_create_lift_extractor, request.app)
            )
            while not load_task.done():
                try:
                    await asyncio.wait_for(asyncio.shield(load_task), timeout=1.0)
                except asyncio.TimeoutError:
                    yield _sse_pack("progress", {
                        "stage": 2,
                        "total_stages": 5,
                        "label": "VLM Pipeline Readiness",
                        "detail": "Verifying Datalab Lift VLM weights and GPU memory allocation...",
                        "elapsed_seconds": round(time.time() - t0, 1)
                    })
            extractor = await load_task

            # Stage 3: Neural Clause Extraction
            yield _sse_pack("progress", {
                "stage": 3,
                "total_stages": 5,
                "label": "Neural Layout Analysis & Clause Extraction",
                "detail": f"Running visual language inference across {n_pages} page buffers...",
                "elapsed_seconds": round(time.time() - t0, 1)
            })

            extract_task = asyncio.create_task(
                asyncio.to_thread(extractor.extract_pdf, temp_pdf_path)
            )
            while not extract_task.done():
                try:
                    await asyncio.wait_for(asyncio.shield(extract_task), timeout=1.0)
                except asyncio.TimeoutError:
                    yield _sse_pack("progress", {
                        "stage": 3,
                        "total_stages": 5,
                        "label": "Neural Layout Analysis & Clause Extraction",
                        "detail": f"Running visual language inference across {n_pages} page buffers...",
                        "elapsed_seconds": round(time.time() - t0, 1)
                    })
            validated, total_tokens, total_latency = await extract_task

            # Stage 4: Schema Normalization & Noise Filtering
            header_dict = validated.header.model_dump() if hasattr(validated.header, "model_dump") else dict(validated.header)
            instructions_list = [
                inst.model_dump() if hasattr(inst, "model_dump") else dict(inst)
                for inst in validated.special_instructions
            ]
            query_strings = [inst.get("instruction", "").strip() for inst in instructions_list if inst.get("instruction", "").strip()]

            yield _sse_pack("progress", {
                "stage": 4,
                "total_stages": 5,
                "label": "Schema Normalization & Noise Filtering",
                "detail": f"Parsed header and {len(query_strings)} special instruction clauses.",
                "elapsed_seconds": round(time.time() - t0, 1)
            })

            # Stage 5: Dual Hybrid Precedent Retrieval
            yield _sse_pack("progress", {
                "stage": 5,
                "total_stages": 5,
                "label": "Precedent Vector & BM25s Retrieval",
                "detail": "Querying dual hybrid pipeline across 7,413 historical vectors via RRF...",
                "elapsed_seconds": round(time.time() - t0, 1)
            })

            final_sheets: list[SheetResult] = []
            if query_strings:
                database_url = os.getenv("DATABASE_URL")
                batch_results = await asyncio.to_thread(
                    iss.search_info_sheets_batch,
                    queries=query_strings,
                    embedder=resources.embedder,
                    collection=resources.collection,
                    n_sheets=20,
                    pool=pool,
                    database_url=database_url,
                    filters=sheet_filter,
                    exact_match=exact_match,
                )
                final_sheets = aggregate_sheet_results(batch_results)

            # Convert SheetResult to JSON-serializable structure
            results_out = []
            for s in final_sheets:
                results_out.append({
                    "source_path": s.source_path,
                    "header": s.header,
                    "total_instructions": s.total_instructions,
                    "matched": [
                        {
                            "sl_no": h.sl_no,
                            "text": h.text,
                            "is_amendment": h.is_amendment,
                            "semantic_distance": h.semantic_distance,
                            "lexical_score": h.lexical_score,
                            "fused_score": h.fused_score,
                        }
                        for h in s.matched
                    ]
                })

            # Emit final complete event
            yield _sse_pack("complete", {
                "stage": 5,
                "total_stages": 5,
                "elapsed_seconds": round(time.time() - t0, 1),
                "extracted_order": {
                    "header": header_dict,
                    "special_instructions": instructions_list
                },
                "results": results_out
            })

        except Exception as err:
            yield _sse_pack("error", {
                "error": str(err) or "An error occurred during PDF processing.",
                "elapsed_seconds": round(time.time() - t0, 1)
            })
        finally:
            shutil.rmtree(temp_dir, ignore_errors=True)

    return StreamingResponse(
        event_generator(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no"
        }
    )



@router.post("/search/document")
def search_document(req: DocumentSearchRequest, resources: Resources = Depends(get_resources)):
    """
    Accepts a list of instruction strings from an uploaded Info Sheet.
    Runs a search for each instruction, and aggregates the scores at the document level.
    The final score for a past Info Sheet is the sum of its best fused-scores
    across all the queried instructions.

    Material/date filters are applied inside the per-instruction search, so a
    sheet excluded by the filter never contributes to the aggregate score.
    """
    if not resources:
        raise HTTPException(status_code=500, detail="Search resources not loaded.")

    embedder = resources.embedder
    collection = resources.collection
    database_url = os.getenv("DATABASE_URL")

    final_sheets: list[SheetResult] = []
    if req.instructions:
        batch_results = search_info_sheets_batch(
            queries=req.instructions,
            embedder=embedder,
            collection=collection,
            n_sheets=20,
            pool=req.pool,
            database_url=database_url,
            filters=req.to_sheet_filter(),
            exact_match=req.exact_match,
        )
        final_sheets = aggregate_sheet_results(batch_results)

    return {"results": final_sheets}
