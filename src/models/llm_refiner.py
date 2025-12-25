from __future__ import annotations

from dataclasses import dataclass
from typing import Optional


@dataclass(frozen=True)
class LlmRefineResult:
    text: str


class LlmRefiner:
    """Optional stub for future text refinement.

    Intended for later: a lightweight local or remote LLM that can refine transcripts
    or add semantic emotion descriptors.
    """

    def __init__(self) -> None:
        self.loaded = False

    def refine(self, transcript: Optional[str]) -> LlmRefineResult:
        return LlmRefineResult(text=transcript or "")
