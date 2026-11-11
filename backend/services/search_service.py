import os

from search.info_sheet_search import SheetFilter, search_info_sheets

from backend.schemas.search import InstructionHitOut, SheetResultOut


def run_search(
    query: str,
    pool: int,
    embedder,
    collection,
    filters: SheetFilter | None = None,
    exact_match: bool = False,
) -> list[SheetResultOut]:
    # No n_sheets: every relevant sheet is returned and the UI pages through
    # them.
    sheets = search_info_sheets(
        query, embedder, collection,
        pool=pool,
        database_url=os.getenv("DATABASE_URL"),
        filters=filters,
        exact_match=exact_match,
    )
    return [
        SheetResultOut(
            source_path=sheet.source_path,
            header=sheet.header,
            total_instructions=sheet.total_instructions,
            matched=[
                InstructionHitOut(
                    sl_no=hit.sl_no,
                    text=hit.text,
                    is_amendment=hit.is_amendment,
                    semantic_distance=hit.semantic_distance,
                    lexical_score=hit.lexical_score,
                    fused_score=hit.fused_score,
                )
                for hit in sheet.matched
            ],
        )
        for sheet in sheets
    ]
