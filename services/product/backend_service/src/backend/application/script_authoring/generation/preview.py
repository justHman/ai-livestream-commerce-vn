"""Deterministic no-LLM generation preview (tasks 7.2/7.3/7.4).

Pure arithmetic over ``GenerationBudgetCalibration`` — no LLM, no network,
no filesystem, and no Change A speech-duration estimation (that estimator
runs only on text that exists). For one product the planned semantic calls
are ``1 (planning) + K (segments)`` (design Decision 7 call budget); a batch
preview sums the per-product estimates (Decision 11).

Preview is the cost/call contract the Workbench shows before a user spends
tokens; it excludes explicit future human actions (Fix, Regenerate).
"""

from __future__ import annotations

from pydantic import BaseModel, Field

from backend.application.script_authoring.generation.calibration import (
    GenerationBudgetCalibration,
)

# Normal semantic workflow: exactly one planning call per product, then K
# sequential segment calls (design Decision 7).
PLANNING_CALLS_PER_PRODUCT: int = 1


class ProductGenerationPreview(BaseModel):
    """Per-product planned segment count and semantic call budget.

    Attributes:
        product_id: Stable product identifier.
        target_duration_s: Requested spoken duration for this product.
        planned_segment_count: Backend-fixed K for the target duration.
        estimated_semantic_calls: ``1 + K`` PLANNED semantic calls.
        maximum_semantic_calls: ``1 + K * segment_max_attempts`` MAXIMUM calls
            for one Generate operation (reviewer R9.2): the backend-owned
            bound for bounded in-place Segment Repair, never model-controlled.
    """

    product_id: str = Field(min_length=1)
    target_duration_s: float = Field(gt=0)
    planned_segment_count: int = Field(ge=0)
    estimated_semantic_calls: int = Field(ge=0)
    maximum_semantic_calls: int = Field(ge=0)


class BatchGenerationPreview(BaseModel):
    """Aggregate preview across selected products (task 7.3).

    Attributes:
        products: Per-product preview rows in deterministic input order.
        estimated_semantic_calls_total: Sum of per-product planned estimates.
        maximum_semantic_calls_total: Sum of per-product maximum bounds.
    """

    products: list[ProductGenerationPreview] = Field(default_factory=list)
    estimated_semantic_calls_total: int = Field(default=0)
    maximum_semantic_calls_total: int = Field(default=0)


def preview_product(
    product_id: str,
    target_duration_s: float,
    calibration: GenerationBudgetCalibration,
    *,
    segment_max_attempts: int = 3,
) -> ProductGenerationPreview:
    """Plan K and the ``1 + K``/``1 + K*N`` semantic-call budget for one product.

    ``segment_max_attempts`` is the backend-owned TOTAL semantic attempts per
    segment (N includes the initial generation), so the maximum Generate call
    bound is deterministic and previewable (reviewer R9.2). Raises
    ``GenerationBudgetError`` for out-of-limit targets so the API can reject
    invalid previews predictably. Zero LLM calls.
    """
    k = calibration.segment_count_for(target_duration_s)
    return ProductGenerationPreview(
        product_id=product_id,
        target_duration_s=target_duration_s,
        planned_segment_count=k,
        estimated_semantic_calls=PLANNING_CALLS_PER_PRODUCT + k,
        maximum_semantic_calls=PLANNING_CALLS_PER_PRODUCT + k * max(1, segment_max_attempts),
    )


def preview_batch(
    targets: list[tuple[str, float]],
    calibration: GenerationBudgetCalibration,
    *,
    segment_max_attempts: int = 3,
) -> BatchGenerationPreview:
    """Aggregate per-product previews and the total call budgets (task 7.3).

    ``targets`` is an ordered ``(product_id, target_duration_s)`` list; the
    preview rows keep that order deterministically.
    """
    products = [
        preview_product(
            product_id, duration, calibration, segment_max_attempts=segment_max_attempts
        )
        for product_id, duration in targets
    ]
    return BatchGenerationPreview(
        products=products,
        estimated_semantic_calls_total=sum(p.estimated_semantic_calls for p in products),
        maximum_semantic_calls_total=sum(p.maximum_semantic_calls for p in products),
    )
