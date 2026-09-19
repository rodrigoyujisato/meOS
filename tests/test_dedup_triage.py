"""Tests for rysos.vault.dedup_triage — read-only context assembly for a /dedup finding."""

from pathlib import Path

from rysos.vault.dedup import find_backlinks
from rysos.vault.dedup_triage import build_finding_context
from rysos.vault.manager import VaultManager


def test_find_backlinks_preserves_alias_and_ignores_other_targets(tmp_path: Path):
    vault = VaultManager(vault_path=tmp_path)
    vault.initialize_vault_structure()
    vault.create_person_note(name="Marta Duarte")
    vault.create_person_note(name="Carlos Nunes")

    meetings_dir = tmp_path / "04_Meetings"
    meetings_dir.mkdir(parents=True, exist_ok=True)
    (meetings_dir / "Reuniao A.md").write_text(
        "Participante: [[05_People/Marta Duarte|Marta]]", encoding="utf-8"
    )
    (meetings_dir / "Reuniao B.md").write_text(
        "Participante: [[05_People/Carlos Nunes]]", encoding="utf-8"
    )

    hits = find_backlinks(vault, "05_People/Marta Duarte.md")
    assert len(hits) == 1
    assert hits[0]["source_path"] == "04_Meetings/Reuniao A.md"
    assert hits[0]["alias"] == "Marta"


def test_build_finding_context_people(tmp_path: Path):
    vault = VaultManager(vault_path=tmp_path)
    vault.initialize_vault_structure()
    vault.create_person_note(name="Marta")
    vault.create_person_note(name="Marta Duarte")

    finding = {
        "a": "Marta", "a_path": str(tmp_path / "05_People" / "Marta.md"),
        "b": "Marta Duarte", "b_path": str(tmp_path / "05_People" / "Marta Duarte.md"),
        "score": 0.9,
    }
    ctx = build_finding_context(vault, "people", finding)
    assert ctx["kind"] == "people"
    assert "Marta" in ctx["a_content"]
    assert "Marta Duarte" in ctx["b_content"]
    assert ctx["a_backlinks"] == []
    assert ctx["score"] == 0.9


def test_build_finding_context_diary_candidate(tmp_path: Path):
    vault = VaultManager(vault_path=tmp_path)
    vault.initialize_vault_structure()
    vault.create_person_note(name="Heitor Sakamoto")

    finding = {
        "orphan_path": "05_People/Heitor Sakamoto.md",
        "orphan_name": "Heitor Sakamoto",
        "mentions": [{
            "cockpit_path": "00_Cockpit/Daily/2026-09-16.md",
            "cockpit_date": "2026-09-16",
            "matched_text": "Heitor Sakamoto",
        }],
    }
    ctx = build_finding_context(vault, "diary_candidates", finding)
    assert ctx["kind"] == "diary_candidates"
    assert "Heitor Sakamoto" in ctx["orphan_content"]
    assert ctx["mentions"][0]["cockpit_date"] == "2026-09-16"


def test_build_finding_context_normalizes_absolute_paths_to_relative(tmp_path: Path):
    """DedupReport findings for people/resources store a_path/b_path absolute (list_people/
    list_resources return str(file)) — build_finding_context must hand the AI/Telegram
    layer vault-relative paths, not a machine-local absolute path."""
    vault = VaultManager(vault_path=tmp_path)
    vault.initialize_vault_structure()
    vault.create_person_note(name="Marta")
    vault.create_person_note(name="Marta Duarte")

    finding = {
        "a": "Marta", "a_path": str(tmp_path / "05_People" / "Marta.md"),
        "b": "Marta Duarte", "b_path": str(tmp_path / "05_People" / "Marta Duarte.md"),
        "score": 0.9,
    }
    ctx = build_finding_context(vault, "people", finding)
    assert ctx["a_path"] == "05_People/Marta.md"
    assert ctx["b_path"] == "05_People/Marta Duarte.md"
    assert not ctx["a_path"].startswith("/")
