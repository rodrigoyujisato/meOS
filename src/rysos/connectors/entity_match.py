"""Robust identity matching for transcript-extracted entities.

Pure module — no vault I/O, no DB. `match_against` takes an in-memory list of
candidate dicts (`{name|title, path, organization, role, email}`) so the whole
thing is exercised by a truth table (`tests/test_entity_matcher.py`).

Two requirements pull apart and both must hold:

  * idiomatic variation — "the owner" / "Rafaelão" / "Moreira" / "R. Moreira" all resolve
    to the vault note "Rafael Moreira";
  * homonym safety — a bare first name ("Nilton") never auto-links on its own;
    only a surname-position token, a full multi-token match, or a composite key
    (org / role / email) is allowed to auto-link.

Design notes:
  - `canonical_tokens` folds PT-BR augmentative/diminutive morphology to a shared
    stem: `carlos`/`carlão` -> `carl`.
  - person matching is fuzzy (spelling variation is the point); project matching
    is strict below an exact title match (projects are the spine — err to review).
"""

from __future__ import annotations

import difflib
import re
import unicodedata
from typing import Any

_HONORIFICS = frozenset({
    "dr", "dra", "sr", "sra", "srta", "prof", "profa", "exmo", "exma", "me",
})

# suffixes tried longest-first; each requires >=3 chars of stem to remain
_AUG_SUFFIXES = (
    "zinhos", "zinhas", "zinho", "zinha", "inhos", "inhas", "inho", "inha",
    "zoes", "zao", "zada", "oes", "ao",
)

_TIER_RANK = {"none": 0, "weak": 1, "strong": 2, "exact": 3}


def _strip_accents(text: str) -> str:
    return unicodedata.normalize("NFKD", text or "").encode("ascii", "ignore").decode("ascii")


def _norm(text: str) -> str:
    """Accent/case/punctuation-insensitive whole-string key (mirrors
    VaultManager._normalize_for_match closely enough for matching)."""
    t = _strip_accents(text).lower()
    t = re.sub(r"[^a-z0-9\s]", " ", t)
    return re.sub(r"\s+", " ", t).strip()


def _fold_token(tok: str) -> str:
    """Canonical stem of a single name token."""
    t = re.sub(r"[^a-z]", "", _strip_accents(tok).lower())
    if len(t) <= 3:
        return t
    for suf in _AUG_SUFFIXES:
        if t.endswith(suf) and len(t) - len(suf) >= 3:
            t = t[: -len(suf)]
            break
    if len(t) > 4:
        t = re.sub(r"(os|es|s)$", "", t)
    if len(t) > 3:
        t = re.sub(r"[aeiou]$", "", t)
    return t


def canonical_tokens(name: str) -> tuple[str, ...]:
    """Ordered, honorific-stripped, morphology-folded name tokens."""
    words = [w for w in re.split(r"[\s.]+", name or "") if w]
    out: list[str] = []
    for w in words:
        bare = re.sub(r"[^a-z]", "", _strip_accents(w).lower())
        if not bare or bare in _HONORIFICS:
            continue
        folded = _fold_token(w)
        if folded:
            out.append(folded)
    return tuple(out)


def _tok_eq(a: str, b: str, *, fuzzy: bool) -> bool:
    if a == b:
        return True
    if (len(a) == 1) ^ (len(b) == 1) and a[:1] == b[:1]:
        return True  # initial vs full given name: "r" ~ "rodrig"
    if len(a) >= 3 and len(b) >= 3 and (a.startswith(b) or b.startswith(a)):
        return True
    if fuzzy and max(len(a), len(b)) >= 4:
        return difflib.SequenceMatcher(None, a, b).ratio() >= 0.86
    return False


def same_token(a: str, b: str) -> bool:
    """Do two already-folded stems denote the same given/family name? Fuzzy."""
    return _tok_eq(a, b, fuzzy=True)


def identity_key(kind: str, surface: str, *, org: str = "") -> str:
    """Cross-transcript grouping key — a pure function, never stored.

    A lone first-name token is namespaced by the org stem so two different
    "Nilton"s from different companies don't collapse into one review group.
    """
    toks = canonical_tokens(surface)
    if not toks:
        return f"{kind}:"
    base = "|".join(sorted(toks))
    if len(toks) == 1:
        org_stem = "".join(canonical_tokens(org))
        if org_stem:
            return f"{kind}:{base}@{org_stem}"
    return f"{kind}:{base}"


def grouping_key(
    kind: str,
    surface: str,
    *,
    org: str = "",
    top_suggestion: dict[str, Any] | None = None,
    min_score: float = 0.85,
) -> str:
    """Cross-transcript / within-meeting grouping key.

    Same as `identity_key`, except a confident top suggestion (score >=
    `min_score`) *is* the identity — every idiomatic variant that resolves to the
    same vault note groups together ("Moreira" / "the owner" / "Rafaelão" -> the note
    "Rafael Moreira"), even a lone surname token that `identity_key` would otherwise
    org-namespace into separate groups. A surname is a stronger identity signal
    than a bare first name, not a weaker one.
    """
    if top_suggestion:
        try:
            score = float(top_suggestion.get("score", 0) or 0)
        except (TypeError, ValueError):
            score = 0.0
        path = top_suggestion.get("path") or ""
        if score >= min_score and path:
            stem = re.sub(r"\.md$", "", str(path).rsplit("/", 1)[-1])
            return f"{kind}:={stem}"
    return identity_key(kind, surface, org=org)


_OWNERSHIP_RE = re.compile(
    r"\b(vai|ir[aá]|ficou|ficar[aá]|assumiu|assume|envi(ar|ou)|mand(ar|ou)|revis(ar|ou)|"
    r"apresent(ar|ou)|conduziu|conduz|respons[aá]vel|encarregad|decidiu|decide|prop[oôõ]s|"
    r"combinou|cuidar|cuida|trazer|traz)\b",
    re.IGNORECASE,
)


def should_queue_person(surface: str, payload: dict[str, Any], raw_text: str) -> bool:
    """A person mention earns a validation row only if it carries real signal:
    a stated role or org, at least two occurrences in the transcript, or a
    `mentions` sentence that assigns them an action / ownership. One-shot
    role-less names (ASR noise, people named in passing) render in the ata but
    produce no `EntityCandidate`."""
    if (payload.get("role") or "").strip() or (payload.get("org") or "").strip():
        return True
    s = _strip_accents(surface or "").lower().strip()
    if s and raw_text:
        # whole-word count — a substring count matches "Su" inside "assunto", "sua"...
        if len(re.findall(rf"\b{re.escape(s)}\b", _strip_accents(raw_text).lower())) >= 2:
            return True
    if _OWNERSHIP_RE.search(payload.get("mentions") or ""):
        return True
    return False


def _tok_overlap(a: str, b: str) -> bool:
    ta, tb = canonical_tokens(a), canonical_tokens(b)
    return any(_tok_eq(x, y, fuzzy=True) for x in ta for y in tb)


def _composite_hit(cand: dict[str, Any], org: str, role: str, email: str) -> bool:
    if email and cand.get("email") and _norm(email) == _norm(cand["email"]):
        return True
    if org and cand.get("organization") and _tok_overlap(org, cand["organization"]):
        return True
    if role and cand.get("role") and _tok_overlap(role, cand["role"]):
        return True
    return False


def _score(s_tok: tuple[str, ...], c_tok: tuple[str, ...], *, fuzzy: bool) -> tuple[str, float]:
    if not s_tok or not c_tok:
        return "none", 0.0
    matched = sum(1 for st in s_tok if any(_tok_eq(st, ct, fuzzy=fuzzy) for ct in c_tok))
    if matched == 0:
        ratio = difflib.SequenceMatcher(None, "".join(s_tok), "".join(c_tok)).ratio()
        if fuzzy and ratio >= 0.85:
            return "weak", ratio
        return "none", ratio
    subset = matched == len(s_tok)
    if subset and len(s_tok) >= 2:
        return "strong", 0.95
    if subset and len(s_tok) == 1:
        st = s_tok[0]
        pos = next((i for i, ct in enumerate(c_tok) if _tok_eq(st, ct, fuzzy=fuzzy)), 0)
        return ("strong", 0.9) if pos >= 1 else ("weak", 0.75)
    return "weak", min(0.85, 0.5 + 0.15 * matched)


def _sugg(cand: dict[str, Any], name_key: str, score: float) -> dict[str, Any]:
    return {name_key: cand[name_key], "path": cand["path"], "score": round(float(score), 3)}


def _result(decision: str, target: str | None, score: float, why: str,
            suggestions: list[dict[str, Any]]) -> dict[str, Any]:
    return {
        "decision": decision, "target": target, "score": round(float(score), 3),
        "why": why, "suggested_matches": suggestions,
    }


def match_against(
    surface: str,
    candidates: list[dict[str, Any]],
    *,
    kind: str = "person",
    org: str = "",
    role: str = "",
    email: str = "",
) -> dict[str, Any]:
    """Resolve `surface` against `candidates` (vault notes of the same kind).

    Returns {decision: 'auto'|'review', target: path|None, score, why,
    suggested_matches: [{name|title, path, score}]}.
    """
    name_key = "title" if kind == "project" else "name"
    fuzzy = kind != "project"
    s_norm = _norm(surface)
    s_tok = canonical_tokens(surface)

    for c in candidates:
        if _norm(c[name_key]) == s_norm and s_norm:
            return _result("auto", c["path"], 1.0, "correspondência exata",
                           [_sugg(c, name_key, 1.0)])

    scored: list[tuple[str, float, dict[str, Any]]] = []
    for c in candidates:
        tier, sc = _score(s_tok, canonical_tokens(c[name_key]), fuzzy=fuzzy)
        if tier != "none":
            scored.append((tier, sc, c))

    if not scored:
        return _result("review", None, 0.0, "sem correspondência no vault", [])

    scored.sort(key=lambda t: (_TIER_RANK[t[0]], t[1]), reverse=True)
    suggestions = [_sugg(c, name_key, sc) for (_t, sc, c) in scored[:3]]
    strong = [x for x in scored if x[0] == "strong"]

    if len(strong) == 1:
        _t, sc, c = strong[0]
        return _result("auto", c["path"], sc, "token forte único", suggestions)

    if kind == "person":
        pool = strong or [x for x in scored if x[0] == "weak"]
        if len(pool) == 1 and _composite_hit(pool[0][2], org, role, email):
            _t, sc, c = pool[0]
            return _result("auto", c["path"], max(sc, 0.9),
                           "chave composta (org/cargo/email)", suggestions)

    return _result("review", None, scored[0][1], "requer validação", suggestions)
