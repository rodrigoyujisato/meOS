"""Fase 1: the spoken-diary ingestion pipeline (07_Inbox_Agent/Diario -> 09_Journal)."""

from datetime import date

import pytest
from sqlalchemy import select

from rysos.ai.gemini import _coerce_minutes
from rysos.connectors.transcripts import TranscriptConnector, _DIARY_INBOX_SUBDIR
from rysos.core import rysos_core
from rysos.db import get_db_session, EntityCandidate, ProcessedDiaryEntry
from rysos.vault.manager import VaultManager
from rysos.vault.triagem import apply_pending_triagem


# --- fixtures ---------------------------------------------------------------

def _narration(marker: str = "hoje") -> str:
    """Plausible first-person diary prose, comfortably over DIARY_MIN_CHARS (120)."""
    return (
        f"Gravando o diario de {marker}. Conversei com a Marina sobre o projeto Altamar; "
        "a Marina vai revisar o orcamento e decidi adiar a proposta pra semana que vem. "
        "Preciso mandar o deck pro Nilton amanha de manha sem falta. "
        "Fiquei com a duvida se o Nilton certo e o do juridico ou o Zeich."
    )


def _rich(**over):
    base = {
        "title": "Diario Altamar e proposta",
        "meeting_type": "reflexao",
        "para_area": "Negocios_e_Governanca",
        "people": [
            {"name": "Marina", "role": "Sócia", "org": "Altamar",
             "mentions": "Marina vai revisar o orçamento e fechar a proposta."},
        ],
        "projects": [{"name": "Altamar", "status": "ativo", "summary": "Parceria em discussao"}],
        "decisions": [
            {"title": "Adiar a proposta", "status": "decidido", "rationale": "Falta fechar orcamento", "owner": "Rafael"},
        ],
        "action_items": [
            {"text": "Mandar o deck pro Nilton", "owner": "", "due": "2026-09-10"},
        ],
        "open_questions": ["O Nilton certo e o do juridico ou o Zeich?"],
        "summary_bullets": ["Conversa com a Marina", "Proposta adiada", "Deck pro Nilton amanha"],
    }
    base.update(over)
    return _coerce_minutes(base, meeting_year=2026)


def _patch_ai(monkeypatch, rich):
    monkeypatch.setattr(
        rysos_core.ai, "summarize_diary_entry",
        lambda text, filename="", *, entry_date=None, today=None: dict(rich),
    )


async def _run(monkeypatch, name="2026-09-09 - diario.txt", rich=None,
               body=None, target_date=date(2026, 9, 9)):
    vault = rysos_core.vault
    inbox = vault.vault_path / "07_Inbox_Agent" / "Diario"
    inbox.mkdir(parents=True, exist_ok=True)
    (inbox / name).write_text(body or _narration(), encoding="utf-8")
    _patch_ai(monkeypatch, rich if rich is not None else _rich())
    return await rysos_core.process_pending_diary(target_date=target_date)


def _journal(vault, date_str="2026-09-09"):
    return vault.vault_path / "09_Journal" / f"{date_str}.md"


def _diary_triagem_notes(vault):
    return sorted((vault.vault_path / "07_Inbox_Agent" / "Triagem").glob("Diario *.md"))


async def _cands():
    async with get_db_session() as s:
        return (await s.execute(select(EntityCandidate))).scalars().all()


# --- connector -----------------------------------------------------------------

def test_diary_connector_scans_its_own_folder_with_a_lower_floor(tmp_path):
    vault = tmp_path / "v"
    base = vault / "07_Inbox_Agent" / "Diario"
    base.mkdir(parents=True)
    (base / "2026-09-09 - curta.txt").write_text("[0.00 - 2.10]  " + "palavra " * 20, encoding="utf-8")  # ~140 chars
    (base / "minuscula.txt").write_text("so isso", encoding="utf-8")  # under 120

    conn = TranscriptConnector(vault, inbox_subdir=_DIARY_INBOX_SUBDIR, min_chars=120)
    found = conn.scan()
    assert [f.filename for f in found] == ["2026-09-09 - curta.txt"]
    assert found[0].date_hint == date(2026, 9, 9)
    assert not found[0].raw_text.startswith("[0.00")  # speech-to-text timestamp stripped


def test_transcript_connector_defaults_unchanged(tmp_path):
    """The parametrization must not alter the meeting-transcript connector."""
    vault = tmp_path / "v"
    (vault / "07_Inbox_Agent" / "Transcricoes").mkdir(parents=True)
    (vault / "07_Inbox_Agent" / "Transcricoes" / "curta.txt").write_text("x " * 100, encoding="utf-8")  # 200 chars < 400
    conn = TranscriptConnector(vault)
    assert conn.inbox_dir.name == "Transcricoes"
    assert conn.scan() == []  # 400-char floor still applies


# --- pipeline ----------------------------------------------------------------

async def test_journal_note_created_with_verbatim_narration(monkeypatch):
    vault = rysos_core.vault
    body = _narration("terca")
    await _run(monkeypatch, body=body)

    note = _journal(vault)
    assert note.exists()
    text = note.read_text(encoding="utf-8")
    assert "type: journal" in text
    assert "# 📓 Diário — 09/09/2026" in text
    assert "#### 🎙️ Narração (verbatim)" in text
    # every source line preserved, blockquote-prefixed, nothing invented/dropped
    for ln in body.splitlines():
        assert f"> {ln}" in text
    assert text.count("<!-- transcript:") == 1


async def test_structured_extraction_and_processed_row(monkeypatch):
    vault = rysos_core.vault
    res = await _run(monkeypatch)
    assert res["processed"] == 1

    text = _journal(vault).read_text(encoding="utf-8")
    assert "### 📓 Entrada: Diario Altamar e proposta" in text
    assert "#### 🧩 Extração estruturada" in text
    assert "**Resumo:**" in text
    assert "tom: reflexao" in text

    async with get_db_session() as s:
        rows = (await s.execute(select(ProcessedDiaryEntry))).scalars().all()
    assert len(rows) == 1
    assert rows[0].journal_note_path.endswith("09_Journal/2026-09-09.md")
    assert rows[0].entry_date == "2026-09-09"


async def test_auto_links_existing_person(monkeypatch):
    vault = rysos_core.vault
    vault.create_person_note(name="Marina Souza", role="Sócia", organization="Altamar")
    rich = _rich(people=[{"name": "Marina Souza", "role": "Sócia", "org": "Altamar",
                          "mentions": "Marina vai revisar o orçamento."}])
    await _run(monkeypatch, rich=rich)

    text = _journal(vault).read_text(encoding="utf-8")
    assert "[[05_People/Marina Souza|Marina Souza]]" in text
    cands = {(c.kind, c.normalized_form) for c in await _cands()}
    assert ("person", "marina souza") not in cands  # resolved, not queued


async def test_action_items_and_deadlines_reach_the_cockpit(monkeypatch):
    vault = rysos_core.vault
    rich = _rich(
        action_items=[{"text": "Mandar o deck pro Nilton", "owner": "", "due": "2026-09-10"}],
        deadlines=[
            {"what": "Entregar orcamento revisado", "when": "2026-09-12"},   # within 14d
            {"what": "Revisao trimestral", "when": "2026-12-01"},            # far out
        ],
    )
    await _run(monkeypatch, rich=rich, target_date=date(2026, 9, 9))

    cockpit = (vault.vault_path / "00_Cockpit" / "Daily" / "2026-09-09.md").read_text(encoding="utf-8")
    assert "**📓 Mandar o deck pro Nilton** (Diário)" in cockpit
    assert "**📅 Entregar orcamento revisado** (Prazo (diário))" in cockpit
    assert "Revisao trimestral" not in cockpit


async def test_ambiguous_entities_go_to_a_per_entry_triagem_note(monkeypatch):
    vault = rysos_core.vault
    await _run(monkeypatch)

    rows = await _cands()
    kinds = {c.kind for c in rows}
    assert "person" in kinds and "decision" in kinds
    assert all(c.status == "pending" for c in rows)

    notes = _diary_triagem_notes(vault)
    assert len(notes) == 1
    tn = notes[0].read_text(encoding="utf-8")
    assert notes[0].name.startswith("Diario 2026-09-09 [")
    assert "Marina" in tn
    assert "[[09_Journal/2026-09-09|" in tn  # header wikilink points at the journal note


async def test_two_entries_same_day_do_not_clobber(monkeypatch):
    vault = rysos_core.vault
    await _run(monkeypatch, name="2026-09-09 - manha.txt", body=_narration("de manha"))
    n_after_first = len(await _cands())
    await _run(monkeypatch, name="2026-09-09 - noite.txt",
              body=_narration("de noite, outra entrada totalmente distinta do dia"),
              rich=_rich(title="Diario da noite"))

    text = _journal(vault).read_text(encoding="utf-8")
    assert text.count("### 📓 Entrada:") == 2
    assert text.count("<!-- transcript:") == 2

    notes = _diary_triagem_notes(vault)
    assert len(notes) == 2  # one per entry, not one overwriting the other
    assert len(await _cands()) >= n_after_first  # first entry's rows survived


async def test_idempotent_on_redrop(monkeypatch):
    vault = rysos_core.vault
    await _run(monkeypatch, name="2026-09-09 - a.txt")
    first = len(await _cands())

    # byte-identical content, different filename
    res2 = await _run(monkeypatch, name="2026-09-09 - copia.txt")
    assert res2["processed"] == 0
    assert len(await _cands()) == first
    text = _journal(vault).read_text(encoding="utf-8")
    assert text.count("### 📓 Entrada:") == 1
    async with get_db_session() as s:
        assert len((await s.execute(select(ProcessedDiaryEntry))).scalars().all()) == 1


def test_append_diary_entry_idempotent_on_marker(tmp_path):
    vault = VaultManager(vault_path=tmp_path)
    vault.initialize_vault_structure()
    jp = vault.ensure_journal_note(date(2026, 9, 9))
    digest = "c" * 64
    w1 = vault.append_diary_entry(jp, digest, title="X", rich=_rich(), raw_narration="linha um\nlinha dois")
    body1 = jp.read_text(encoding="utf-8")
    w2 = vault.append_diary_entry(jp, digest, title="X", rich=_rich(), raw_narration="linha um\nlinha dois")
    body2 = jp.read_text(encoding="utf-8")
    assert w1 is True and w2 is False
    assert body1 == body2
    assert body1.count("<!-- transcript:" + digest[:12] + " -->") == 1


async def test_parseback_resolution_upgrades_journal_wikilink(monkeypatch):
    vault = rysos_core.vault
    await _run(monkeypatch)
    tn = _diary_triagem_notes(vault)[0]

    text = tn.read_text(encoding="utf-8")
    # edit the Marina resolução line the way a human would in Obsidian
    out = []
    hit = False
    for ln in text.splitlines():
        if ln.startswith("- [ ] **Marina**"):
            hit = True
        elif hit and ln.strip().startswith("resolução:"):
            indent = ln[: len(ln) - len(ln.lstrip())]
            ln = f"{indent}resolução: NOVO"
            hit = False
        out.append(ln)
    tn.write_text("\n".join(out) + "\n", encoding="utf-8")

    applied = await apply_pending_triagem(vault)
    assert applied >= 1
    assert (vault.vault_path / "05_People" / "Marina.md").exists()
    cand = next(c for c in await _cands() if c.kind == "person" and c.normalized_form == "marina")
    assert cand.status == "confirmed_new"
    journal = _journal(vault).read_text(encoding="utf-8")
    assert "[[05_People/Marina|Marina]]" in journal
    assert "⚠️ não confirmado" not in journal.split("Marina")[1][:40]


async def test_unconfirmed_date_becomes_open_question_not_a_meeting_date_row(monkeypatch):
    vault = rysos_core.vault
    # filename carries no date -> date_source == "mtime"
    await _run(monkeypatch, name="diario sem data.txt", rich=_rich(date_hint=""))

    rows = await _cands()
    assert all(c.kind != "meeting_date" for c in rows)
    text = _journal(vault).read_text(encoding="utf-8")
    assert "De que dia é esta entrada?" in text


async def test_same_day_entry_does_not_nag_about_its_date(monkeypatch):
    """The common case: recorded and transcribed the same day. The model returns
    today's date, `_resolve_meeting_date` discards it as a prompt echo and reports
    'mtime' — but the date was never in doubt, so no open question is raised."""
    vault = rysos_core.vault
    # no date in the filename; model spoke today's date (== mtime == target_date)
    await _run(monkeypatch, name="diario sem data.txt",
               rich=_rich(date_hint="2026-09-09"))

    text = _journal(vault).read_text(encoding="utf-8")
    assert "De que dia é esta entrada?" not in text


async def test_degraded_extraction_still_files_and_flags(monkeypatch):
    vault = rysos_core.vault
    degraded = _rich(title="entrada ruidosa")
    degraded["_degraded"] = True
    await _run(monkeypatch, rich=degraded)

    assert _journal(vault).exists()
    tn = _diary_triagem_notes(vault)[0].read_text(encoding="utf-8")
    assert "Extração degradada" in tn
    rows = await _cands()
    assert any(c.kind == "extraction_failed" for c in rows)
    # raw file archived
    assert not list((vault.vault_path / "07_Inbox_Agent" / "Diario").glob("*.txt"))
    assert list((vault.vault_path / "07_Inbox_Agent" / "Diario" / "Processado").glob("*.txt"))


async def test_gemini_unavailable_still_files_the_entry(monkeypatch):
    vault = rysos_core.vault
    inbox = vault.vault_path / "07_Inbox_Agent" / "Diario"
    inbox.mkdir(parents=True, exist_ok=True)
    body = _narration("sem IA")
    (inbox / "2026-09-09 - offline.txt").write_text(body, encoding="utf-8")
    monkeypatch.setattr(rysos_core.ai, "is_available", lambda: False)
    monkeypatch.delattr(rysos_core.ai, "summarize_diary_entry", raising=False)

    res = await rysos_core.process_pending_diary(target_date=date(2026, 9, 9))
    assert res["processed"] == 1
    text = _journal(vault).read_text(encoding="utf-8")
    for ln in body.splitlines():
        assert f"> {ln}" in text


async def test_run_daily_sync_invokes_diary(monkeypatch):
    called = {}

    async def _fake(target_date=None):
        called["hit"] = target_date
        return {"processed": 3, "notes": []}

    monkeypatch.setattr(rysos_core, "process_pending_diary", _fake)
    monkeypatch.setattr(rysos_core, "process_pending_transcripts",
                        lambda *a, **k: _noop_result())
    out = await rysos_core.run_daily_sync(target_date=date(2026, 9, 9))
    assert called.get("hit") == date(2026, 9, 9)
    assert out["diary_processed"] == 3


async def _noop_result():
    return {"processed": 0, "notes": []}


async def test_run_daily_sync_archives_descartado_decision_and_drops_db_row(monkeypatch):
    from rysos.db import DecisionRecord

    vault = rysos_core.vault
    vault.create_decision_note(
        decision_id="DEC-2026-999", title="Decisão lixo", status="descartado",
        file_stem="DEC-2026-999 - Decisão lixo",
    )
    async with get_db_session() as session:
        session.add(DecisionRecord(id="DEC-2026-999", title="Decisão lixo", status="descartado"))
        # a decision archived with no matching DB row must not error the sync
        await session.commit()

    monkeypatch.setattr(rysos_core, "process_pending_diary", lambda *a, **k: _noop_result())
    monkeypatch.setattr(rysos_core, "process_pending_transcripts",
                        lambda *a, **k: _noop_result())

    out = await rysos_core.run_daily_sync(target_date=date(2026, 9, 9))

    assert not (vault.vault_path / "03_Decisions" / "DEC-2026-999 - Decisão lixo.md").exists()
    assert (vault.vault_path / "08_Archive" / "Decisions" / "DEC-2026-999 - Decisão lixo.md").exists()
    async with get_db_session() as session:
        row = await session.get(DecisionRecord, "DEC-2026-999")
    assert row is None
    assert not out.get("warnings")


async def test_run_daily_sync_does_not_error_when_archived_decision_has_no_db_row(monkeypatch):
    vault = rysos_core.vault
    vault.create_decision_note(
        decision_id="DEC-2026-998", title="Decisão sem registro", status="descartado",
        file_stem="DEC-2026-998 - Decisão sem registro",
    )

    monkeypatch.setattr(rysos_core, "process_pending_diary", lambda *a, **k: _noop_result())
    monkeypatch.setattr(rysos_core, "process_pending_transcripts",
                        lambda *a, **k: _noop_result())

    out = await rysos_core.run_daily_sync(target_date=date(2026, 9, 9))

    assert not (vault.vault_path / "03_Decisions" / "DEC-2026-998 - Decisão sem registro.md").exists()
    assert (vault.vault_path / "08_Archive" / "Decisions" / "DEC-2026-998 - Decisão sem registro.md").exists()
    assert not out.get("warnings")
