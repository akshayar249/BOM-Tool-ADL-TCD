from fastapi import APIRouter, Depends

from backend.core.dependencies import Resources, get_resources
from backend.schemas.search import SearchRequest, SearchResponse
from backend.services.search_service import run_search

router = APIRouter()


@router.post("/search", response_model=SearchResponse)
def search(request: SearchRequest, resources: Resources = Depends(get_resources)):
    results = run_search(
        request.query, request.pool,
        resources.embedder, resources.collection,
        filters=request.to_sheet_filter(),
        exact_match=request.exact_match,
    )
    return SearchResponse(results=results)
