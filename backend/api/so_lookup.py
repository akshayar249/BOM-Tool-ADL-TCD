import os

from fastapi import APIRouter, HTTPException
from sqlalchemy import create_engine, text

router = APIRouter()


@router.get("/so/{customer_so_no}")
def get_so(customer_so_no: str):
    """Look up an info sheet by customer SO number and return its full data."""
    database_url = os.getenv("DATABASE_URL")
    if not database_url:
        raise HTTPException(status_code=500, detail="DATABASE_URL not configured")

    engine = create_engine(database_url, pool_pre_ping=True)
    with engine.connect() as conn:
        row = conn.execute(
            text("SELECT extraction_json, source_file FROM info_sheets WHERE customer_so_no = :so"),
            {"so": customer_so_no},
        ).mappings().first()

    if not row:
        raise HTTPException(
            status_code=404,
            detail=f"No info sheet found for customer SO number: {customer_so_no}",
        )

    data = dict(row["extraction_json"])
    data["source_file"] = row["source_file"]
    return data
