import os

from fastapi import APIRouter, HTTPException, Request
from sqlalchemy import create_engine, text

from backend.schemas.search import (
    CorpusStatsResponse,
    MaterialIdOption,
    MaterialsResponse,
    SONumberOption,
    SONumbersResponse,
)

router = APIRouter()


@router.get("/materials", response_model=MaterialsResponse)
def list_materials():
    """Distinct material descriptions and material IDs, alphabetically.

    Feeds the two material filter dropdowns. It lists the whole corpus rather
    than only what a given result set happens to contain, so the options are
    there before the first search and don't shift underneath the user as
    results change.
    """
    database_url = os.getenv("DATABASE_URL")
    if not database_url:
        raise HTTPException(status_code=500, detail="DATABASE_URL not configured")

    engine = create_engine(database_url, pool_pre_ping=True)
    with engine.connect() as conn:
        materials = conn.execute(
            text(
                "SELECT DISTINCT material_description FROM info_sheets "
                "WHERE material_description IS NOT NULL "
                "AND btrim(material_description) <> '' "
                "ORDER BY material_description"
            )
        ).scalars().all()

        # An ID maps to one machine in practice, but nothing enforces that,
        # so pick one description per ID rather than emitting duplicate IDs
        # the datalist would render twice.
        id_rows = conn.execute(
            text(
                "SELECT material_id, MIN(material_description) AS description "
                "FROM info_sheets "
                "WHERE material_id IS NOT NULL AND btrim(material_id) <> '' "
                "GROUP BY material_id ORDER BY material_id"
            )
        ).mappings().all()

    return MaterialsResponse(
        materials=list(materials),
        material_ids=[
            MaterialIdOption(id=row["material_id"], description=row["description"])
            for row in id_rows
        ],
    )


@router.get("/so_numbers", response_model=SONumbersResponse)
def list_so_numbers():
    """Distinct Customer SO numbers in the database with customer name and material."""
    database_url = os.getenv("DATABASE_URL")
    if not database_url:
        raise HTTPException(status_code=500, detail="DATABASE_URL not configured")

    engine = create_engine(database_url, pool_pre_ping=True)
    with engine.connect() as conn:
        rows = conn.execute(
            text(
                "SELECT customer_so_no, "
                "MIN(customer_name) AS customer_name, "
                "MIN(material_description) AS material_description "
                "FROM info_sheets "
                "WHERE customer_so_no IS NOT NULL AND btrim(customer_so_no) <> '' "
                "GROUP BY customer_so_no ORDER BY customer_so_no"
            )
        ).mappings().all()

    return SONumbersResponse(
        so_numbers=[
            SONumberOption(
                so_no=row["customer_so_no"],
                customer_name=row["customer_name"],
                material_description=row["material_description"],
            )
            for row in rows
        ]
    )


@router.get("/stats", response_model=CorpusStatsResponse)
@router.get("/corpus_stats", response_model=CorpusStatsResponse)
def get_corpus_stats(request: Request):
    """Corpus-level grounding metrics from PostgreSQL and ChromaDB."""
    database_url = os.getenv("DATABASE_URL")
    if not database_url:
        raise HTTPException(status_code=500, detail="DATABASE_URL not configured")

    engine = create_engine(database_url, pool_pre_ping=True)
    with engine.connect() as conn:
        row = conn.execute(
            text(
                "SELECT "
                "COUNT(*) AS total_sheets, "
                "COALESCE(SUM(special_instruction_count), 0) AS total_instructions, "
                "COUNT(DISTINCT customer_name) FILTER (WHERE customer_name IS NOT NULL AND btrim(customer_name) <> '') AS total_customers, "
                "COUNT(DISTINCT material_description) FILTER (WHERE material_description IS NOT NULL AND btrim(material_description) <> '') AS total_models "
                "FROM info_sheets"
            )
        ).mappings().first()

        sample_customers = conn.execute(
            text(
                "SELECT customer_name FROM info_sheets "
                "WHERE customer_name IS NOT NULL AND btrim(customer_name) <> '' "
                "GROUP BY customer_name ORDER BY count(*) DESC LIMIT 3"
            )
        ).scalars().all()

        sample_models = conn.execute(
            text(
                "SELECT material_description FROM info_sheets "
                "WHERE material_description IS NOT NULL AND btrim(material_description) <> '' "
                "GROUP BY material_description ORDER BY count(*) DESC LIMIT 4"
            )
        ).scalars().all()

    vector_count = 0
    resources = getattr(request.app.state, "resources", None)
    if resources and hasattr(resources, "collection") and resources.collection:
        try:
            vector_count = resources.collection.count()
        except Exception:
            vector_count = int(row["total_instructions"]) if row else 0
    else:
        vector_count = int(row["total_instructions"]) if row else 0

    return CorpusStatsResponse(
        total_instructions=int(row["total_instructions"]) if row else 0,
        total_sheets=int(row["total_sheets"]) if row else 0,
        total_customers=int(row["total_customers"]) if row else 0,
        total_models=int(row["total_models"]) if row else 0,
        vector_count=vector_count,
        sample_customers=list(sample_customers),
        sample_models=list(sample_models),
    )

