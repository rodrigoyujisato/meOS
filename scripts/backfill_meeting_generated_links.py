"""One-off: backfill Meeting -> Resource/Decision backlinks for entities that were
already resolved before `VaultManager.append_meeting_generated_link` existed.

  .venv/bin/python scripts/backfill_meeting_generated_links.py            # dry-run
  .venv/bin/python scripts/backfill_meeting_generated_links.py --apply    # write

Source of truth: `entity_candidates` rows (kind resource/decision, status
confirmed_new/linked, resolved_to set) already record exactly which meeting produced
or mentioned which resource/decision — the same data `entity_apply._apply_new` now
uses going forward. This script replays that history through
`append_meeting_generated_link` for every row whose meeting note and target note both
still exist. A resource's own `resolved_to` is its file path directly; a decision's is
its `dec_id`, resolved to a current file path via `DecisionRecord.note_path`.

Rows whose meeting note or target note no longer exists are skipped (reported, not an
error) — nothing to backfill onto. This does not touch anything created via
`save_quick_capture` (a direct Telegram capture, no source meeting at all) — those
notes have no meeting to link back to and stay orphaned, correctly.
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from sqlalchemy import select  # noqa: E402

from rysos.db import DecisionRecord, EntityCandidate, get_db_session  # noqa: E402
from rysos.vault.manager import VaultManager  # noqa: E402


async def main(apply: bool) -> None:
    vault = VaultManager()

    async with get_db_session() as session:
        cand_stmt = select(EntityCandidate).where(
            EntityCandidate.kind.in_(("resource", "decision")),
            EntityCandidate.resolved_to.isnot(None),
            EntityCandidate.status.in_(("confirmed_new", "linked")),
        )
        rows = (await session.execute(cand_stmt)).scalars().all()

        dec_stmt = select(DecisionRecord.id, DecisionRecord.title, DecisionRecord.note_path)
        dec_rows = (await session.execute(dec_stmt)).all()
        dec_path_by_id = {r.id: r.note_path for r in dec_rows if r.note_path}
        dec_title_by_id = {r.id: r.title for r in dec_rows}

    added = 0
    already_linked = 0
    skipped_no_meeting = 0
    skipped_no_target = 0

    for row in rows:
        meeting_path = Path(row.meeting_note_path)
        if not meeting_path.exists():
            skipped_no_meeting += 1
            continue

        if row.kind == "resource":
            target_path = Path(row.resolved_to)
            display_name = row.surface_form
        else:
            note_path_str = dec_path_by_id.get(row.resolved_to)
            target_path = Path(note_path_str) if note_path_str else None
            display_name = dec_title_by_id.get(row.resolved_to, row.surface_form)

        if not target_path or not target_path.exists():
            skipped_no_target += 1
            continue

        link_target = str(target_path.relative_to(vault.vault_path).with_suffix("").as_posix())
        wikilink = f"[[{link_target}|{display_name}]]"
        print(f"{meeting_path.name}: {wikilink}")

        if apply:
            changed = vault.append_meeting_generated_link(meeting_path, link_target, display_name)
            if changed:
                added += 1
            else:
                already_linked += 1
        else:
            added += 1

    mode = "aplicado" if apply else "dry-run (use --apply para gravar)"
    print(
        f"\n{added} link(s) {'adicionados' if apply else 'seriam adicionados'}, "
        f"{already_linked} já existiam, {skipped_no_meeting} pulados (reunião não existe mais), "
        f"{skipped_no_target} pulados (nota de destino não existe mais) — {mode}."
    )


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--apply", action="store_true", help="write changes (default: dry-run)")
    args = parser.parse_args()
    asyncio.run(main(args.apply))
