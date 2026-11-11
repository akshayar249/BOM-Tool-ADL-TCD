from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from backend.api.auth import router as auth_router
from backend.api.facets import router as facets_router
from backend.api.health import router as health_router
from backend.api.search import router as search_router
from backend.api.so_lookup import router as so_lookup_router
from backend.api.document_search import router as document_search_router
from backend.api.documents import router as documents_router
from backend.auth.store import SESSION_COOKIE, ensure_auth_schema, user_for_token
from backend.core.config import settings
from backend.core.dependencies import load_resources

_PUBLIC_PATHS = {"/health", "/auth/login", "/auth/logout", "/openapi.json", "/docs", "/redoc"}


@asynccontextmanager
async def lifespan(app: FastAPI):
    ensure_auth_schema()
    app.state.resources = load_resources()
    yield


app = FastAPI(lifespan=lifespan)


@app.middleware("http")
async def require_session(request: Request, call_next):
    if request.method == "OPTIONS" or request.url.path in _PUBLIC_PATHS:
        return await call_next(request)
    user = user_for_token(request.cookies.get(SESSION_COOKIE))
    if not user:
        return JSONResponse(status_code=401, content={"detail": "Sign in required."})
    request.state.user = user
    return await call_next(request)


_origins = [origin.strip() for origin in settings.cors_origins if origin.strip() and origin.strip() != "*"]
app.add_middleware(
    CORSMiddleware,
    allow_origins=_origins or ["http://localhost:3000"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)
app.include_router(auth_router)
app.include_router(health_router)
app.include_router(search_router)
app.include_router(so_lookup_router)
app.include_router(document_search_router)
app.include_router(facets_router)
app.include_router(documents_router)
