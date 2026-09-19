"""Tests for the manual meeting-transcript ingestion pipeline."""

from datetime import date, datetime
from pathlib import Path

import pytest

from rysos.connectors.transcripts import TranscriptConnector, _strip_vtt_srt
from rysos.core import rysos_core
from rysos.db import get_db_session, ProcessedTranscript
from rysos.vault.manager import VaultManager


def _body(marker: str = "reuniao") -> str:
    """A block of plausible transcript prose comfortably over _MIN_TRANSCRIPT_CHARS."""
    return (
        f"Rafael: bom dia pessoal, vamos comecar a {marker}. "
        "Hoje precisamos alinhar o escopo do projeto, os proximos passos e quem fica responsavel por cada frente. "
        "Maria: perfeito, do meu lado ja adiantei a parte de contrato e mando o rascunho ainda esta semana. "
        "Rafael: otimo, entao fechamos que voce envia o rascunho e eu reviso antes da proxima call. "
        "Joao: eu cuido da integracao tecnica e trago um status na sexta. "
        "Rafael: combinado, obrigado a todos, encerramos por aqui."
    )


# --- connector -----------------------------------------------------------------

def test_connector_scan_hashes_and_dates(tmp_path: Path):
    vault = tmp_path / "v"
    (vault / "07_Inbox_Agent" / "Transcricoes").mkdir(parents=True)
    f = vault / "07_Inbox_Agent" / "Transcricoes" / "Reuniao Farmalfa 2026-09-05.txt"
    f.write_text(_body("abbvie"), encoding="utf-8")

    conn = TranscriptConnector(vault)
    found = conn.scan()
    assert len(found) == 1
    assert found[0].date_hint == date(2026, 9, 5)
    assert len(found[0].sha256) == 64


def test_connector_skips_readme_short_and_processed(tmp_path: Path):
    vault = tmp_path / "v"
    base = vault / "07_Inbox_Agent" / "Transcricoes"
    (base / "Processado").mkdir(parents=True)
    (base / "empty.txt").write_text("   \n", encoding="utf-8")
    (base / "LEIA-ME.md").write_text("Solte aqui as transcricoes. " * 40, encoding="utf-8")  # long but ignored by name
    (base / "nota rapida.txt").write_text("lembrar de ligar pro Nilton", encoding="utf-8")   # under the min
    (base / "Processado" / "old.txt").write_text(_body("antiga"), encoding="utf-8")
    (base / "good.txt").write_text(_body("valida"), encoding="utf-8")

    found = TranscriptConnector(vault).scan()
    assert [f.filename for f in found] == ["good.txt"]


def test_connector_move_to_processed_no_clobber(tmp_path: Path):
    vault = tmp_path / "v"
    base = vault / "07_Inbox_Agent" / "Transcricoes"
    base.mkdir(parents=True)
    (base / "ata.txt").write_text(_body("versao um"), encoding="utf-8")
    conn = TranscriptConnector(vault)
    first = conn.scan()[0]
    conn.move_to_processed(first)

    (base / "ata.txt").write_text(_body("versao dois totalmente diferente"), encoding="utf-8")
    second = conn.scan()[0]
    dest = conn.move_to_processed(second)
    assert dest.exists()
    assert "versao um" in (base / "Processado" / "ata.txt").read_text(encoding="utf-8")
    assert "versao dois" in dest.read_text(encoding="utf-8")


def test_strip_vtt_srt_removes_timing_and_cue_numbers():
    vtt = "WEBVTT\n\n1\n00:00:01.000 --> 00:00:03.000\nOlá\n\n2\n00:00:03.000 --> 00:00:05.000\nTudo bem?"
    assert _strip_vtt_srt(vtt) == "Olá\nTudo bem?"


def test_strip_leading_bracket_timestamps_bracket_style():
    raw = "[0.00 - 3.74]  Pessoal, bom dia\n[3.74 - 5.70]  vamos comecar\n[00:01:12]  proximo ponto"
    assert _strip_vtt_srt(raw) == "Pessoal, bom dia\nvamos comecar\nproximo ponto"


# --- vault helpers -----------------------------------------------------------------

def test_append_transcript_minutes_is_idempotent_on_marker(tmp_path: Path):
    vault = VaultManager(vault_path=tmp_path)
    vault.initialize_vault_structure()
    mp = vault.create_meeting_note(
        summary="Rede XYZ", start_time=datetime(2026, 9, 5, 10), end_time=datetime(2026, 9, 5, 11),
        attendees=["Rafael Moreira"], context_brief="brief",
    )
    digest = "a" * 64
    wrote1 = vault.append_transcript_minutes(
        mp, digest, title="Rede XYZ",
        summary_bullets=["Discutido orçamento", "Prazo acordado"],
        decisions=["Aprovar fase 2"],
        action_items=[{"text": "Enviar contrato", "owner": "Rafael", "due": "2026-09-10"}],
        source_link="[[07_Inbox_Agent/Transcricoes/Processado/xyz.txt]]", meeting_date="2026-09-05",
    )
    body1 = mp.read_text(encoding="utf-8")
    wrote2 = vault.append_transcript_minutes(mp, digest, title="Rede XYZ", summary_bullets=["x"])
    body2 = mp.read_text(encoding="utf-8")

    assert wrote1 is True and wrote2 is False
    assert body1 == body2
    assert body1.count("<!-- transcript:" + digest[:12] + " -->") == 1
    assert "- [ ] Enviar contrato — Rafael (prazo: 2026-09-10)" in body1
    assert "Aprovar fase 2" in body1
    # everything sits below the human marker so a calendar re-sync preserves it
    assert body1.index(vault._MEETING_HUMAN_MARKER) < body1.index("Ata da transcrição")


def test_append_transcript_survives_meeting_note_resync(tmp_path: Path):
    vault = VaultManager(vault_path=tmp_path)
    vault.initialize_vault_structure()
    start = datetime(2026, 9, 5, 10)
    mp = vault.create_meeting_note(summary="Comitê X", start_time=start, end_time=start,
                                   attendees=["A"], context_brief="brief antigo", event_id="evt-1")
    vault.append_transcript_minutes(mp, "b" * 64, title="Comitê X", summary_bullets=["ponto importante"])

    vault.create_meeting_note(summary="Comitê X", start_time=start, end_time=start,
                              attendees=["A", "B"], context_brief="brief NOVO", event_id="evt-1")
    body = mp.read_text(encoding="utf-8")
    assert "brief NOVO" in body
    assert "ponto importante" in body


def test_add_cockpit_focus_dedupes(tmp_path: Path):
    vault = VaultManager(vault_path=tmp_path)
    vault.initialize_vault_structure()
    d = date(2026, 9, 6)
    vault.add_cockpit_focus(d, "🗣️ Fechar SOW", "Follow-up de reunião", "De: Kickoff")
    vault.add_cockpit_focus(d, "🗣️ Fechar SOW", "Follow-up de reunião", "De: Kickoff again")
    note = (tmp_path / "00_Cockpit" / "Daily" / "2026-09-06.md").read_text(encoding="utf-8")
    assert note.count("Fechar SOW") == 1
    assert "- [ ] **🗣️ Fechar SOW** (Follow-up de reunião) — De: Kickoff" in note


def test_add_cockpit_focus_prefix_labels_stay_distinct(tmp_path: Path):
    vault = VaultManager(vault_path=tmp_path)
    vault.initialize_vault_structure()
    d = date(2026, 9, 6)
    vault.add_cockpit_focus(d, "🗣️ Enviar SOW", "Follow-up de reunião", "De: X")
    vault.add_cockpit_focus(d, "🗣️ Enviar SOW assinado", "Follow-up de reunião", "De: X")
    note = (tmp_path / "00_Cockpit" / "Daily" / "2026-09-06.md").read_text(encoding="utf-8")
    assert "Enviar SOW**" in note
    assert "Enviar SOW assinado**" in note


def test_add_cockpit_focus_not_suppressed_by_substring_elsewhere(tmp_path: Path):
    vault = VaultManager(vault_path=tmp_path)
    vault.initialize_vault_structure()
    d = date(2026, 9, 6)
    cockpit = tmp_path / "00_Cockpit" / "Daily" / "2026-09-06.md"
    vault.create_daily_cockpit(target_date=d, actionable_emails=[{
        "sender": "chefe@x.com", "subject": "Enviar relatório trimestral hoje",
        "suggested_action": "responder", "priority": "alta",
    }])
    vault.add_cockpit_focus(d, "🗣️ Enviar relatório trimestral", "Follow-up de reunião", "De: Board")
    body = cockpit.read_text(encoding="utf-8")
    assert "**🗣️ Enviar relatório trimestral** (Follow-up de reunião)" in body


def test_transcript_focus_survives_daily_cockpit_regen(tmp_path: Path):
    vault = VaultManager(vault_path=tmp_path)
    vault.initialize_vault_structure()
    d = date(2026, 9, 6)
    vault.add_cockpit_focus(d, "🗣️ Assinar contrato fornecedor A", "Follow-up de reunião", "De: Kickoff")
    # a later run_daily_sync re-renders today's cockpit from the template
    vault.create_daily_cockpit(target_date=d, actionable_emails=[], pending_decisions=[])
    body = (tmp_path / "00_Cockpit" / "Daily" / "2026-09-06.md").read_text(encoding="utf-8")
    assert "Assinar contrato fornecedor A" in body


# --- rich extraction: _coerce_minutes / _parse_minutes_json --------------------

from rysos.ai.gemini import _coerce_minutes, _parse_minutes_json

_RICH_KEYS = {
    "title", "meeting_type", "date_hint", "language_quality_notes", "people", "organizations",
    "projects", "para_area", "para_area_rationale", "decisions", "open_questions", "action_items",
    "resources", "risks", "financials", "deadlines", "followups_meetings", "topics_tags",
    "glossary", "summary_bullets",
}


def _fake_rich(**overrides):
    """A sparse-but-valid rich minutes dict (missing keys prove _coerce_minutes fills them)."""
    base = {
        "title": "Kickoff Projeto Lia",
        "date_hint": "2026-09-05",
        "para_area": "Negocios_e_Governanca",
        "people": [
            {"name": "Rafael Moreira", "role": "Diretor", "org": "rysOS", "mentions": "Conduziu o kickoff."},
            {"name": "Maria Lima", "role": "", "org": "Fornecedor A", "mentions": "Fecha o contrato."},
        ],
        "projects": [{"name": "Projeto Lia", "status": "ativo", "summary": "Piloto com o fornecedor A"}],
        "decisions": [{"title": "Seguir com fornecedor A", "status": "decidido", "rationale": "Melhor preço", "owner": "Rafael"}],
        "action_items": [
            {"text": "Enviar SOW assinado", "owner": "Rafael", "due": "2026-09-09"},
            {"text": "Agendar follow-up", "owner": "Maria", "due": ""},
        ],
        "summary_bullets": ["Escopo alinhado", "Riscos mapeados"],
        "risks": ["Prazo apertado"],
        "resources": [{"type": "contrato", "name": "SOW Fornecedor A", "note": "assinar"}],
        "glossary": [{"term": "SOW", "meaning": "Statement of Work"}],
    }
    base.update(overrides)
    return _coerce_minutes(base, meeting_year=2026)


def test_coerce_minutes_fills_all_keys_from_partial():
    out = _coerce_minutes({"title": "X"}, meeting_year=2026)
    assert _RICH_KEYS.issubset(out.keys())
    assert out["title"] == "X"
    assert out["people"] == [] and out["decisions"] == [] and out["topics_tags"] == []


def test_coerce_minutes_bare_string_person_and_project():
    out = _coerce_minutes({"people": ["Nilton"], "projects": ["Acervo"]}, meeting_year=2026)
    assert out["people"] == [{"name": "Nilton", "role": "", "org": "", "mentions": ""}]
    assert out["projects"][0]["name"] == "Acervo"


def test_coerce_minutes_strips_hallucinated_year():
    out = _coerce_minutes(
        {"date_hint": "2019-04-01",
         "action_items": [{"text": "x", "due": "2024-09-28"}],
         "deadlines": [{"what": "entrega", "when": "2030-01-01"}]},
        meeting_year=2026,
    )
    assert out["date_hint"] == ""
    assert out["action_items"][0]["due"] == ""
    assert out["deadlines"][0]["when"] == ""


def test_coerce_minutes_keeps_human_deadline_text():
    out = _coerce_minutes({"deadlines": [{"what": "entrega", "when": "28 de setembro"}]}, meeting_year=2026)
    assert out["deadlines"][0]["when"] == "28 de setembro"


def test_parse_minutes_json_repairs_trailing_comma_and_bare_key():
    d, r = _parse_minutes_json('{"title": "X", "people": [],}')
    assert r == "repaired" and d["title"] == "X"
    d, r = _parse_minutes_json('{title: "X"}')
    assert r == "repaired" and d["title"] == "X"


def test_parse_minutes_json_detects_truncation():
    d, r = _parse_minutes_json('{"title": "X", "people": [{"name": "Rod')
    assert d is None and r == "truncated"


def test_parse_minutes_json_strips_fence_and_prose():
    d, r = _parse_minutes_json('Segue:\n```json\n{"title": "Y"}\n```')
    assert r == "ok" and d["title"] == "Y"


class _FakeResp:
    def __init__(self, text):
        self.text = text
        self.usage_metadata = None


def test_summarize_retries_once_on_truncation(monkeypatch):
    from rysos.ai import gemini as gmod
    calls = []

    class _Models:
        def generate_content(self, **kw):
            calls.append(kw["contents"])
            if len(calls) == 1:
                return _FakeResp('{"title": "Reunião", "people": [{"name": "Rod')  # truncated
            return _FakeResp('{"title": "Reunião", "people": [{"name": "Rafael"}]}')

    client = type("C", (), {"models": _Models()})()
    monkeypatch.setattr(gmod.gemini_ai, "_client", client, raising=False)
    monkeypatch.setattr(gmod.gemini_ai, "api_key", "x" * 12)
    out = gmod.gemini_ai.summarize_meeting_transcript("uma reunião real " * 40, "r.txt",
                                                      mtime_date=date(2026, 9, 5), today=date(2026, 9, 6))
    assert len(calls) == 2
    assert out["title"] == "Reunião" and out["people"][0]["name"] == "Rafael"


# --- end-to-end pipeline ---------------------------------------------------------

def _patch_ai(monkeypatch, rich):
    monkeypatch.setattr(
        rysos_core.ai, "summarize_meeting_transcript",
        lambda text, filename="", *, mtime_date=None, today=None: dict(rich),
    )


async def _candidates(sha=None):
    from rysos.db import EntityCandidate
    from sqlalchemy import select
    async with get_db_session() as session:
        stmt = select(EntityCandidate)
        if sha:
            stmt = stmt.where(EntityCandidate.transcript_sha == sha)
        return (await session.execute(stmt)).scalars().all()


async def test_pipeline_files_transcript_and_is_idempotent(tmp_path, monkeypatch):
    vault = rysos_core.vault  # isolated by conftest
    inbox = vault.vault_path / "07_Inbox_Agent" / "Transcricoes"
    inbox.mkdir(parents=True, exist_ok=True)
    src = inbox / "kickoff-maria 2026-09-05.txt"
    src.write_text(_body("kickoff maria"), encoding="utf-8")
    _patch_ai(monkeypatch, _fake_rich())

    res = await rysos_core.process_pending_transcripts(target_date=date(2026, 9, 6))
    assert res["processed"] == 1

    note = vault.vault_path / "04_Meetings" / "2026-09-05 - Kickoff Projeto Lia.md"
    assert note.exists()
    body = note.read_text(encoding="utf-8")
    assert "Escopo alinhado" in body
    assert "Seguir com fornecedor A" in body
    assert "- [ ] Enviar SOW assinado — Rafael (prazo: 2026-09-09)" in body

    # CANARY: attendees are NOT auto-created as Person notes — they become pending candidates
    assert not (vault.vault_path / "05_People" / "Rafael Moreira.md").exists()
    assert not (vault.vault_path / "05_People" / "Maria Lima.md").exists()
    cands = await _candidates()
    kinds = {(c.kind, c.normalized_form) for c in cands}
    assert ("person", "rafael moreira") in kinds
    assert ("person", "maria lima") in kinds
    assert all(c.status == "pending" for c in cands)

    # rich block dimensions rendered in the note body
    for token in ("Participantes citados", "Área sugerida", "Projetos citados",
                  "Decisões discutidas", "requer validação", "não confirmado", "Glossário"):
        assert token in body

    # a Triagem note mirrors the pending candidates
    triagem = vault.vault_path / "07_Inbox_Agent" / "Triagem" / "2026-09-05 - Kickoff Projeto Lia.md"
    assert triagem.exists()
    assert "Rafael Moreira" in triagem.read_text(encoding="utf-8")

    # action items land on the day they're actually due: "Enviar SOW assinado" (due
    # 2026-09-09) routes to that day's cockpit, not today's; "Agendar follow-up" has no
    # resolvable due date, so it stays on today's (2026-09-06) cockpit.
    cockpit_today = (vault.vault_path / "00_Cockpit" / "Daily" / "2026-09-06.md").read_text(encoding="utf-8")
    assert "Agendar follow-up" in cockpit_today
    assert "Enviar SOW assinado" not in cockpit_today
    cockpit_due = (vault.vault_path / "00_Cockpit" / "Daily" / "2026-09-09.md").read_text(encoding="utf-8")
    assert "Enviar SOW assinado" in cockpit_due

    assert not src.exists()
    assert (inbox / "Processado" / "kickoff-maria 2026-09-05.txt").exists()

    async with get_db_session() as session:
        from sqlalchemy import select
        rows = (await session.execute(select(ProcessedTranscript))).scalars().all()
    assert len(rows) == 1
    assert rows[0].action_items_count == 2
    assert rows[0].decisions_count == 1
    assert rows[0].candidates_count == len(cands)
    assert rows[0].area == "Negocios_e_Governanca"

    # re-drop identical content -> no reprocessing, no second block, no duplicate candidates
    (inbox / "kickoff-maria-copy.txt").write_text(_body("kickoff maria"), encoding="utf-8")
    res2 = await rysos_core.process_pending_transcripts(target_date=date(2026, 9, 6))
    assert res2["processed"] == 0
    assert note.read_text(encoding="utf-8").count("### 📄 Ata da transcrição:") == 1
    assert len(await _candidates()) == len(cands)


async def test_pipeline_auto_links_exact_existing_person(tmp_path, monkeypatch):
    vault = rysos_core.vault
    vault.create_person_note(name="Rafael Moreira", role="Diretor", organization="rysOS")
    inbox = vault.vault_path / "07_Inbox_Agent" / "Transcricoes"
    inbox.mkdir(parents=True, exist_ok=True)
    (inbox / "call 2026-09-05.txt").write_text(_body("call"), encoding="utf-8")
    _patch_ai(monkeypatch, _fake_rich(people=[{"name": "Rafael Moreira", "role": "Diretor", "org": "rysOS", "mentions": "abriu"}]))

    await rysos_core.process_pending_transcripts(target_date=date(2026, 9, 6))
    cands = await _candidates()
    assert not any(c.kind == "person" and c.normalized_form == "rafael moreira" for c in cands)
    note = next((vault.vault_path / "04_Meetings").glob("*.md"))
    assert "[[05_People/Rafael Moreira|Rafael Moreira]]" in note.read_text(encoding="utf-8")


async def test_pipeline_salience_filter_drops_one_shot_names_but_ata_keeps_them(tmp_path, monkeypatch):
    vault = rysos_core.vault
    inbox = vault.vault_path / "07_Inbox_Agent" / "Transcricoes"
    inbox.mkdir(parents=True, exist_ok=True)
    (inbox / "call 2026-09-05.txt").write_text(_body("call"), encoding="utf-8")
    _patch_ai(monkeypatch, _fake_rich(people=[
        {"name": "Rafael Moreira", "role": "Diretor", "org": "rysOS", "mentions": "conduziu"},
        {"name": "Heit", "role": "", "org": "", "mentions": "citado uma vez"},  # ASR-noise shape
    ]))

    await rysos_core.process_pending_transcripts(target_date=date(2026, 9, 6))

    cands = await _candidates()
    norms = {c.normalized_form for c in cands if c.kind == "person"}
    assert "rafael moreira" in norms
    assert "saka" not in norms                       # dropped, no candidate row

    body = next((vault.vault_path / "04_Meetings").glob("*.md")).read_text(encoding="utf-8")
    assert "Heit" in body                            # still rendered in the ata
    # the dropped name renders plain — no ⚠️ tag on its line
    saka_line = next(ln for ln in body.splitlines() if "Heit" in ln and ln.lstrip().startswith("-"))
    assert "não confirmado" not in saka_line


async def test_pipeline_flags_unconfirmed_meeting_date(tmp_path, monkeypatch):
    vault = rysos_core.vault
    inbox = vault.vault_path / "07_Inbox_Agent" / "Transcricoes"
    inbox.mkdir(parents=True, exist_ok=True)
    (inbox / "sem-data.txt").write_text(_body("sem data"), encoding="utf-8")  # no date in name
    _patch_ai(monkeypatch, _fake_rich(date_hint=""))  # and none in transcript

    res = await rysos_core.process_pending_transcripts(target_date=date(2026, 9, 6))
    assert res["notes"][0]["date_source"] == "mtime"
    cands = await _candidates()
    assert any(c.kind == "meeting_date" for c in cands)


async def test_pipeline_rich_block_survives_calendar_resync(tmp_path, monkeypatch):
    vault = rysos_core.vault
    inbox = vault.vault_path / "07_Inbox_Agent" / "Transcricoes"
    inbox.mkdir(parents=True, exist_ok=True)
    (inbox / "kickoff-maria 2026-09-05.txt").write_text(_body("kickoff"), encoding="utf-8")
    _patch_ai(monkeypatch, _fake_rich())
    await rysos_core.process_pending_transcripts(target_date=date(2026, 9, 6))

    note = vault.vault_path / "04_Meetings" / "2026-09-05 - Kickoff Projeto Lia.md"
    vault.create_meeting_note(
        summary="Kickoff Projeto Lia",
        start_time=datetime(2026, 9, 5, 14, 0), end_time=datetime(2026, 9, 5, 15, 0),
        attendees=["Rafael Moreira"], context_brief="Brief do calendário NOVO", event_id="evt-1",
    )
    body = note.read_text(encoding="utf-8")
    assert "Brief do calendário NOVO" in body
    assert "Escopo alinhado" in body and body.count("### 📄 Ata da transcrição:") == 1


async def test_pipeline_does_not_clobber_preexisting_calendar_note(tmp_path, monkeypatch):
    """Reverse of the resync test: the calendar note exists FIRST, then the transcript
    lands on a matching title. The transcript pass passes event_id=None, so
    _resolve_meeting_path adopts the calendar note — create_only=True must keep its
    frontmatter, attendees, meet_link and Brief intact and only splice the ata block."""
    vault = rysos_core.vault
    vault.create_meeting_note(
        summary="Kickoff Projeto Lia",
        start_time=datetime(2026, 9, 5, 14, 0), end_time=datetime(2026, 9, 5, 15, 0),
        attendees=["Rafael Moreira"], meet_link="https://meet.example/abc",
        context_brief="Brief real do calendario", event_id="evt-99",
    )
    note = vault.vault_path / "04_Meetings" / "2026-09-05 - Kickoff Projeto Lia.md"
    assert 'event_id: "evt-99"' in note.read_text(encoding="utf-8")

    inbox = vault.vault_path / "07_Inbox_Agent" / "Transcricoes"
    inbox.mkdir(parents=True, exist_ok=True)
    (inbox / "kickoff-maria 2026-09-05.txt").write_text(_body("kickoff maria"), encoding="utf-8")
    _patch_ai(monkeypatch, _fake_rich())

    res = await rysos_core.process_pending_transcripts(target_date=date(2026, 9, 6))
    assert res["processed"] == 1

    after = note.read_text(encoding="utf-8")
    assert 'event_id: "evt-99"' in after
    assert "Brief real do calendario" in after
    assert "https://meet.example/abc" in after
    assert "[[05_People/Rafael Moreira|Rafael Moreira]]" in after
    assert "### 📄 Ata da transcrição:" in after and "Escopo alinhado" in after
    assert len(list((vault.vault_path / "04_Meetings").glob("*.md"))) == 1


async def test_pipeline_degraded_extraction_is_surfaced(tmp_path, monkeypatch):
    """A transient model failure yields a `_degraded` skeleton. The file is still
    processed (bounded — re-dropping the same SHA is a no-op), but an
    `extraction_failed` candidate + a Triagem banner make the near-empty note loud
    instead of silent."""
    vault = rysos_core.vault
    inbox = vault.vault_path / "07_Inbox_Agent" / "Transcricoes"
    inbox.mkdir(parents=True, exist_ok=True)
    (inbox / "ruido 2026-09-05.txt").write_text(_body("ruido"), encoding="utf-8")
    rich = _fake_rich(people=[], decisions=[], action_items=[])
    rich["_degraded"] = True
    _patch_ai(monkeypatch, rich)

    res = await rysos_core.process_pending_transcripts(target_date=date(2026, 9, 6))
    assert res["processed"] == 1
    assert any(c.kind == "extraction_failed" for c in await _candidates())
    assert not (inbox / "ruido 2026-09-05.txt").exists()
    triagem = next((vault.vault_path / "07_Inbox_Agent" / "Triagem").glob("*.md"))
    assert "Extração degradada" in triagem.read_text(encoding="utf-8")


async def test_pipeline_no_gemini_still_files_raw(tmp_path, monkeypatch):
    vault = rysos_core.vault
    inbox = vault.vault_path / "07_Inbox_Agent" / "Transcricoes"
    inbox.mkdir(parents=True, exist_ok=True)
    (inbox / "call sem data.txt").write_text(_body("call sem data"), encoding="utf-8")

    # simulate Gemini offline -> skeleton (still the full 17-key shape via _coerce_minutes)
    monkeypatch.setattr(rysos_core.ai, "is_available", lambda: False)

    res = await rysos_core.process_pending_transcripts(target_date=date(2026, 9, 6))
    assert res["processed"] == 1
    meetings = list((vault.vault_path / "04_Meetings").glob("*.md"))
    assert len(meetings) == 1
    assert "call sem data" in meetings[0].name
    assert not (inbox / "call sem data.txt").exists()
    # skeleton has no people/projects/decisions/resources; only the mtime date flag row
    assert [c.kind for c in await _candidates()] == ["meeting_date"]
