import csv
import os
from pathlib import Path
from typing import Any
from fastapi import APIRouter, HTTPException, Query
from fastapi.responses import FileResponse
from sqlalchemy import create_engine, text

router = APIRouter(prefix="/documents", tags=["documents"])


def find_pdf_for_path(json_path: str) -> Path | None:
    if not json_path:
        return None

    # Paths may have been ingested on Windows (backslashes) or Linux (forward
    # slashes); normalize so lookup works regardless of which OS wrote them.
    normalized_path = json_path.replace("\\", "/")

    stem = Path(normalized_path).stem
    # If source path has "data/", compute relative folder inside data/
    if "data/" in normalized_path:
        rel_subpath = normalized_path.split("data/", 1)[1]
        rel_dir = os.path.dirname(rel_subpath)
    else:
        rel_dir = os.path.dirname(normalized_path)

    search_dirs = [
        Path("/app/data") / rel_dir,
        Path("data") / rel_dir,
        Path(normalized_path).parent,
        Path("/app") / rel_dir,
    ]

    for dir_path in search_dirs:
        if not dir_path.exists() or not dir_path.is_dir():
            continue

        # 1. Exact match with stem and case variations
        for ext in [".pdf", ".PDF", ".Pdf"]:
            candidate = dir_path / f"{stem}{ext}"
            if candidate.exists() and candidate.is_file():
                return candidate

        # 2. Case-insensitive stem matching
        stem_lower = stem.lower()
        for file in dir_path.iterdir():
            if file.is_file() and file.name.lower() == f"{stem_lower}.pdf":
                return file

        # 3. Fallback: return any PDF in this directory
        for file in dir_path.iterdir():
            if file.is_file() and file.suffix.lower() == ".pdf":
                return file

    return None


def find_bom_for_path(json_path: str) -> Path | None:
    if not json_path:
        return None

    normalized_path = json_path.replace("\\", "/")
    stem = Path(normalized_path).stem
    if "data/" in normalized_path:
        rel_subpath = normalized_path.split("data/", 1)[1]
        rel_dir = os.path.dirname(rel_subpath)
    else:
        rel_dir = os.path.dirname(normalized_path)

    search_dirs = [
        Path("/app/data") / rel_dir,
        Path("data") / rel_dir,
        Path(normalized_path).parent,
        Path("/app") / rel_dir,
    ]

    for dir_path in search_dirs:
        if not dir_path.exists() or not dir_path.is_dir():
            continue

        # 1. Exact match with stem
        for ext in [".csv", ".CSV"]:
            candidate = dir_path / f"{stem}{ext}"
            if candidate.exists() and candidate.is_file():
                return candidate

        # 2. Case-insensitive stem matching
        stem_lower = stem.lower()
        for file in dir_path.iterdir():
            if file.is_file() and file.name.lower() == f"{stem_lower}.csv":
                return file

        # 3. Fallback: return any multi-column CSV in this directory
        for file in dir_path.iterdir():
            if file.is_file() and file.suffix.lower() == ".csv":
                if not file.name.lower().startswith("unique sales order"):
                    return file

    return None


def resolve_bom_for_so(so_no: str) -> Path | None:
    database_url = os.getenv("DATABASE_URL")
    if database_url:
        try:
            engine = create_engine(database_url, pool_pre_ping=True)
            with engine.connect() as conn:
                row = conn.execute(
                    text("SELECT source_file FROM info_sheets WHERE customer_so_no = :so OR internal_so_no = :so"),
                    {"so": so_no},
                ).mappings().first()
                if row and row["source_file"]:
                    bom = find_bom_for_path(row["source_file"])
                    if bom:
                        return bom
        except Exception:
            pass

    # Fallback: search data directories containing so_no
    for base in [Path("data"), Path("/app/data")]:
        if base.exists() and base.is_dir():
            for d in base.iterdir():
                if d.is_dir() and so_no in d.name:
                    bom = find_bom_for_path(str(d / f"{d.name}.json"))
                    if bom:
                        return bom
    return None


def parse_bom_csv(csv_path: Path) -> tuple[list[str], list[dict[str, Any]], dict[str, Any]]:
    with csv_path.open(encoding="utf-8-sig", newline="") as f:
        reader = csv.DictReader(f)
        columns = [c.strip() for c in (reader.fieldnames or []) if c and c.strip()]
        rows = []
        level_counts: dict[str, int] = {}
        for r in reader:
            clean_row = {k.strip(): (v.strip() if v else "") for k, v in r.items() if k and k.strip()}
            rows.append(clean_row)
            lvl = clean_row.get("Level", "")
            if lvl:
                level_counts[lvl] = level_counts.get(lvl, 0) + 1
        summary = {
            "total_items": len(rows),
            "level_counts": level_counts,
        }
        return columns, rows, summary


@router.get("/pdf")
def get_pdf_by_path(source_path: str = Query(..., description="Path to source JSON file")):
    """Serve the original PDF corresponding to a source JSON path."""
    pdf_path = find_pdf_for_path(source_path)
    if not pdf_path or not pdf_path.is_file():
        raise HTTPException(
            status_code=404,
            detail=f"Original PDF not found on disk for: {os.path.basename(source_path)}"
        )

    return FileResponse(
        path=str(pdf_path),
        filename=pdf_path.name,
        content_disposition_type="inline",
        media_type="application/pdf",
        headers={
            "Cache-Control": "public, max-age=3600",
        },
    )


@router.get("/{internal_so_no}/pdf")
def get_pdf_by_internal_so(internal_so_no: str):
    """Serve the original PDF corresponding to an internal SO number or customer SO number."""
    database_url = os.getenv("DATABASE_URL")
    if not database_url:
        raise HTTPException(status_code=500, detail="DATABASE_URL not configured")

    engine = create_engine(database_url, pool_pre_ping=True)
    with engine.connect() as conn:
        row = conn.execute(
            text("SELECT source_file FROM info_sheets WHERE internal_so_no = :so OR customer_so_no = :so"),
            {"so": internal_so_no},
        ).mappings().first()

    if not row:
        raise HTTPException(status_code=404, detail=f"No info sheet found for SO: {internal_so_no}")

    source_file = row["source_file"]
    pdf_path = find_pdf_for_path(source_file)
    if not pdf_path or not pdf_path.is_file():
        raise HTTPException(
            status_code=404,
            detail=f"Original PDF not found on disk for SO: {internal_so_no}"
        )

    return FileResponse(
        path=str(pdf_path),
        filename=pdf_path.name,
        content_disposition_type="inline",
        media_type="application/pdf",
        headers={
            "Cache-Control": "public, max-age=3600",
        },
    )


@router.get("/{so_no}/bom/status")
def get_bom_status(so_no: str):
    """Check whether a BOM CSV exists on disk for a given sales order."""
    bom_path = resolve_bom_for_so(so_no)
    if not bom_path or not bom_path.is_file():
        return {"has_bom": False, "so_no": so_no, "filename": None}

    return {
        "has_bom": True,
        "so_no": so_no,
        "filename": bom_path.name,
    }


@router.get("/{so_no}/bom")
def get_bom_data(so_no: str):
    """Serve parsed Bill of Materials JSON for a given sales order."""
    bom_path = resolve_bom_for_so(so_no)
    if not bom_path or not bom_path.is_file():
        raise HTTPException(
            status_code=404,
            detail=f"Bill of Materials CSV not found on disk for SO: {so_no}"
        )

    try:
        columns, rows, summary = parse_bom_csv(bom_path)
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to parse BOM CSV: {e}")

    return {
        "exists": True,
        "so_no": so_no,
        "filename": bom_path.name,
        "total_rows": len(rows),
        "columns": columns,
        "rows": rows,
        "summary": summary,
    }


@router.get("/{so_no}/bom/download")
def download_bom_csv(so_no: str):
    """Download the raw Bill of Materials CSV file for a given sales order."""
    bom_path = resolve_bom_for_so(so_no)
    if not bom_path or not bom_path.is_file():
        raise HTTPException(
            status_code=404,
            detail=f"Bill of Materials CSV not found on disk for SO: {so_no}"
        )

    return FileResponse(
        path=str(bom_path),
        filename=bom_path.name,
        media_type="text/csv",
        headers={
            "Content-Disposition": f'attachment; filename="{bom_path.name}"',
            "Cache-Control": "public, max-age=3600",
        },
    )
