"""Pre-generation model-output budget calibration (tasks 7.1/7.2/7.9).

``GenerationBudgetCalibration`` converts a provider's max output-token limit
into a conservative safe amount of segment work BEFORE any prose exists. It
is authoring cost/call planning only — it is NOT the Change A
``SpeechDurationEstimator`` (which estimates how long existing text will
take to speak). This module never imports the estimator or anything from
``text_chunker``; see ``change_a_contract`` for the boundary.

K formula (design Decision 7):

    safe_output_tokens = model_max_output_tokens * output_safety_factor
    safe_segment_duration_s = safe_output_tokens / observed_output_tokens_per_second
    K = ceil(target_duration_s / safe_segment_duration_s), clamped to K bounds

The first implementation uses conservative configuration until empirical
model-output calibration exists.
"""

from __future__ import annotations

import math

from pydantic import BaseModel, Field, model_validator


class GenerationBudgetError(ValueError):
    """Deterministic rejection for an out-of-bounds preview target."""


class GenerationBudgetCalibration(BaseModel):
    """Conservative model-output calibration for generation cost planning.

    Attributes:
        model_max_output_tokens: Provider max output tokens per call.
        output_safety_factor: Conservative fraction of the max output budget.
        observed_output_tokens_per_second: Observed rate at which model output
            tokens map to seconds of script content (statistic, not a speech
            estimator; conservative until empirically calibrated).
        min_target_duration_s: Lower bound of an acceptable target duration.
        max_target_duration_s: Upper bound of an acceptable target duration.
        min_segment_count: Lower clamp for the backend-fixed segment count K.
        max_segment_count: Upper clamp for the backend-fixed segment count K.
    """

    model_max_output_tokens: int = Field(default=4096, gt=0)
    output_safety_factor: float = Field(default=0.5, gt=0.0, le=1.0)
    observed_output_tokens_per_second: float = Field(default=8.0, gt=0.0)
    min_target_duration_s: float = Field(default=600.0, ge=1.0)
    max_target_duration_s: float = Field(default=3600.0, ge=1.0)
    min_segment_count: int = Field(default=1, ge=1)
    max_segment_count: int = Field(default=30, ge=1)

    @model_validator(mode="after")
    def _check_limits(self) -> "GenerationBudgetCalibration":
        if self.min_target_duration_s > self.max_target_duration_s:
            raise ValueError(
                "min_target_duration_s must be <= max_target_duration_s"
            )
        if self.min_segment_count > self.max_segment_count:
            raise ValueError("min_segment_count must be <= max_segment_count")
        return self

    def safe_output_tokens(self) -> int:
        """Conservative per-call output budget: max tokens * safety factor."""
        return math.floor(self.model_max_output_tokens * self.output_safety_factor)

    def safe_segment_duration_s(self) -> float:
        """Seconds of script a safe output-token budget can fill.

        Uses the observed model-output rate — the calibrated duration for
        ``safe_output_tokens``. Not a speech-duration estimate.
        """
        return self.safe_output_tokens() / self.observed_output_tokens_per_second

    def segment_count_for(self, target_duration_s: float) -> int:
        """Backend-fixed segment count K for a target duration (Decision 7).

        Rejects non-finite/non-positive/out-of-limit targets deterministically
        (``GenerationBudgetError``) and clamps K to the configured segment
        count bounds.
        """
        if not math.isfinite(target_duration_s) or target_duration_s <= 0:
            raise GenerationBudgetError(
                f"target_duration_s must be a finite positive number, "
                f"got {target_duration_s!r}"
            )
        if not self.min_target_duration_s <= target_duration_s <= self.max_target_duration_s:
            raise GenerationBudgetError(
                f"target_duration_s {target_duration_s:g}s outside configured bounds "
                f"[{self.min_target_duration_s:g}, {self.max_target_duration_s:g}]"
            )
        k = math.ceil(target_duration_s / self.safe_segment_duration_s())
        return min(max(k, self.min_segment_count), self.max_segment_count)
