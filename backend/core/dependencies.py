import os
import threading
from dataclasses import dataclass

from fastapi import Request, FastAPI

from search.semantic_search import get_collection, load_embedder

_extractor_lock = threading.Lock()


@dataclass
class Resources:
    embedder: object
    collection: object


def load_resources() -> Resources:
    return Resources(
        embedder=load_embedder(os.getenv("EMBED_DEVICE", "cpu")),
        collection=get_collection(),
    )


def get_resources(request: Request) -> Resources:
    return request.app.state.resources


def get_or_create_lift_extractor(app: FastAPI):
    """Lazy thread-safe singleton for Datalab Lift VLM extractor."""
    if hasattr(app.state, "lift_extractor") and app.state.lift_extractor is not None:
        return app.state.lift_extractor

    with _extractor_lock:
        if hasattr(app.state, "lift_extractor") and app.state.lift_extractor is not None:
            return app.state.lift_extractor

        from scripts.pdf_to_json_lift import LiftExtractor
        extractor = LiftExtractor(quantization="4bit")
        app.state.lift_extractor = extractor
        return extractor
