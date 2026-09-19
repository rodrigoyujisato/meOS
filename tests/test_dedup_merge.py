"""Tests for the /dedup merge primitives: VaultManager.merge_notes/repoint_backlinks
(the people/projects/resources/decisions note-pair shape) and wikify_diary_mention
(a different operation shape — a sentence edit, not a note-pair merge).
"""

from pathlib import Path

from rysos.vault.manager import VaultManager


def _vault(tmp_path: Path) -> VaultManager:
    vault = VaultManager(vault_path=tmp_path / "myvault")
    vault.initialize_vault_structure()
    return vault


def test_repoint_backlinks_rewrites_every_source_and_is_idempotent(tmp_path: Path):
    vault = _vault(tmp_path)
    (vault.vault_path / "05_People" / "João Silva.md").write_text("# João Silva\n", encoding="utf-8")
    (vault.vault_path / "05_People" / "João da Silva.md").write_text("# João da Silva\n", encoding="utf-8")
    (vault.vault_path / "01_Projects" / "Alfa.md").write_text(
        "Stakeholder: [[João Silva]].\n", encoding="utf-8"
    )
    (vault.vault_path / "01_Projects" / "Beta.md").write_text(
        "Contato: [[João Silva|Joãozinho]].\n", encoding="utf-8"
    )

    touched = vault.repoint_backlinks("05_People/João Silva.md", "05_People/João da Silva.md")
    assert set(touched) == {"01_Projects/Alfa.md", "01_Projects/Beta.md"}
    assert "[[João da Silva]]" in vault.read_file(vault.vault_path / "01_Projects" / "Alfa.md")
    assert "[[João da Silva|Joãozinho]]" in vault.read_file(vault.vault_path / "01_Projects" / "Beta.md")

    # Re-running finds nothing left pointing at the old note — idempotent.
    again = vault.repoint_backlinks("05_People/João Silva.md", "05_People/João da Silva.md")
    assert again == []


def test_merge_notes_folds_summary_repoints_and_archives_loser(tmp_path: Path):
    vault = _vault(tmp_path)
    survivor = vault.vault_path / "05_People" / "João da Silva.md"
    loser = vault.vault_path / "05_People" / "João Silva.md"
    survivor.write_text("# João da Silva\nid: p-1\n", encoding="utf-8")
    loser.write_text("# João Silva\nid: p-2\n", encoding="utf-8")
    (vault.vault_path / "01_Projects" / "Alfa.md").write_text(
        "Stakeholder: [[João Silva]].\n", encoding="utf-8"
    )

    result = vault.merge_notes(
        "people",
        survivor_path="05_People/João da Silva.md",
        loser_path="05_People/João Silva.md",
        consolidated_summary="mesma pessoa, grafias diferentes",
    )

    assert result["survivor_path"] == "05_People/João da Silva.md"
    assert result["archived_loser_path"] == str(vault.vault_path / "08_Archive" / "People" / "João Silva.md")
    assert result["repointed_sources"] == ["01_Projects/Alfa.md"]

    assert not loser.exists()
    archived_content = vault.read_file(vault.vault_path / "08_Archive" / "People" / "João Silva.md")
    assert "# João Silva" in archived_content  # loser note itself untouched, just moved

    survivor_content = vault.read_file(survivor)
    assert "mesma pessoa, grafias diferentes" in survivor_content
    assert '_Fundido com "João Silva"' in survivor_content

    project_content = vault.read_file(vault.vault_path / "01_Projects" / "Alfa.md")
    assert "[[João da Silva]]" in project_content


def test_merge_notes_retry_after_partial_failure_converges(tmp_path: Path):
    """Simulates a crash between the repoint step and the archive step (the [[Errno 5]]
    class of rclone hiccup): repoint already happened, archive didn't. Calling
    merge_notes again must not double-append the summary or fail on the already-repointed
    backlink, and must still complete the archive."""
    vault = _vault(tmp_path)
    survivor = vault.vault_path / "05_People" / "João da Silva.md"
    loser = vault.vault_path / "05_People" / "João Silva.md"
    survivor.write_text("# João da Silva\n", encoding="utf-8")
    loser.write_text("# João Silva\n", encoding="utf-8")
    (vault.vault_path / "01_Projects" / "Alfa.md").write_text(
        "Stakeholder: [[João Silva]].\n", encoding="utf-8"
    )

    # First call, but pretend the process died before the archive move by restoring the
    # loser file right after (repoint + summary fold already landed on disk for real).
    vault.merge_notes(
        "people", "05_People/João da Silva.md", "05_People/João Silva.md",
        consolidated_summary="dup",
    )
    archived = vault.vault_path / "08_Archive" / "People" / "João Silva.md"
    assert archived.exists()
    archived.replace(loser)  # simulate: archive step never happened

    result = vault.merge_notes(
        "people", "05_People/João da Silva.md", "05_People/João Silva.md",
        consolidated_summary="dup",
    )

    assert result["repointed_sources"] == []  # already repointed, nothing left to touch
    assert not loser.exists()
    survivor_content = vault.read_file(survivor)
    assert survivor_content.count('_Fundido com "João Silva"') == 1  # not double-appended


def test_merge_notes_rejects_unknown_kind(tmp_path: Path):
    vault = _vault(tmp_path)
    (vault.vault_path / "05_People" / "A.md").write_text("# A\n", encoding="utf-8")
    (vault.vault_path / "05_People" / "B.md").write_text("# B\n", encoding="utf-8")
    try:
        vault.merge_notes("diary_link", "05_People/A.md", "05_People/B.md")
        assert False, "expected ValueError"
    except ValueError:
        pass


def test_merge_notes_supports_decisions(tmp_path: Path):
    vault = _vault(tmp_path)
    survivor = vault.vault_path / "03_Decisions" / "DEC-2026-00001 - Aprovar orçamento.md"
    loser = vault.vault_path / "03_Decisions" / "DEC-2026-00002 - Aprovar orçamento.md"
    survivor.write_text('---\ntitle: "Aprovar orçamento"\n---\n', encoding="utf-8")
    loser.write_text('---\ntitle: "Aprovar orçamento"\n---\n', encoding="utf-8")
    (vault.vault_path / "00_Cockpit" / "Daily" / "2026-09-18.md").write_text(
        "Pendente: [[DEC-2026-00002 - Aprovar orçamento]].\n", encoding="utf-8"
    )

    result = vault.merge_notes(
        "decisions",
        survivor_path="03_Decisions/DEC-2026-00001 - Aprovar orçamento.md",
        loser_path="03_Decisions/DEC-2026-00002 - Aprovar orçamento.md",
        consolidated_summary="mesma decisão, extraída duas vezes",
    )

    assert result["archived_loser_path"] == str(
        vault.vault_path / "08_Archive" / "Decisions" / "DEC-2026-00002 - Aprovar orçamento.md"
    )
    assert not loser.exists()
    cockpit_content = vault.read_file(vault.vault_path / "00_Cockpit" / "Daily" / "2026-09-18.md")
    assert "[[DEC-2026-00001 - Aprovar orçamento]]" in cockpit_content


def _cockpit_with_free_text(vault: VaultManager, date_str: str, free_text: str) -> Path:
    path = vault.vault_path / "00_Cockpit" / "Daily" / f"{date_str}.md"
    path.write_text(
        "---\ntitle: Daily Cockpit\n---\n# Cockpit\n\n"
        "<!-- notas-livres:start -->\n" + free_text + "\n<!-- notas-livres:end -->\n",
        encoding="utf-8",
    )
    return path


def test_wikify_diary_mention_links_only_inside_the_free_text_block(tmp_path: Path):
    vault = _vault(tmp_path)
    (vault.vault_path / "05_People" / "Heitor Sakamoto.md").write_text("# Heitor Sakamoto\n", encoding="utf-8")
    _cockpit_with_free_text(vault, "2026-09-18", "Falei com Heitor Sakamoto sobre o projeto.")

    touched = vault.wikify_diary_mention(
        "05_People/Heitor Sakamoto.md",
        [{"cockpit_path": "00_Cockpit/Daily/2026-09-18.md", "cockpit_date": "2026-09-18", "matched_text": "Heitor Sakamoto"}],
    )

    assert touched == ["00_Cockpit/Daily/2026-09-18.md"]
    content = vault.read_file(vault.vault_path / "00_Cockpit" / "Daily" / "2026-09-18.md")
    assert "[[05_People/Heitor Sakamoto|Heitor Sakamoto]]" in content
    assert "title: Daily Cockpit" in content  # frontmatter outside the block untouched


def test_wikify_diary_mention_is_idempotent(tmp_path: Path):
    vault = _vault(tmp_path)
    (vault.vault_path / "05_People" / "Heitor Sakamoto.md").write_text("# Heitor Sakamoto\n", encoding="utf-8")
    _cockpit_with_free_text(vault, "2026-09-18", "Falei com Heitor Sakamoto sobre o projeto.")
    mentions = [{"cockpit_path": "00_Cockpit/Daily/2026-09-18.md", "cockpit_date": "2026-09-18", "matched_text": "Heitor Sakamoto"}]

    vault.wikify_diary_mention("05_People/Heitor Sakamoto.md", mentions)
    once = vault.read_file(vault.vault_path / "00_Cockpit" / "Daily" / "2026-09-18.md")

    touched_again = vault.wikify_diary_mention("05_People/Heitor Sakamoto.md", mentions)
    twice = vault.read_file(vault.vault_path / "00_Cockpit" / "Daily" / "2026-09-18.md")

    assert touched_again == []  # already linked — nothing left to do
    assert once == twice
    assert once.count("[[05_People/Heitor Sakamoto") == 1  # never double-wrapped


def test_wikify_diary_mention_skips_when_sample_text_gone(tmp_path: Path):
    vault = _vault(tmp_path)
    (vault.vault_path / "05_People" / "Heitor Sakamoto.md").write_text("# Heitor Sakamoto\n", encoding="utf-8")
    _cockpit_with_free_text(vault, "2026-09-18", "Nada relacionado aqui.")

    touched = vault.wikify_diary_mention(
        "05_People/Heitor Sakamoto.md",
        [{"cockpit_path": "00_Cockpit/Daily/2026-09-18.md", "cockpit_date": "2026-09-18", "matched_text": "Heitor Sakamoto"}],
    )

    assert touched == []
