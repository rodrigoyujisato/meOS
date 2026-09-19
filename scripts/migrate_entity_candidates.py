"""One-off: migrate pre-existing pending EntityCandidate rows to the Phase B/C/D model.

  .venv/bin/python scripts/migrate_entity_candidates.py            # dry-run (default)
  .venv/bin/python scripts/migrate_entity_candidates.py --apply    # write

Dry-run prints, without touching anything:
  - which person candidates the salience filter now drops,
  - which candidates get refreshed suggested_matches against the current vault,
  - a unified diff of every Triagem note that would change.

Cross-transcript duplicates are NOT collapsed here — Phase D propagation resolves
them the first time the user acts on one. Archived transcript text is trusted
only when the file's bytes still hash to its ProcessedTranscript row.
"""

from __future__ import annotations

import argparse
import asyncio
import difflib
import hashlib
import json
import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from sqlalchemy import select  # noqa: E402

from rysos.connectors.entity_match import identity_key, should_queue_person  # noqa: E402
from rysos.connectors.entity_resolver import CANDIDATE, ResolvedEntity  # noqa: E402
from rysos.connectors.transcripts import _strip_vtt_srt  # noqa: E402
from rysos.db import EntityCandidate, ProcessedTranscript, get_db_session  # noqa: E402
from rysos.vault.manager import VaultManager  # noqa: E402
from rysos.vault.triagem import (  # noqa: E402
    _KIND_HEADERS, _bulk_block_lines, _candidate_block, _dedupe_by_identity,
    _triagem_marker, _TRIAGEM_SUBDIR,
)

REVIEWABLE = ("person", "project", "decision", "resource")
_SECTION_STARTS = ("## ⚡ Ações em massa", "## 👤", "## 🚀", "## ⚖️", "## 📄", "_Nenhuma entidade")


def _payload(c: EntityCandidate) -> dict:
    try:
        return json.loads(c.payload_json or "{}") or {}
    except (ValueError, TypeError):
        return {}


def _to_resolved(c: EntityCandidate) -> ResolvedEntity:
    p = _payload(c)
    try:
        sug = json.loads(c.suggested_matches_json or "[]") or []
    except (ValueError, TypeError):
        sug = []
    return ResolvedEntity(
        c.kind, c.surface_form, c.normalized_form, c.context_sentence or "",
        p, CANDIDATE, None, sug,
        identity_key(c.kind, c.surface_form, org=p.get("org", "")),
    )


def _archived_text_by_sha(vault: VaultManager, processed) -> dict[str, str]:
    proc_dir = vault.vault_path / "07_Inbox_Agent" / "Transcricoes" / "Processado"
    out: dict[str, str] = {}
    for pt in processed:
        f = proc_dir / (pt.source_filename or "")
        if not f.exists():
            continue
        raw = f.read_bytes()
        if hashlib.sha256(raw).hexdigest() != pt.sha256:
            continue  # re-drop renamed / drifted — do not trust
        try:
            text = raw.decode("utf-8")
        except UnicodeDecodeError:
            text = raw.decode("latin-1", errors="replace")
        out[pt.sha256] = _strip_vtt_srt(text)
    return out


def _note_for_sha(base: Path, sha: str, vault: VaultManager) -> Path | None:
    marker = _triagem_marker(sha)
    for md in sorted(base.glob("*.md")):
        if marker in (vault.read_file(md) or ""):
            return md
    return None


def _render(old_text: str, sha: str, rows: list[EntityCandidate]) -> str:
    lines = old_text.splitlines()
    cut = len(lines)
    for i, ln in enumerate(lines):
        if any(ln.startswith(h) for h in _SECTION_STARTS):
            cut = i
            break
    head = lines[:cut]
    while head and not head[-1].strip():
        head.pop()
    if not rows:
        head = ["status: resolved" if ln.strip() == "status: pending" else ln for ln in head]

    body: list[str] = []
    if rows:
        body += _bulk_block_lines()
        for kind, header in _KIND_HEADERS:
            grp = [_to_resolved(r) for r in rows if r.kind == kind]
            if not grp:
                continue
            body.append(header)
            for r in _dedupe_by_identity(grp):
                body += _candidate_block(r)
            body.append("")
    else:
        body.append("_Nenhuma entidade pendente após a migração._")

    return "\n".join(head + [""] + body).rstrip() + "\n"


async def run(apply: bool) -> None:
    vault = VaultManager()
    people_idx = vault.list_people()
    projects_idx = vault.list_projects()

    async with get_db_session() as session:
        cands = (await session.execute(
            select(EntityCandidate).where(EntityCandidate.status == "pending")
        )).scalars().all()
        processed = (await session.execute(select(ProcessedTranscript))).scalars().all()
        text_by_sha = _archived_text_by_sha(vault, processed)

        reviewable = [c for c in cands if c.kind in REVIEWABLE]
        dismissed: list[EntityCandidate] = []
        refreshed: list[tuple[EntityCandidate, str | None]] = []

        for c in reviewable:
            p = _payload(c)
            # only apply the salience filter when we could verify the archived text
            # (its bytes still hash to the ProcessedTranscript row) — otherwise keep the row
            if (c.kind == "person" and c.transcript_sha in text_by_sha
                    and not should_queue_person(c.surface_form, p, text_by_sha[c.transcript_sha])):
                dismissed.append(c)
                continue
            if c.kind == "person":
                m = vault.match_person(c.surface_form, org=p.get("org", ""),
                                       role=p.get("role", ""), index=people_idx)
            elif c.kind == "project":
                m = vault.match_project(c.surface_form, index=projects_idx)
            else:
                m = None
            if m is not None:
                new_sug = (json.dumps(m["suggested_matches"], ensure_ascii=False)
                           if m["suggested_matches"] else None)
                if new_sug != c.suggested_matches_json:
                    refreshed.append((c, new_sug))

        # stage changes in-memory (committed only under --apply)
        for c in dismissed:
            c.status = "dismissed"
            c.resolved_at = datetime.now()
            c.resolved_to = None
        for c, new_sug in refreshed:
            c.suggested_matches_json = new_sug

        by_sha: dict[str, list[EntityCandidate]] = {}
        for c in cands:
            by_sha.setdefault(c.transcript_sha, []).append(c)

        base = vault.vault_path.joinpath(*_TRIAGEM_SUBDIR)
        note_diffs: list[tuple[Path, str, str]] = []
        for sha, rows in by_sha.items():
            note = _note_for_sha(base, sha, vault)
            if note is None:
                continue
            survivors = [r for r in rows if r.kind in REVIEWABLE and r.status == "pending"]
            old_text = vault.read_file(note) or ""
            new_text = _render(old_text, sha, survivors)
            if new_text != old_text:
                note_diffs.append((note, old_text, new_text))

        # ---- report ----
        print(f"pending reviewable rows: {len(reviewable)}")
        print(f"  dismiss (salience):    {len(dismissed)}")
        for c in dismissed:
            print(f"      - {c.kind}/{c.surface_form!r}  sha {c.transcript_sha[:8]}")
        print(f"  suggestions refreshed: {len(refreshed)}")
        for c, new_sug in refreshed:
            print(f"      - {c.kind}/{c.surface_form!r} -> {new_sug}")
        print(f"\nTriagem notes changed: {len(note_diffs)}")
        for note, old, new in note_diffs:
            print(f"\n===== {note.name} =====")
            sys.stdout.writelines(difflib.unified_diff(
                old.splitlines(keepends=True), new.splitlines(keepends=True),
                fromfile="antes", tofile="depois", n=2))

        if not apply:
            await session.rollback()
            print("\n[dry-run] nada gravado. Rode com --apply para aplicar.")
            return

        for note, _old, new in note_diffs:
            vault.write_file_atomic(note, new)
        await session.commit()
        print(f"\n[applied] {len(dismissed)} dismissed, "
              f"{len(refreshed)} refreshed, {len(note_diffs)} notas reescritas.")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true", help="write changes (default: dry-run)")
    asyncio.run(run(ap.parse_args().apply))
