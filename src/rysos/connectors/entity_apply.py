"""Execute a validated transcript-entity resolution against the vault.

Shared by both resolution surfaces:
  - Telegram buttons (`rysos.telegram.entity_review`)
  - Triagem-note parse-back (`rysos.vault.triagem`, Phase 3)

Pure vault side effects + `cand` field mutation; the CALLER owns the DB session and
commit. Runs synchronously — wrap in `asyncio.to_thread` on an async path.
"""

from __future__ import annotations

import json
import logging
import re
import uuid
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Optional

from rysos.db import EntityCandidate
from rysos.vault.manager import VaultManager

logger = logging.getLogger("rysos.connectors.entity_apply")

NEW = "new"
LINK = "link"
SKIP = "skip"

# Bulk macros — one gesture over every still-pending row of a meeting. Shared by the
# consolidated Telegram card (`entgrp:` callbacks) and the Triagem "⚡ Ações em massa"
# block, so both surfaces resolve identically.
BULK_ACCEPT = "accept_suggestions"    # link every person/project with a strong suggestion
BULK_IGNORE = "ignore_unmatched"      # dismiss every person/project with no suggestion
BULK_DECISIONS = "register_decisions"  # promote every decision to a DecisionRecord
BULK_RESOURCES = "create_resources"   # record every resource as new
BULK_MACROS = (BULK_ACCEPT, BULK_IGNORE, BULK_DECISIONS, BULK_RESOURCES)

_BULK_MIN_SCORE = 0.85


def _row_identity_key(row) -> str:
    """Compute the cross-transcript grouping key for an EntityCandidate row.

    A confident top suggestion overrides the computed key so idiomatic variants
    that all point at the same vault note ("Moreira" / "the owner" / "Rafaelão")
    propagate as one group (see `entity_match.grouping_key`)."""
    from rysos.connectors.entity_match import grouping_key
    try:
        org = (json.loads(row.payload_json or "{}") or {}).get("org", "")
    except (ValueError, TypeError):
        org = ""
    try:
        ms = json.loads(row.suggested_matches_json or "[]") or []
    except (ValueError, TypeError):
        ms = []
    return grouping_key(row.kind, row.surface_form or "", org=org,
                        top_suggestion=ms[0] if ms else None, min_score=_BULK_MIN_SCORE)


def plan_sibling_resolutions(cand, status: str, resolved_to, pending_rows):
    """After `cand` resolved to (`status`, `resolved_to`), the same real-world
    person/project may sit unresolved in other transcripts. Return
    [(row, action, target)] applying the equivalent resolution to each sibling
    (same kind + identity key). Review once, applied everywhere.

    `pending_rows` must be every still-pending EntityCandidate the caller wants
    considered — the match is by computed identity key, not the pending filter,
    so the caller passes the rows explicitly (see C14)."""
    if cand.kind not in ("person", "project"):
        return []
    key = _row_identity_key(cand)
    if not key or key.endswith(":"):
        return []
    name_key = "title" if cand.kind == "project" else "name"
    plans = []
    for r in pending_rows:
        if r.id == cand.id or r.kind != cand.kind or getattr(r, "status", "") != "pending":
            continue
        if _row_identity_key(r) != key:
            continue
        if status in ("linked", "confirmed_new") and resolved_to:
            from pathlib import Path
            plans.append((r, LINK, {name_key: Path(resolved_to).stem, "path": resolved_to}))
        elif status == "dismissed":
            plans.append((r, SKIP, None))
    return plans


def expand_bulk(macro: str, rows, *, min_score: float = _BULK_MIN_SCORE):
    """Return [(cand, action, target)] for a bulk macro over `rows` (pending
    EntityCandidate rows). `target` is the chosen match dict for a link, else None.
    Rows the macro does not apply to are simply omitted."""
    plans = []
    for c in rows:
        if getattr(c, "status", "pending") != "pending":
            continue
        if macro == BULK_ACCEPT and c.kind in ("person", "project"):
            ms = json.loads(c.suggested_matches_json or "[]")
            top = ms[0] if ms else None
            if top and float(top.get("score", 0) or 0) >= min_score:
                plans.append((c, LINK, top))
        elif macro == BULK_IGNORE and c.kind in ("person", "project"):
            if not json.loads(c.suggested_matches_json or "[]"):
                plans.append((c, SKIP, None))
        elif macro == BULK_DECISIONS and c.kind == "decision":
            plans.append((c, NEW, None))
        elif macro == BULK_RESOURCES and c.kind == "resource":
            plans.append((c, NEW, None))
    return plans


@dataclass
class Resolution:
    """Outcome of applying a resolution: a human-facing summary line, the
    `EntityCandidate.status` to set, and (decision `new` only) the DecisionRecord
    fields for the caller to insert."""
    summary: str
    status: str  # confirmed_new | linked | dismissed
    decision_row: Optional[dict[str, Any]] = None
    learn: Optional[dict[str, str]] = None  # {"term","resolved_path"} to feed the learning loop


def _match_label(m: dict[str, Any]) -> str:
    return m.get("name") or m.get("title") or "?"


def apply_resolution(
    vault: VaultManager,
    cand: EntityCandidate,
    action: str,
    target: Optional[dict[str, Any]] = None,
    desired_name: Optional[str] = None,
    overwrite: bool = False,
) -> Resolution:
    """`action` ∈ new | link | skip. `target` is the chosen suggested-match dict for
    link. `desired_name` (new-only) overrides the AI's literal transcription
    (`cand.surface_form`) as the created note's title — e.g. Triagem's
    `NOVO =<nome desejado>`. `overwrite` (link-only, person kind) replaces an existing
    non-blank role/organization instead of only filling a blank one — pass True only
    for the hand-typed Triagem `=<nome exato>` resolution, where the user explicitly
    named the target; the single-tap Telegram "vincular" button and the bulk "aceitar
    sugestões fortes" macro both keep the conservative fill-blank-only default."""
    if action == SKIP:
        cand.resolved_to = None
        return Resolution(f"🚫 Ignorado: <b>{cand.surface_form}</b>", "dismissed")
    if action == NEW:
        return _apply_new(vault, cand, desired_name)
    if action == LINK:
        if not target:
            raise ValueError("link resolution needs a target match")
        return _apply_link(vault, cand, target, overwrite=overwrite)
    raise ValueError(f"unknown resolution action: {action!r}")


def _apply_new(vault: VaultManager, cand: EntityCandidate, desired_name: Optional[str] = None) -> Resolution:
    payload = json.loads(cand.payload_json or "{}")
    mp = Path(cand.meeting_note_path)
    name = (desired_name or "").strip() or cand.surface_form

    if cand.kind == "person":
        path = vault.ensure_person_note(  # never clobbers an existing note
            name=name,
            role=payload.get("role", ""),
            organization=payload.get("org", ""),
        )
        vault.patch_transcript_entity(
            mp, cand.transcript_sha,
            kind="person", surface_form=cand.surface_form, link_target=str(path), display_name=name,
        )
        cand.resolved_to = str(path)
        return Resolution(
            f"✅ Pessoa criada e vinculada: <b>{name}</b>", "confirmed_new",
            learn={"term": cand.normalized_form, "resolved_path": str(path)},
        )

    if cand.kind == "project":
        path = vault.ensure_project_note(  # never clobbers an existing project note
            title=name,
            outcome_description=payload.get("summary", ""),
        )
        vault.patch_transcript_entity(
            mp, cand.transcript_sha,
            kind="project", surface_form=cand.surface_form, link_target=str(path), display_name=name,
        )
        cand.resolved_to = str(path)
        return Resolution(f"✅ Projeto criado e vinculado: <b>{name}</b>", "confirmed_new")

    if cand.kind == "decision":
        now = datetime.now()
        # Second-resolution suffix alone collides when a batch resolves several
        # decisions in the same wall-clock second (common: one Triagem note, one
        # tight loop) — that tripped a UNIQUE constraint on decisions.id, which
        # rolled back the whole note's session and caused an infinite retry loop
        # (see 2026-09-14 incident: 140+ duplicate notes over 34h). The random
        # suffix makes same-second collisions astronomically unlikely.
        dec_id = f"DEC-{now.strftime('%Y')}-{now.strftime('%d%H%M%S')[-5:]}{uuid.uuid4().hex[:3]}"
        path = vault.create_decision_note(
            decision_id=dec_id,
            title=name,
            context=cand.context_sentence or "",
            outcome=payload.get("rationale", ""),
            status=payload.get("status", "em_analise"),
            file_stem=f"{dec_id} - {name}",
        )
        cand.resolved_to = dec_id
        # Decisions have no rendered "ata" mention line to upgrade (unlike person/
        # project), so without this the meeting is only reachable FROM the decision,
        # never the other way — see `append_meeting_generated_link`'s docstring.
        vault.append_meeting_generated_link(
            mp, str(path.relative_to(vault.vault_path).with_suffix("").as_posix()), name,
        )
        return Resolution(
            f"✅ Decisão registrada: <b>{dec_id}</b> — {name}", "confirmed_new",
            decision_row={
                "id": dec_id, "title": name,
                "context": cand.context_sentence or None, "note_path": str(path),
            },
        )

    # resource: generic reading/reference note — no rendered "ata" mention line to
    # upgrade to a wikilink here (unlike person/project), so no patch_transcript_entity call.
    # Link it back to the meeting it came from (and, transitively, that meeting's Area) so
    # it stops being an orphan note — this is exactly what the vault/manager.py resource
    # template and the Area MOC's Dataview query were already built to expect but never got.
    source_link, area_link = _resource_backlinks(vault, mp)
    path = vault.ensure_resource_note(  # never mints a second note for the same title
        title=name, summary=cand.context_sentence or "", source=source_link, area_link=area_link,
    )
    cand.resolved_to = str(path)
    # ...and the other direction: the meeting itself gets a link to the resource, so
    # it's reachable from either end (see `append_meeting_generated_link`'s docstring).
    vault.append_meeting_generated_link(
        mp, str(path.relative_to(vault.vault_path).with_suffix("").as_posix()), name,
    )
    return Resolution(f"✅ Recurso criado: <b>{name}</b>", "confirmed_new")


def _resource_backlinks(vault: VaultManager, meeting_note_path: Path) -> tuple[str, str]:
    """(source_link, area_link) for a new resource note, both derived from the meeting note
    it was mentioned in. `source_link` is a vault-relative wikilink (same construction as
    core.py's transcript source_link) unless the meeting note's filename has a literal `[`/`]`
    in it — Obsidian's [[...]] syntax can't target those, so it falls back to plain text rather
    than writing a dead link (mirrors core.py's own non-linkable fallback for that same case).
    `area_link` is copied verbatim from the meeting note's own `area:` frontmatter field, which
    is already a fully-formed "[[02_Areas/...|...]]" string — no re-resolution needed."""
    if not meeting_note_path.exists():
        return "", ""
    if "[" in meeting_note_path.stem or "]" in meeting_note_path.stem:
        source_link = f"Fonte: {meeting_note_path.name}"
    else:
        try:
            source_link = f"[[{meeting_note_path.relative_to(vault.vault_path).as_posix()}]]"
        except ValueError:
            source_link = f"Fonte: {meeting_note_path.name}"
    area_link = ""
    try:
        head = meeting_note_path.read_text(encoding="utf-8")[:1000]
    except (OSError, ValueError):
        head = ""
    m = re.search(r'^area:\s*"?([^"\n]*)"?\s*$', head, re.MULTILINE)
    if m:
        area_link = m.group(1).strip()
    return source_link, area_link


def _apply_link(
    vault: VaultManager, cand: EntityCandidate, match: dict[str, Any], *, overwrite: bool = False,
) -> Resolution:
    target = match.get("path") or ""
    name = _match_label(match)
    learn = None
    if cand.kind == "person":
        payload = json.loads(cand.payload_json or "{}")
        if payload.get("role") or payload.get("org"):
            vault.enrich_person_note(
                name, role=payload.get("role", ""), org=payload.get("org", ""), overwrite=overwrite,
            )
        if target:
            vault.patch_transcript_entity(
                Path(cand.meeting_note_path), cand.transcript_sha,
                kind="person", surface_form=cand.surface_form,
                link_target=target, display_name=cand.surface_form,
            )
            learn = {"term": cand.normalized_form, "resolved_path": target}
    elif cand.kind == "project" and target:
        vault.patch_transcript_entity(
            Path(cand.meeting_note_path), cand.transcript_sha,
            kind="project", surface_form=cand.surface_form,
            link_target=target, display_name=cand.surface_form,
        )
    cand.resolved_to = target or name
    return Resolution(f"🔗 Vinculado: <b>{cand.surface_form}</b> → {name}", "linked", learn=learn)
