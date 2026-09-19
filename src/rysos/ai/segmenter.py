"""Deterministic clause segmentation for multi-item Telegram captures.

the owner often reports several unrelated facts in one message, using ';' as an explicit
divider ("proposta enviada; paguei a Nimbus Cloud; contratei o Wagner..."), but also runs distinct
action items together across sentence boundaries with no ';' at all. Splitting on ';'
alone silently fuses those (2026-09-16 incident: a long report became one "esquizofrênica"
note). This is a pre-classification step, not a content guardrail — CAPTURE_NOTE
extraction (ai/gemini.py) still does structure-free LLM copy on each resulting clause;
segmentation only decides how many notes come out of one message.
"""

import re

# Below this a clause is almost always a continuation, not a standalone fact (e.g. "no
# valor de R$ 5 mil" trailing after a semicolon) — merge it into the previous clause
# instead of spawning a near-empty note.
_MIN_CLAUSE_LEN = 20

# A sentence boundary: a '.' not between digits (decimals, "R$ 16.000") and not right
# after a common title abbreviation, followed by whitespace and a capital letter (i.e.
# the start of a new statement, not a mid-sentence abbreviation like "Av. Paulista").
_SENTENCE_BOUNDARY_RE = re.compile(
    r"(?<!\d)(?<!\bSr)(?<!\bSra)(?<!\bDr)(?<!\bDra)(?<!\bSrs)\.\s+(?=[A-ZÀ-Ú])"
)


def segment_capture_message(text: str) -> list[str]:
    """Splits a capture message into standalone clauses on ';' and sentence boundaries.

    Sentence-boundary splitting only kicks in once the message already contains a ';' —
    that's the owner's own explicit signal that this is a multi-item report. Without it, an
    ordinary multi-sentence capture ("novo projeto com a Beta, prazo outubro. Stakeholder
    é o Carlos.") stays one note; unguarded sentence-splitting would fragment routine
    captures into confirmation spam. A ';' is that explicit divisor, so every
    ';'-delimited chunk always yields at least one clause of its own, however short —
    only a further sentence-boundary split *within* one such chunk gets merged back if
    it's too short to stand alone. Returns a single-element list (the stripped input)
    when nothing splits, so ordinary one-item captures are unaffected.
    """
    if ";" not in text:
        stripped = text.strip()
        return [stripped] if stripped else []

    chunks = [chunk.strip() for chunk in text.split(";") if chunk.strip()]
    if not chunks:
        return [text.strip()] if text.strip() else []

    pieces: list[str] = []
    for chunk in chunks:
        sub = [p.strip() for p in _SENTENCE_BOUNDARY_RE.split(chunk) if p.strip()]
        if not sub:
            continue
        merged = [sub[0]]
        for p in sub[1:]:
            if len(p) < _MIN_CLAUSE_LEN:
                merged[-1] = f"{merged[-1]} {p}"
            else:
                merged.append(p)
        pieces.extend(merged)
    return pieces
