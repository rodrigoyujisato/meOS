"""Assembles the read-only context an AI reviewer (or the owner, in Telegram) needs to
decide what to do about one `scan_vault` finding — the same material Claude Code read by
hand for every finding in the 2026-09-17 dedup session before proposing a merge: both
notes' full content, who links to each of them (and what alias they used), and, for a
diary-mention candidate, the actual diary sentence(s). Nothing here writes to the vault —
see [[dedup_unitary_approval_required]] and the `rysos.vault.dedup` module docstring on why
every step from here to an applied merge stays behind an explicit approval.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from rysos.vault.dedup import build_backlink_index, find_backlinks


def _to_rel(vault, path: str) -> str:
    """Normalizes a finding's path to vault-relative. `DedupReport` findings for
    people/resources store `a_path`/`b_path` absolute (`list_people`/`list_resources`
    return `str(file)`); everything downstream — the Gemini prompt, the Telegram message,
    `DedupSuggestion.proposed_survivor_path` — should only ever see the vault-relative
    form, so this is normalized once, here, rather than by every consumer."""
    p = Path(path)
    return str(p.relative_to(vault.vault_path)) if p.is_absolute() else path


def _read_rel(vault, rel_path: str) -> str:
    return vault.read_file(vault.vault_path / rel_path) or ""


def build_finding_context(
    vault, kind: str, finding: dict[str, Any],
    backlink_index: dict[str, list[dict[str, Any]]] | None = None,
) -> dict[str, Any]:
    """`kind` is one of "people" | "resources" | "projects" | "decisions" |
    "diary_candidates" — matching the `DedupReport` field the finding came from.

    Pass `backlink_index` (`build_backlink_index(collect_vault_files(vault)[0])`, built
    once per `/dedup` triage run) to avoid a full vault re-scan per finding.

    Returns a dict shaped for `assess_dedup_finding`'s prompt: never more than what the
    AI needs to reason about *this* finding, so a merge/keep-separate call is grounded in
    real note content and real usage, not guesswork. All paths are normalized vault-relative.
    """
    if kind == "diary_candidates":
        orphan_path = _to_rel(vault, finding["orphan_path"])
        return {
            "kind": kind,
            "orphan_path": orphan_path,
            "orphan_name": finding.get("orphan_name") or Path(orphan_path).stem,
            "orphan_content": _read_rel(vault, orphan_path),
            "mentions": finding["mentions"],
        }

    a_path = _to_rel(vault, finding["a_path"])
    b_path = _to_rel(vault, finding["b_path"])
    return {
        "kind": kind,
        "a_path": a_path,
        "a_name": finding.get("a") or Path(a_path).stem,
        "a_content": _read_rel(vault, a_path),
        "a_backlinks": find_backlinks(vault, a_path, index=backlink_index),
        "b_path": b_path,
        "b_name": finding.get("b") or Path(b_path).stem,
        "b_content": _read_rel(vault, b_path),
        "b_backlinks": find_backlinks(vault, b_path, index=backlink_index),
        "score": finding.get("score"),
    }
