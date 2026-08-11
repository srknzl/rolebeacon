from __future__ import annotations

from pathlib import Path
from typing import Any, Protocol

from .domain import CollectionBatch, EligibilityResult, ScoreResult


class SourceAdapter(Protocol):
    async def collect(self, since: Any, cursor: str = "") -> CollectionBatch: ...


class LlmProvider(Protocol):
    async def available(self) -> bool: ...

    async def score(
        self,
        job: dict[str, Any],
        eligibility: EligibilityResult,
        search_profile: dict[str, Any],
        candidate_profile: dict[str, Any],
    ) -> ScoreResult: ...


class ResumeRenderer(Protocol):
    async def render(
        self,
        *,
        profile: dict[str, Any],
        job: dict[str, Any],
        output_dir: Path,
    ) -> Path: ...


class NotificationProvider(Protocol):
    async def send(self, *, subject: str, body: str, url: str = "") -> None: ...
