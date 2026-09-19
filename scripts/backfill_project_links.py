"""One-off: backfill the project <-> meeting/decision/resource wikilinks for everything
resolved before `rysos.connectors.project_links` existed.

  .venv/bin/python scripts/backfill_project_links.py            # dry-run
  .venv/bin/python scripts/backfill_project_links.py --apply    # write
  .venv/bin/python scripts/backfill_project_links.py --apply --ask  # + next batch of questions

Same code path as the periodic job (`sync_project_links`), over all history instead of the
last 7 days: every meeting note lists itself under each project it links to, and every
resolved decision/resource gets the strict rule (cites a project by full title, else the
meeting's single project). Decisions/resources from a meeting that touched several projects
without citing one stay unlinked; with `--ask` (needs `--apply`) the next batch of at most 5
of them is sent to Telegram as questions (each is asked once — rerun for the next batch). Idempotent and safe to re-run — a second `--apply` finishes whatever an rclone
`[Errno 5]` interrupted. The dry-run does not report meeting -> project edges (they are
idempotent and always written on --apply).
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from rysos.connectors.project_links import sync_project_links  # noqa: E402
from rysos.vault.manager import VaultManager  # noqa: E402


async def main(apply: bool, ask: bool) -> None:
    ask_fn = None
    if ask:
        from rysos.telegram.entity_review import ask_project_link
        ask_fn = ask_project_link
    report = await sync_project_links(VaultManager(), since_days=None, apply=apply, ask=ask_fn)

    for title, projects in report.linked:
        print(f"vincula: {title} -> {', '.join(projects)}")
    for title, projects in report.ambiguous:
        print(f"AMBÍGUO (não vinculado): {title} — reunião tratou de {', '.join(projects)}")

    mode = "aplicado" if apply else "dry-run (use --apply para gravar)"
    print(
        f"\n{len(report.linked)} item(ns) {'vinculados' if apply else 'seriam vinculados'}, "
        f"{len(report.ambiguous)} ambíguo(s) sem vínculo, {report.deferred} adiado(s) "
        f"(reunião com projeto ainda pendente), {report.locked} travado(s) (related_projects preenchido "
        f"por você, só espelhado), {report.skipped_missing} pulado(s) (nota não existe mais); "
        f"{report.meeting_edges} nota(s) de projeto atualizada(s) com reuniões — {mode}."
    )
    if ask:
        print(f"{report.asked} pergunta(s) enviada(s) ao Telegram.")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--apply", action="store_true", help="write changes (default: dry-run)")
    parser.add_argument("--ask", action="store_true", help="with --apply: send the next batch of questions")
    args = parser.parse_args()
    if args.ask and not args.apply:
        parser.error("--ask requires --apply")
    asyncio.run(main(args.apply, args.ask))
