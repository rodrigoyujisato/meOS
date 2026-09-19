"""The Cockpit's free-write 'Notas Livres' block -> AI classification pipeline."""

from datetime import date

from sqlalchemy import select

from rysos.ai.gemini import _coerce_minutes
from rysos.core import rysos_core
from rysos.db import get_db_session, EntityCandidate, ProcessedCockpitNote


def _notes_text() -> str:
    return (
        "Hoje conversei com a Gisele sobre o projeto Altamar e decidimos adiar a proposta. "
        "Preciso mandar o deck pro Nilton amanha sem falta. Vi um artigo interessante sobre biobancos "
        "que quero guardar como referencia."
    )


def _rich(**over):
    base = {
        "title": "Notas Livres — 2026-09-15",
        "people": [{"name": "Gisele", "role": "", "org": "", "mentions": "Conversamos sobre o projeto."}],
        "projects": [{"name": "Altamar", "status": "ativo", "summary": "Discussao sobre a proposta"}],
        "decisions": [
            {"title": "Adiar a proposta", "status": "decidido", "rationale": "Falta alinhar detalhes", "owner": "Rafael"},
        ],
        "resources": [{"name": "Artigo sobre biobancos", "note": "Referencia mencionada"}],
        "action_items": [{"text": "Mandar o deck pro Nilton", "owner": "", "due": "2026-09-16"}],
        "summary_bullets": ["Conversa sobre Altamar", "Deck pro Nilton amanha"],
    }
    base.update(over)
    return _coerce_minutes(base, meeting_year=2026)


def _patch_ai(monkeypatch, rich):
    monkeypatch.setattr(
        rysos_core.ai, "summarize_diary_entry",
        lambda text, filename="", *, entry_date=None, today=None: dict(rich),
    )


def _write_free_notes(vault, target_date: date, text: str):
    cockpit = vault.create_daily_cockpit(target_date=target_date)
    content = cockpit.read_text(encoding="utf-8")
    updated = content.replace(
        "<!-- notas-livres:start -->\n\n<!-- notas-livres:end -->",
        f"<!-- notas-livres:start -->\n{text}\n<!-- notas-livres:end -->",
    )
    cockpit.write_text(updated, encoding="utf-8")
    return cockpit


async def _cands():
    async with get_db_session() as s:
        return (await s.execute(select(EntityCandidate))).scalars().all()


async def test_empty_block_is_a_noop(monkeypatch):
    vault = rysos_core.vault
    target_date = date(2026, 9, 15)
    vault.create_daily_cockpit(target_date=target_date)
    res = await rysos_core.process_cockpit_free_notes(target_date=target_date)
    assert res == {"processed": 0}
    assert await _cands() == []


async def test_action_item_lands_on_cockpit_and_candidates_are_queued(monkeypatch):
    vault = rysos_core.vault
    target_date = date(2026, 9, 15)
    _write_free_notes(vault, target_date, _notes_text())
    _patch_ai(monkeypatch, _rich())

    res = await rysos_core.process_cockpit_free_notes(target_date=target_date)
    assert res["processed"] == 1
    assert res["action_items"] == 1
    assert res["candidates"] >= 1

    cockpit = (vault.vault_path / "00_Cockpit" / "Daily" / "2026-09-15.md").read_text(encoding="utf-8")
    assert "**✍️ Mandar o deck pro Nilton** (Notas Livres)" in cockpit

    cands = await _cands()
    kinds = {c.kind for c in cands}
    assert "decision" in kinds or "project" in kinds or "resource" in kinds

    triagem_notes = list((vault.vault_path / "07_Inbox_Agent" / "Triagem").glob("Notas Livres *.md"))
    assert len(triagem_notes) == 1

    async with get_db_session() as s:
        rows = (await s.execute(select(ProcessedCockpitNote))).scalars().all()
    assert len(rows) == 1
    assert rows[0].cockpit_date == "2026-09-15"
    assert rows[0].action_items_count == 1


async def test_reprocessing_same_text_is_idempotent(monkeypatch):
    vault = rysos_core.vault
    target_date = date(2026, 9, 15)
    _write_free_notes(vault, target_date, _notes_text())
    _patch_ai(monkeypatch, _rich())

    first = await rysos_core.process_cockpit_free_notes(target_date=target_date)
    assert first["processed"] == 1

    second = await rysos_core.process_cockpit_free_notes(target_date=target_date)
    assert second == {"processed": 0, "already_done": True}

    async with get_db_session() as s:
        rows = (await s.execute(select(ProcessedCockpitNote))).scalars().all()
    assert len(rows) == 1  # not duplicated
