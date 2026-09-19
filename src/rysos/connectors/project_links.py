"""Project <-> meeting / decision / resource wikilinks.

Two edges per relation, both written by rysOS so the project note is a real hub:
child -> project (`related_projects` frontmatter on a decision/resource) and
project -> child (a static list under the project note's section). A meeting's own edge
to its project is the ata bullet `patch_transcript_entity` already upgrades to a wikilink
(meeting frontmatter is re-rendered on every calendar sync, so nothing is written there).

Strict rule for a decision/resource (never an invented edge):
  1. its title / context sentence cites a project by full title -> link exactly those;
  2. else the source meeting touched exactly ONE project -> that project;
  3. else the meeting touched several -> ask on Telegram (`ambiguous`), link nothing yet.
A decision/resource whose `related_projects` frontmatter is already filled is LOCKED: that is
the user's word, so no rule and no question applies — the listed projects are only mirrored
into the project notes' sections. Not persisted as a marker: emptying the field hands the
item back to the rules. While the meeting still has a pending project candidate the answer is `defer`: the set of
projects it touched is not final, so rule 2/3 must wait. A meeting -> project edge is a
fact and is always written for every project the meeting links.

Idempotent end to end (every vault write and the `payload_json["project_link"]` marker),
so the periodic job and the backfill script share `sync_project_links`.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any, Awaitable, Callable, Optional

from sqlalchemy import select

from rysos.db import DecisionRecord, EntityCandidate, get_db_session
from rysos.vault.manager import VaultManager

logger = logging.getLogger("rysos.connectors.project_links")

MARK_DONE = "done"      # linked (or answered "nenhum") — never re-evaluated
MARK_ASKED = "asked"    # question card sent — never asked twice
MAX_QUESTIONS_PER_RUN = 5

_PROJECT_LINK_RE = re.compile(r"\[\[01_Projects/([^\]|#]+)")
_UNLINKABLE = re.compile(r"[\[\]|]")  # Obsidian's [[...]] can't target these in a filename


def _norm(text: str) -> str:
    return VaultManager._normalize_for_match(text)


def cited_projects(text: str, projects: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Projects whose FULL normalised title appears in `text` on word boundaries —
    "Transicao Moreira" never fires for "Rafael Moreira"."""
    hay = f" {_norm(text)} "
    return [p for p in projects if (t := _norm(p["title"])) and f" {t} " in hay]


def meeting_projects(
    vault: VaultManager, meeting_path: Path, projects: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Projects a meeting/journal note links to, sorted by title."""
    content = vault.read_file(meeting_path) or ""
    stems = {s.strip() for s in _PROJECT_LINK_RE.findall(content)}
    return sorted((p for p in projects if p["title"] in stems), key=lambda p: p["title"])


@dataclass
class Verdict:
    action: str  # link | ambiguous | defer | none
    projects: list[dict[str, Any]] = field(default_factory=list)


def decide(
    cites: list[dict[str, Any]], meeting_projs: list[dict[str, Any]], meeting_has_pending_projects: bool,
) -> Verdict:
    if cites:
        return Verdict("link", cites)
    if meeting_has_pending_projects:
        return Verdict("defer")
    if len(meeting_projs) == 1:
        return Verdict("link", meeting_projs)
    if len(meeting_projs) > 1:
        return Verdict("ambiguous", meeting_projs)
    return Verdict("none")


def _display(text: str) -> str:
    return re.sub(r"\s+", " ", _UNLINKABLE.sub(" ", text)).strip()


def _rel_target(vault: VaultManager, path: Path) -> str:
    return path.relative_to(vault.vault_path).with_suffix("").as_posix()


def link_item(vault: VaultManager, kind: str, item_path: Path, project: dict[str, Any], title: str) -> bool:
    """Writes both edges between a decision/resource note and a project. Returns True when
    anything changed. The project-side link is skipped for a filename Obsidian cannot link."""
    changed = vault.add_related_project(item_path, project["title"])
    if not _UNLINKABLE.search(item_path.stem):
        changed |= vault.append_project_link(
            Path(project["path"]), kind, _rel_target(vault, item_path), _display(title) or item_path.stem,
        )
    return changed


def sync_meeting_edges(vault: VaultManager, meeting_path: Path, projects: list[dict[str, Any]]) -> int:
    """Lists `meeting_path` under every project it links to. Returns how many notes changed."""
    if _UNLINKABLE.search(meeting_path.stem):
        return 0
    target, changed = _rel_target(vault, meeting_path), 0
    for p in meeting_projects(vault, meeting_path, projects):
        changed += vault.append_project_link(Path(p["path"]), "meeting", target, _display(meeting_path.stem))
    return changed


def payload_of(cand: EntityCandidate) -> dict[str, Any]:
    try:
        data = json.loads(cand.payload_json or "{}")
    except (ValueError, TypeError):
        return {}
    return data if isinstance(data, dict) else {}


def mark(cand: EntityCandidate, value: str) -> None:
    data = payload_of(cand)
    data["project_link"] = value
    cand.payload_json = json.dumps(data, ensure_ascii=False)


async def resolve_item_path(
    session, vault: VaultManager, cand: EntityCandidate, decision_names: Optional[dict[str, str]] = None,
) -> Optional[tuple[Path, str]]:
    """(note path, display title) of the decision/resource a resolved candidate produced."""
    if not cand.resolved_to:
        return None
    if cand.kind == "resource":
        p = Path(cand.resolved_to)
        if not p.is_file():
            p = _renamed_note(vault, "06_Resources", p.stem)
        return (p, p.stem) if p else None
    if cand.kind == "decision":
        names = decision_names if decision_names is not None else await asyncio.to_thread(vault.scan_decision_note_names)
        dec = await session.get(DecisionRecord, cand.resolved_to)
        stem = names.get(cand.resolved_to)
        path = (vault.vault_path / "03_Decisions" / f"{stem}.md") if stem else (Path(dec.note_path) if dec and dec.note_path else None)
        if path is None or not path.is_file():
            # `resolved_to` can hold the note's file stem instead of a decision id
            path = _renamed_note(vault, "03_Decisions", Path(cand.resolved_to).stem)
        if path is None:
            return None
        return path, (dec.title if dec else cand.surface_form)
    return None


def _renamed_note(vault: VaultManager, folder: str, stem: str) -> Optional[Path]:
    """The single live note under `folder` whose normalised stem equals `stem`'s — a note
    renamed after the candidate row was written ("Design System" vs "Design_System", "/" vs
    "-"). None when there is no match or more than one (never guess between two notes).
    The archive is never searched."""
    key = _norm(stem)
    hits = [p for p in (vault.vault_path / folder).rglob("*.md") if _norm(p.stem) == key]
    return hits[0] if len(hits) == 1 else None


@dataclass
class SyncReport:
    meeting_edges: int = 0
    linked: list[tuple[str, list[str]]] = field(default_factory=list)      # (item title, project titles)
    ambiguous: list[tuple[str, list[str]]] = field(default_factory=list)   # (item title, meeting's projects)
    deferred: int = 0
    locked: int = 0            # items whose related_projects the user filled: mirrored, never decided
    skipped_missing: int = 0
    asked: int = 0


AskFn = Callable[[EntityCandidate, str, Path, list[dict[str, Any]]], Awaitable[bool]]


async def sync_project_links(
    vault: VaultManager, *, since_days: Optional[int] = 7, apply: bool = True, ask: Optional[AskFn] = None,
    ask_within_days: Optional[int] = None,
) -> SyncReport:
    """Reconciles every project edge for meetings/candidates newer than `since_days`
    (None = all history). `apply=False` reports without writing anything. `ask` is called
    for each ambiguous item (bounded by MAX_QUESTIONS_PER_RUN) and must return True once the
    question is delivered; without it ambiguous items are only reported. `ask_within_days`
    narrows who may be asked to items resolved that recently (default: the whole window), so
    a wide linking window never turns old history into a stream of questions."""
    report = SyncReport()
    cutoff = (datetime.now() - timedelta(days=since_days)) if since_days is not None else None
    ask_cutoff = (datetime.now() - timedelta(days=ask_within_days)) if ask_within_days is not None else None
    projects = await asyncio.to_thread(vault.list_projects)
    if not projects:
        return report

    def _recent_meetings() -> list[Path]:
        out = []
        for p in sorted((vault.vault_path / "04_Meetings").glob("*.md")):
            m = re.match(r"(\d{4}-\d{2}-\d{2})", p.name)
            try:
                d = date.fromisoformat(m.group(1)) if m else None
            except ValueError:
                d = None
            if cutoff is None or d is None or d >= cutoff.date():
                out.append(p)
        return out

    for mp in await asyncio.to_thread(_recent_meetings):
        if apply:
            report.meeting_edges += await asyncio.to_thread(sync_meeting_edges, vault, mp, projects)

    decision_names = await asyncio.to_thread(vault.scan_decision_note_names)
    async with get_db_session() as session:
        pending_meetings = {
            r for (r,) in (await session.execute(
                select(EntityCandidate.meeting_note_path).where(
                    EntityCandidate.kind == "project", EntityCandidate.status == "pending",
                )
            )).all()
        }
        q = select(EntityCandidate).where(
            EntityCandidate.kind.in_(("decision", "resource")),
            EntityCandidate.status.in_(("confirmed_new", "linked")),
            EntityCandidate.resolved_to.is_not(None),
        ).order_by(EntityCandidate.id)
        if cutoff is not None:
            q = q.where(EntityCandidate.resolved_at >= cutoff)
        rows = (await session.execute(q)).scalars().all()

        for cand in rows:
            if payload_of(cand).get("project_link") in (MARK_DONE, MARK_ASKED):
                continue
            meeting = Path(cand.meeting_note_path)
            found = await resolve_item_path(session, vault, cand, decision_names)
            if found is None or not meeting.is_file():
                report.skipped_missing += 1
                continue
            item_path, title = found
            stems = await asyncio.to_thread(vault.related_project_stems, item_path)
            if stems:
                report.locked += 1
                if apply:
                    for p in (p for p in projects if p["title"] in stems):
                        await asyncio.to_thread(link_item, vault, cand.kind, item_path, p, title)
                continue
            text = f"{title} {cand.surface_form} {cand.context_sentence or ''}"
            verdict = decide(
                cited_projects(text, projects),
                await asyncio.to_thread(meeting_projects, vault, meeting, projects),
                cand.meeting_note_path in pending_meetings,
            )
            names = [p["title"] for p in verdict.projects]
            if verdict.action == "link":
                report.linked.append((title, names))
                if apply:
                    for p in verdict.projects:
                        await asyncio.to_thread(link_item, vault, cand.kind, item_path, p, title)
                    mark(cand, MARK_DONE)
            elif verdict.action == "ambiguous":
                report.ambiguous.append((title, names))
                fresh = ask_cutoff is None or (cand.resolved_at is not None and cand.resolved_at >= ask_cutoff)
                if apply and ask and fresh and report.asked < MAX_QUESTIONS_PER_RUN:
                    if await ask(cand, title, meeting, verdict.projects):
                        mark(cand, MARK_ASKED)
                        report.asked += 1
            elif verdict.action == "defer":
                report.deferred += 1
        if apply:
            await session.commit()
    return report
