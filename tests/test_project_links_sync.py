"""Tests for connectors/project_links.py: the strict rule, the periodic sync and the Telegram question."""

from pathlib import Path

from rysos.vault.manager import VaultManager

# --- strict rule ----------------------------------------------------------------------

import json
from datetime import datetime
from unittest.mock import AsyncMock

from rysos.connectors import project_links as pl
from rysos.core import rysos_core
from rysos.db import DecisionRecord, EntityCandidate, get_db_session
from rysos.telegram import entity_review as er


def _proj(title: str) -> dict:
    return {"title": title, "path": f"/x/{title}.md"}


def test_cited_projects_matches_full_title_on_word_boundaries():
    projects = [_proj("Meridian"), _proj("Transicao Moreira"), _proj("Zenith Care")]
    assert [p["title"] for p in pl.cited_projects("PI da Meridian pertence à empresa", projects)] == ["Meridian"]
    assert pl.cited_projects("Reunião com Rafael Moreira", projects) == []
    assert pl.cited_projects("Meridiana e outros", projects) == []
    assert [p["title"] for p in pl.cited_projects("Transição Moreira: próximos passos", projects)] == ["Transicao Moreira"]


def test_decide_follows_the_strict_rule():
    a, b = _proj("A"), _proj("B")
    assert pl.decide([a], [a, b], False).projects == [a]            # citation wins over ambiguity
    assert pl.decide([a], [], True).action == "link"                # ...even with a pending project
    assert pl.decide([], [a], True).action == "defer"               # touched-set not final yet
    assert pl.decide([], [a], False).projects == [a]                # single project inherits
    assert pl.decide([], [a, b], False).action == "ambiguous"       # several, none cited -> ask
    assert pl.decide([], [], False).action == "none"


def _meeting(vault: VaultManager, name: str, projects: list[str]) -> Path:
    links = "\n".join(f"- [[01_Projects/{p}|{p}]]" for p in projects)
    path = vault.vault_path / "04_Meetings" / name
    path.write_text(f"# Reunião\n\n## Projetos\n{links}\n", encoding="utf-8")
    return path


async def _decision(vault: VaultManager, meeting: Path, title: str, dec_id="DEC-2026-1", **over):
    path = vault.create_decision_note(dec_id, title, file_stem=f"{dec_id} - {title}")
    row = dict(
        transcript_sha="e" * 64, meeting_note_path=str(meeting), kind="decision",
        surface_form=title, normalized_form=title.lower(), context_sentence="",
        status="confirmed_new", resolved_to=dec_id, resolved_at=datetime.now(),
    )
    row.update(over)
    async with get_db_session() as session:
        session.add(DecisionRecord(id=dec_id, title=title, note_path=str(path)))
        c = EntityCandidate(**row)
        session.add(c)
        await session.commit()
        return path, c.id


async def _cand(cand_id: int) -> EntityCandidate:
    async with get_db_session() as session:
        return await session.get(EntityCandidate, cand_id)


async def test_sync_links_a_single_project_meeting_both_ways(tmp_path):
    vault = rysos_core.vault
    vault.initialize_vault_structure()
    proj = vault.create_project_note("Meridian")
    meeting = _meeting(vault, "2026-09-15 - Alinhamento.md", ["Meridian"])
    dec, cid = await _decision(vault, meeting, "Contratar advogado")

    report = await pl.sync_project_links(vault, since_days=None)

    assert report.linked == [("Contratar advogado", ["Meridian"])]
    assert "[[01_Projects/Meridian|Meridian]]" in dec.read_text(encoding="utf-8")
    proj_text = proj.read_text(encoding="utf-8")
    assert "[[03_Decisions/DEC-2026-1 - Contratar advogado|Contratar advogado]]" in proj_text
    assert "[[04_Meetings/2026-09-15 - Alinhamento|2026-09-15 - Alinhamento]]" in proj_text  # meeting edge
    assert json.loads((await _cand(cid)).payload_json)["project_link"] == "done"

    again = await pl.sync_project_links(vault, since_days=None)  # idempotent: marker + guarded writes
    assert again.linked == [] and proj.read_text(encoding="utf-8") == proj_text


async def test_sync_multi_project_meeting_cites_or_asks(tmp_path):
    vault = rysos_core.vault
    vault.initialize_vault_structure()
    nuv = vault.create_project_note("Meridian")
    viky = vault.create_project_note("Zenith Care")
    meeting = _meeting(vault, "2026-09-11 - Zenith e Meridian.md", ["Meridian", "Zenith Care"])
    cited, _ = await _decision(vault, meeting, "PI da Meridian pertence à empresa", "DEC-2026-1")
    vague, vague_id = await _decision(vault, meeting, "Tirar duas semanas de férias", "DEC-2026-2")

    ask = AsyncMock(return_value=True)
    report = await pl.sync_project_links(vault, since_days=None, ask=ask)

    assert report.linked == [("PI da Meridian pertence à empresa", ["Meridian"])]
    assert "Zenith" not in cited.read_text(encoding="utf-8").split("---")[1]   # strict: not inherited
    assert "01_Projects" not in vague.read_text(encoding="utf-8")            # ambiguous: nothing yet
    assert report.ambiguous == [("Tirar duas semanas de férias", ["Meridian", "Zenith Care"])]
    ask.assert_awaited_once()
    assert json.loads((await _cand(vague_id)).payload_json)["project_link"] == "asked"

    await pl.sync_project_links(vault, since_days=None, ask=ask)
    assert ask.await_count == 1  # asked once, never again


async def test_sync_defers_while_a_project_candidate_is_pending_and_dry_run_writes_nothing(tmp_path):
    vault = rysos_core.vault
    vault.initialize_vault_structure()
    proj = vault.create_project_note("Meridian")
    meeting = _meeting(vault, "2026-09-15 - A.md", ["Meridian"])
    dec, _ = await _decision(vault, meeting, "Algo qualquer")
    async with get_db_session() as session:
        session.add(EntityCandidate(
            transcript_sha="e" * 64, meeting_note_path=str(meeting), kind="project",
            surface_form="Outro", normalized_form="outro", status="pending",
        ))
        await session.commit()

    report = await pl.sync_project_links(vault, since_days=None)
    assert report.deferred == 1 and report.linked == []
    assert "01_Projects" not in dec.read_text(encoding="utf-8")

    before = proj.read_text(encoding="utf-8")
    async with get_db_session() as session:
        from sqlalchemy import delete
        await session.execute(delete(EntityCandidate).where(EntityCandidate.kind == "project"))
        await session.commit()
    dry = await pl.sync_project_links(vault, since_days=None, apply=False)
    assert dry.linked == [("Algo qualquer", ["Meridian"])]
    assert "01_Projects" not in dec.read_text(encoding="utf-8") and proj.read_text(encoding="utf-8") == before


async def test_prjlnk_callback_links_the_chosen_project_only(tmp_path, monkeypatch):
    vault = rysos_core.vault
    vault.initialize_vault_structure()
    nuv = vault.create_project_note("Meridian")
    viky = vault.create_project_note("Zenith Care")
    meeting = _meeting(vault, "2026-09-11 - Zenith e Meridian.md", ["Meridian", "Zenith Care"])
    dec, cid = await _decision(vault, meeting, "Tirar duas semanas de férias")

    ans = AsyncMock(return_value=True)
    edit = AsyncMock(return_value={"ok": True})
    monkeypatch.setattr(er.telegram_bot, "answer_callback_query", ans)
    monkeypatch.setattr(er.telegram_bot, "edit_message_text", edit)
    cb = {"id": "c", "message": {"chat": {"id": 1}, "message_id": 2}}

    await er.handle_prjlnk_callback(cb, ["prjlnk", str(cid), "1"])  # sorted: 0=Meridian, 1=Zenith Care

    fm = dec.read_text(encoding="utf-8").split("---")[1]
    assert "[[01_Projects/Zenith Care|Zenith Care]]" in fm and "Meridian" not in fm
    assert "Tirar duas semanas" in viky.read_text(encoding="utf-8")
    assert "Tirar duas semanas" not in nuv.read_text(encoding="utf-8")
    assert json.loads((await _cand(cid)).payload_json)["project_link"] == "done"

    await er.handle_prjlnk_callback(cb, ["prjlnk", str(cid), "all"])  # already answered: no-op
    assert "Meridian" not in dec.read_text(encoding="utf-8").split("---")[1]


async def test_resolve_item_path_finds_a_renamed_note_but_never_guesses(tmp_path):
    vault = rysos_core.vault
    vault.initialize_vault_structure()
    res_dir = vault.vault_path / "06_Resources" / "Leituras_e_Pesquisas"
    live = res_dir / "Service Blueprint - Design System.md"
    live.write_text("# x\n", encoding="utf-8")
    cand = EntityCandidate(
        transcript_sha="e" * 64, meeting_note_path=str(tmp_path / "m.md"), kind="resource",
        surface_form="Service Blueprint / Design System", normalized_form="sb",
        resolved_to=str(res_dir / "Service Blueprint _ Design System.md"),
    )
    async with get_db_session() as session:
        found = await pl.resolve_item_path(session, vault, cand)
        assert found and found[0] == live

        (res_dir / "Service Blueprint _ Design System copy.md").write_text("# y\n", encoding="utf-8")
        (res_dir / "Service Blueprint Design System.md").write_text("# z\n", encoding="utf-8")
        assert await pl.resolve_item_path(session, vault, cand) is None  # two matches: no guess
        (vault.vault_path / "08_Archive" / "Resources").mkdir(parents=True, exist_ok=True)
        cand.resolved_to = str(res_dir / "Sumiu.md")
        (vault.vault_path / "08_Archive" / "Resources" / "Sumiu.md").write_text("# a\n", encoding="utf-8")
        assert await pl.resolve_item_path(session, vault, cand) is None  # archive is never searched


async def test_ask_within_days_links_old_items_but_only_asks_fresh_ones(tmp_path):
    from datetime import timedelta
    vault = rysos_core.vault
    vault.initialize_vault_structure()
    vault.create_project_note("Meridian")
    vault.create_project_note("Zenith Care")
    meeting = _meeting(vault, "2026-09-11 - Zenith e Meridian.md", ["Meridian", "Zenith Care"])
    old_single = _meeting(vault, "2026-09-05 - So Meridian.md", ["Meridian"])
    old_dec, old_id = await _decision(vault, meeting, "Decisão antiga vaga", "DEC-2026-1",
                                      resolved_at=datetime.now() - timedelta(days=5))
    fresh_dec, fresh_id = await _decision(vault, meeting, "Decisão nova vaga", "DEC-2026-2")
    linked_dec, _ = await _decision(vault, old_single, "Decisão antiga clara", "DEC-2026-3",
                                    resolved_at=datetime.now() - timedelta(days=5))

    ask = AsyncMock(return_value=True)
    report = await pl.sync_project_links(vault, since_days=14, ask=ask, ask_within_days=1)

    assert ask.await_count == 1 and ask.await_args.args[1] == "Decisão nova vaga"  # only the fresh one
    assert json.loads((await _cand(fresh_id)).payload_json)["project_link"] == "asked"
    assert (await _cand(old_id)).payload_json in (None, "{}", "")                   # old: reported, not marked
    assert {t for t, _ in report.ambiguous} == {"Decisão antiga vaga", "Decisão nova vaga"}
    assert "[[01_Projects/Meridian|Meridian]]" in linked_dec.read_text(encoding="utf-8")  # old but clear: linked


async def test_filled_related_projects_locks_the_item_and_is_mirrored(tmp_path):
    vault = rysos_core.vault
    vault.initialize_vault_structure()
    nuv = vault.create_project_note("Meridian")
    viky = vault.create_project_note("Zenith Care")
    meeting = _meeting(vault, "2026-09-11 - Zenith e Meridian.md", ["Meridian", "Zenith Care"])
    dec, cid = await _decision(vault, meeting, "Decisão vaga")
    vault.add_related_project(dec, "Zenith Care")       # the user's manual edit
    vault.add_related_project(dec, "Projeto Fantasma")  # unknown stem: ignored, never invented

    ask = AsyncMock(return_value=True)
    report = await pl.sync_project_links(vault, since_days=None, ask=ask)

    assert report.locked == 1 and report.ambiguous == [] and report.linked == []
    ask.assert_not_awaited()
    assert "Decisão vaga" in viky.read_text(encoding="utf-8")       # mirrored into the project note
    assert "Decisão vaga" not in nuv.read_text(encoding="utf-8")    # nothing added beyond the user's word
    assert (await _cand(cid)).payload_json in (None, "{}", "")      # no marker: emptying hands it back

    vault.remove_related_project(dec, "Zenith Care")
    vault.remove_related_project(dec, "Projeto Fantasma")
    back = await pl.sync_project_links(vault, since_days=None, ask=ask)
    assert back.locked == 0 and [t for t, _ in back.ambiguous] == ["Decisão vaga"]
    ask.assert_awaited_once()


def test_related_project_stems_reads_block_and_inline_lists(tmp_path):
    vault = rysos_core.vault
    vault.initialize_vault_structure()
    a = tmp_path / "a.md"
    a.write_text('---\nid: X\nrelated_projects:\n  - "[[01_Projects/A|A]]"\n  - "[[01_Projects/B B|x]]"\ndecision_date: 1\n---\n', encoding="utf-8")
    b = tmp_path / "b.md"
    b.write_text('---\nrelated_projects: ["[[01_Projects/C|C]]"]\narea: "[[02_Areas/X]]"\n---\n', encoding="utf-8")
    c = tmp_path / "c.md"
    c.write_text('---\nrelated_projects:\n\ntags:\n  - "[[01_Projects/NAO]]"\n---\n', encoding="utf-8")
    assert vault.related_project_stems(a) == ["A", "B B"]
    assert vault.related_project_stems(b) == ["C"]
    assert vault.related_project_stems(c) == []
