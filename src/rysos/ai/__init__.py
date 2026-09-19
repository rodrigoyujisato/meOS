"""AI module exports."""

from rysos.ai.gemini import GeminiAIClient, gemini_ai
from rysos.ai.prompts import (
    MORNING_BRIEF_SYSTEM_PROMPT,
    EMAIL_ANALYSIS_PROMPT,
    MEETING_PREP_PROMPT,
    DECISION_EXTRACTION_PROMPT,
)

__all__ = [
    "GeminiAIClient",
    "gemini_ai",
    "MORNING_BRIEF_SYSTEM_PROMPT",
    "EMAIL_ANALYSIS_PROMPT",
    "MEETING_PREP_PROMPT",
    "DECISION_EXTRACTION_PROMPT",
]
