"""Tests for connectors/entity_apply.py's resolution execution."""

from pathlib import Path

from rysos.connectors.entity_apply import apply_resolution, NEW
from rysos.db import EntityCandidate
from rysos.vault.manager import VaultManager


def _resource_candidate(tmp_path: Path) -> EntityCandidate:
    return EntityCandidate(
        transcript_sha="a" * 64,
        meeting_note_path=str(tmp_path / "00_Cockpit" / "Daily" / "2026-09-15.md"),
        kind="resource",
        surface_form="Artigo sobre biobancos",
        normalized_form="artigo sobre biobancos",
        context_sentence="Mencionado como leitura recomendada.",
    )


def _make_meeting_note(vault: VaultManager, name: str = "Reuniao Teste.md") -> Path:
    meeting_path = vault.vault_path / "04_Meetings" / name
    meeting_path.write_text("# Reunião Teste\n\n## Notas\n- algo\n", encoding="utf-8")
    return meeting_path


def test_apply_new_resource_creates_note(tmp_path: Path):
    vault = VaultManager(vault_path=tmp_path)
    vault.initialize_vault_structure()
    cand = _resource_candidate(tmp_path)

    res = apply_resolution(vault, cand, NEW)

    assert res.status == "confirmed_new"
    assert cand.resolved_to
    path = Path(cand.resolved_to)
    assert path.exists()
    assert path.parent.name == "Leituras_e_Pesquisas"
    content = path.read_text(encoding="utf-8")
    assert "type: resource" in content
    assert "Mencionado como leitura recomendada." in content


def test_apply_new_resource_links_back_from_the_meeting_note(tmp_path: Path):
    """Not just the resource citing its source meeting — the meeting must gain a
    link to the resource too, or the resource is only reachable from one direction
    (2026-09-17 /dedup investigation: every resource note was a graph orphan)."""
    vault = VaultManager(vault_path=tmp_path)
    vault.initialize_vault_structure()
    meeting_path = _make_meeting_note(vault)
    cand = EntityCandidate(
        transcript_sha="a" * 64,
        meeting_note_path=str(meeting_path),
        kind="resource",
        surface_form="Artigo sobre biobancos",
        normalized_form="artigo sobre biobancos",
        context_sentence="Mencionado como leitura recomendada.",
    )

    res = apply_resolution(vault, cand, NEW)

    assert res.status == "confirmed_new"
    resource_path = Path(cand.resolved_to)
    meeting_content = meeting_path.read_text(encoding="utf-8")
    expected_target = resource_path.relative_to(vault.vault_path).with_suffix("").as_posix()
    assert f"[[{expected_target}|Artigo sobre biobancos]]" in meeting_content


def test_apply_new_resource_reuses_existing_note_for_same_normalized_title(tmp_path: Path):
    """entity_apply's resource branch must call ensure_resource_note, not
    create_resource_note directly — otherwise the same real-world resource mentioned
    in two meetings under the same title (just different case) mints two notes
    (2026-09-17 XYZ contract dedup)."""
    vault = VaultManager(vault_path=tmp_path)
    vault.initialize_vault_structure()
    meeting_1 = _make_meeting_note(vault, "Reuniao Um.md")
    meeting_2 = _make_meeting_note(vault, "Reuniao Dois.md")
    cand_1 = EntityCandidate(
        transcript_sha="a" * 64,
        meeting_note_path=str(meeting_1),
        kind="resource",
        surface_form="Artigo sobre biobancos",
        normalized_form="artigo sobre biobancos",
        context_sentence="Primeira menção.",
    )
    cand_2 = EntityCandidate(
        transcript_sha="b" * 64,
        meeting_note_path=str(meeting_2),
        kind="resource",
        surface_form="artigo sobre biobancos",
        normalized_form="artigo sobre biobancos",
        context_sentence="Segunda menção, mesmo recurso.",
    )

    res_1 = apply_resolution(vault, cand_1, NEW)
    res_2 = apply_resolution(vault, cand_2, NEW)

    assert res_1.status == res_2.status == "confirmed_new"
    assert cand_1.resolved_to == cand_2.resolved_to
    resources_dir = tmp_path / "06_Resources" / "Leituras_e_Pesquisas"
    assert len(list(resources_dir.glob("*.md"))) == 1


def test_apply_new_decision_links_back_from_the_meeting_note(tmp_path: Path):
    vault = VaultManager(vault_path=tmp_path)
    vault.initialize_vault_structure()
    meeting_path = _make_meeting_note(vault)
    cand = EntityCandidate(
        transcript_sha="a" * 64,
        meeting_note_path=str(meeting_path),
        kind="decision",
        surface_form="Aprovar novo orçamento",
        normalized_form="aprovar novo orcamento",
        context_sentence="Decidido em reunião.",
    )

    res = apply_resolution(vault, cand, NEW)

    assert res.status == "confirmed_new"
    decision_path = Path(res.decision_row["note_path"])
    meeting_content = meeting_path.read_text(encoding="utf-8")
    expected_target = decision_path.relative_to(vault.vault_path).with_suffix("").as_posix()
    assert f"[[{expected_target}|Aprovar novo orçamento]]" in meeting_content


def test_append_meeting_generated_link_is_idempotent_and_reuses_heading(tmp_path: Path):
    vault = VaultManager(vault_path=tmp_path)
    vault.initialize_vault_structure()
    meeting_path = _make_meeting_note(vault)

    changed_1 = vault.append_meeting_generated_link(meeting_path, "06_Resources/A", "A")
    changed_2 = vault.append_meeting_generated_link(meeting_path, "06_Resources/B", "B")
    changed_3 = vault.append_meeting_generated_link(meeting_path, "06_Resources/A", "A")

    assert changed_1 is True
    assert changed_2 is True
    assert changed_3 is False  # idempotent: same target, no duplicate line

    content = meeting_path.read_text(encoding="utf-8")
    assert content.count("[[06_Resources/A|A]]") == 1
    assert content.count("[[06_Resources/B|B]]") == 1
    assert content.count(VaultManager._MEETING_GENERATED_HEADING) == 1
