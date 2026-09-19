"""The ata's summary lands in `## 📝 Notas & Discussão` only while that section is empty."""

from datetime import datetime

from rysos.ai.gemini import _coerce_minutes
from rysos.core import rysos_core

MARKER = "## 📝 Notas & Discussão"


def _meeting():
    vault = rysos_core.vault
    vault.initialize_vault_structure()
    return vault, vault.create_meeting_note(
        summary="Alinhamento", start_time=datetime(2026, 9, 5, 10), end_time=datetime(2026, 9, 5, 11),
        attendees=[], context_brief="brief",
    )


def _rich(*bullets):
    return _coerce_minutes({"title": "Alinhamento", "summary_bullets": list(bullets)}, meeting_year=2026)


def _section(content: str) -> str:
    return content.split(MARKER)[1].split("\n---")[0]


def test_summary_fills_the_empty_notes_section(tmp_path):
    vault, mp = _meeting()
    assert vault.append_transcript_minutes(mp, "a" * 64, title="A", rich=_rich("Fechamos o escopo.", "Prazo em outubro."))
    content = mp.read_text(encoding="utf-8")
    assert _section(content).strip().splitlines() == ["- Fechamos o escopo.", "- Prazo em outubro."]
    assert "## ✅ Ações & Follow-ups Decididos" in content and "**Resumo:**" in content  # rest untouched


def test_hand_typed_notes_and_a_second_transcript_are_never_overwritten(tmp_path):
    vault, mp = _meeting()
    mp.write_text(mp.read_text(encoding="utf-8").replace(f"{MARKER}\n- ", f"{MARKER}\nminha anotação"), encoding="utf-8")
    vault.append_transcript_minutes(mp, "a" * 64, title="A", rich=_rich("Resumo da máquina."))
    assert _section(mp.read_text(encoding="utf-8")).strip() == "minha anotação"

    vault2, mp2 = _meeting()
    mp2 = vault2.create_meeting_note(
        summary="Outra", start_time=datetime(2026, 9, 6, 10), end_time=datetime(2026, 9, 6, 11),
        attendees=[], context_brief="brief",
    )
    vault2.append_transcript_minutes(mp2, "b" * 64, title="A", rich=_rich("Primeiro resumo."))
    vault2.append_transcript_minutes(mp2, "c" * 64, title="B", rich=_rich("Segundo resumo."))
    assert _section(mp2.read_text(encoding="utf-8")).strip() == "- Primeiro resumo."


def test_no_summary_bullets_leaves_the_placeholder(tmp_path):
    vault, mp = _meeting()
    vault.append_transcript_minutes(mp, "a" * 64, title="A", rich=_rich())
    assert _section(mp.read_text(encoding="utf-8")).strip() == "-"
