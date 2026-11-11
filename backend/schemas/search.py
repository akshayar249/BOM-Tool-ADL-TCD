from datetime import date

from pydantic import BaseModel, Field, field_validator, model_validator

from search.info_sheet_search import SheetFilter


class FilteredRequest(BaseModel):
    """Result filters shared by /search and /search/document.

    Every filter is optional and they combine with AND. Sent flat in the
    request body (rather than nested) so the two endpoints keep the same
    shape they already had, with three new optional keys.
    """

    material: str | None = Field(
        default=None,
        max_length=256,
        description="Material description; matched case-insensitively as a substring, "
                    "so both 'SLC-16 LM G - FANUC' and 'SLC-16' work.",
    )
    material_id: str | None = Field(
        default=None,
        max_length=128,
        description="Material ID; matched exactly (trimmed, case-insensitive), "
                    "e.g. '40004'.",
    )
    date_from: date | None = Field(
        default=None, description="Earliest required delivery date, inclusive."
    )
    date_to: date | None = Field(
        default=None, description="Latest required delivery date, inclusive."
    )
    exact_match: bool = Field(
        default=False,
        description="Return only instructions that literally contain every content "
                    "word of the query (whole words, order-independent, stopwords "
                    "ignored), instead of semantically similar ones.",
    )

    @field_validator("material", "material_id")
    @classmethod
    def _blank_is_no_filter(cls, value: str | None) -> str | None:
        # The UI sends "" for an untouched text box; treat that as unset
        # rather than as a filter value that matches everything by accident.
        if value is None:
            return None
        return value.strip() or None

    @model_validator(mode="after")
    def _range_is_ordered(self):
        if self.date_from and self.date_to and self.date_from > self.date_to:
            raise ValueError("date_from must be on or before date_to")
        return self

    def to_sheet_filter(self) -> SheetFilter:
        return SheetFilter(
            material=self.material,
            material_id=self.material_id,
            date_from=self.date_from,
            date_to=self.date_to,
        )


# Both endpoints return every sheet that clears the relevance floor -- the UI
# paginates them rather than capping the count, so there is no n_sheets field.
class SearchRequest(FilteredRequest):
    query: str = Field(min_length=1)
    pool: int = Field(default=30, ge=1, le=200)


class DocumentSearchRequest(FilteredRequest):
    instructions: list[str]
    pool: int = Field(default=30, ge=1, le=200)


class InstructionHitOut(BaseModel):
    sl_no: int | None
    text: str
    is_amendment: bool
    semantic_distance: float | None
    lexical_score: float | None
    fused_score: float


class SheetResultOut(BaseModel):
    source_path: str
    header: dict | None
    total_instructions: int | None
    matched: list[InstructionHitOut]


class SearchResponse(BaseModel):
    results: list[SheetResultOut]


class MaterialIdOption(BaseModel):
    id: str
    description: str | None


class MaterialsResponse(BaseModel):
    materials: list[str]
    # Paired with a description so the ID dropdown can label "40004" with the
    # machine it refers to instead of showing a bare code.
    material_ids: list[MaterialIdOption]


class SONumberOption(BaseModel):
    so_no: str
    customer_name: str | None
    material_description: str | None


class SONumbersResponse(BaseModel):
    so_numbers: list[SONumberOption]


class CorpusStatsResponse(BaseModel):
    total_instructions: int
    total_sheets: int
    total_customers: int
    total_models: int
    vector_count: int
    sample_customers: list[str] = []
    sample_models: list[str] = []

