"""Tests for the read-only vault integrity scan (rysos.vault.dedup).

Detection-only module: nothing here merges, archives, or deletes. Pure functions
(check_link_health, find_decision_duplicates) are exercised with in-memory data;
the vault-backed scans (people/projects/resource ids) use a real VaultManager
against tmp_path, matching the pattern used elsewhere in this suite.
"""

from pathlib import Path

from rysos.vault.dedup import (
    check_link_health,
    collect_vault_files,
    find_decision_duplicates,
    find_diary_backlink_candidates,
    find_people_duplicates,
    find_project_duplicates,
    find_resource_duplicates,
    find_resource_id_collisions,
    rewrite_wikilinks,
    scan_vault,
)
from rysos.vault.manager import VaultManager


def test_check_link_health_finds_orphan_and_phantom():
    files = [
        ("01_Projects/Alfa.md", "# Alfa\nLigado a [[Beta]]."),
        ("01_Projects/Beta.md", "# Beta\nSem links."),
        ("01_Projects/Gama.md", "# Gama\nMenciona [[Nota Inexistente]]."),
    ]
    orphans, phantoms = check_link_health(files)
    # Alfa links to Beta, so Beta is referenced; Alfa and Gama are never linked TO.
    assert "01_Projects/Alfa.md" in orphans
    assert "01_Projects/Gama.md" in orphans
    assert "01_Projects/Beta.md" not in orphans
    assert any(p["target"] == "Nota Inexistente" for p in phantoms)


def test_check_link_health_archive_and_cockpit_need_no_incoming_link_but_are_still_scanned():
    files = [
        ("08_Archive/Triagem/Old.md", "# Old\n[[Nada Aqui]]"),
        ("00_Cockpit/Daily/2026-09-16.md", "# Cockpit\n[[05_People/Rafael Moreira.md]]"),
        ("05_People/Rafael Moreira.md", "# Rafael"),
    ]
    orphans, phantoms = check_link_health(files)
    # Neither Archive nor Cockpit needs a backlink pointing to it to avoid "orphan".
    assert "08_Archive/Triagem/Old.md" not in orphans
    assert "00_Cockpit/Daily/2026-09-16.md" not in orphans
    # But a real phantom link inside one of them is still worth catching...
    assert any(p["target"] == "Nada Aqui" for p in phantoms)
    # ...and a real link from Cockpit still counts toward the target being "found".
    assert "05_People/Rafael Moreira.md" not in orphans


def test_check_link_health_ignores_templates_and_obsidian_config_entirely():
    files = [
        ("_templates/Person.md", "id: PERSON-{{ slug }}\n[[{{ placeholder }}]]"),
        (".obsidian/workspace.json", "{}"),
    ]
    orphans, phantoms = check_link_health(files)
    assert orphans == []
    assert phantoms == []


def test_check_link_health_resolves_explicit_md_extension_links():
    files = [
        ("05_People/Rafael Moreira.md", "# Rafael"),
        ("04_Meetings/Call.md", "Presente: [[05_People/Rafael Moreira.md]]."),
    ]
    orphans, phantoms = check_link_health(files)
    assert phantoms == []
    assert "05_People/Rafael Moreira.md" not in orphans


def test_check_link_health_resolves_attachment_links_by_full_filename():
    files = [
        ("04_Meetings/Call.md", "Transcript: [[07_Inbox_Agent/Transcricoes/Processado/nilton.txt]]"),
    ]
    all_paths = [
        "04_Meetings/Call.md",
        "07_Inbox_Agent/Transcricoes/Processado/nilton.txt",
    ]
    orphans, phantoms = check_link_health(files, all_paths)
    assert phantoms == []


def test_check_link_health_resolves_alias_and_heading_links():
    files = [
        ("05_People/Rafael Moreira.md", "# Rafael"),
        ("01_Projects/Alfa.md", "Contato: [[Rafael Moreira|Rafaelão]] via [[Rafael Moreira#Contato]]."),
    ]
    orphans, phantoms = check_link_health(files)
    assert phantoms == []
    assert "05_People/Rafael Moreira.md" not in orphans


def test_find_decision_duplicates_groups_by_normalized_area_and_title():
    decisions = [
        {"id": "DEC-001", "title": "Aprovar orçamento 2026", "area": "Financeiro"},
        {"id": "DEC-002", "title": "aprovar   ORÇAMENTO 2026", "area": "financeiro"},
        {"id": "DEC-003", "title": "Contratar consultoria", "area": "Financeiro"},
    ]
    dups = find_decision_duplicates(decisions)
    assert len(dups) == 1
    assert set(dups[0]["ids"]) == {"DEC-001", "DEC-002"}


def test_find_decision_duplicates_ignores_blank_titles():
    assert find_decision_duplicates([{"id": "DEC-001", "title": "", "area": "X"}]) == []


def test_find_resource_id_collisions(tmp_path: Path):
    vault = VaultManager(vault_path=tmp_path)
    vault.initialize_vault_structure()
    res_dir = tmp_path / "06_Resources" / "Leituras_e_Pesquisas"
    (res_dir / "A.md").write_text(
        "---\nid: RESOURCE-2026-DUP\ntitle: \"A\"\n---\n# A\n", encoding="utf-8"
    )
    (res_dir / "B.md").write_text(
        "---\nid: RESOURCE-2026-DUP\ntitle: \"B\"\n---\n# B\n", encoding="utf-8"
    )
    (res_dir / "C.md").write_text(
        "---\nid: RESOURCE-2026-UNIQUE\ntitle: \"C\"\n---\n# C\n", encoding="utf-8"
    )

    collisions = find_resource_id_collisions(vault)
    assert len(collisions) == 1
    assert collisions[0]["id"] == "RESOURCE-2026-DUP"
    assert set(collisions[0]["titles"]) == {"A", "B"}


def test_find_people_duplicates_flags_near_matches(tmp_path: Path):
    vault = VaultManager(vault_path=tmp_path)
    vault.initialize_vault_structure()
    vault.create_person_note(name="Marta")
    vault.create_person_note(name="Marta Duarte")
    vault.create_person_note(name="Carlos Nunes")

    pairs = find_people_duplicates(vault)
    names = {(p["a"], p["b"]) for p in pairs} | {(p["b"], p["a"]) for p in pairs}
    assert ("Marta", "Marta Duarte") in names
    assert not any("Carlos Nunes" in pair for pair in names)


def test_find_people_duplicates_rejects_shared_first_name_or_honorific(tmp_path: Path):
    """2026-09-17 /dedup false-positive incident: a shared honorific or a common first
    name alone must not score as a near-certain duplicate — these are 4 different
    people, not 2, and the old max(full_ratio, first_token_ratio) scoring flagged all
    of them at 1.00."""
    vault = VaultManager(vault_path=tmp_path)
    vault.initialize_vault_structure()
    vault.create_person_note(name="Dr. Jonas Pimentel")
    vault.create_person_note(name="Dr. Rafael Sampaio")
    vault.create_person_note(name="Gabriela Cortez")
    vault.create_person_note(name="Gabriela Guerra")

    pairs = find_people_duplicates(vault)
    names = {(p["a"], p["b"]) for p in pairs} | {(p["b"], p["a"]) for p in pairs}
    assert ("Dr. Jonas Pimentel", "Dr. Rafael Sampaio") not in names
    assert ("Gabriela Cortez", "Gabriela Guerra") not in names


def test_find_people_duplicates_flags_bare_first_name_against_fuller_record(tmp_path: Path):
    vault = VaultManager(vault_path=tmp_path)
    vault.initialize_vault_structure()
    vault.create_person_note(name="Gustavo Portela")
    vault.create_person_note(name="Gustavo")

    pairs = find_people_duplicates(vault)
    names = {(p["a"], p["b"]) for p in pairs} | {(p["b"], p["a"]) for p in pairs}
    assert ("Gustavo Portela", "Gustavo") in names


def test_jaro_winkler_matches_jellyfish_golden_values():
    """Cross-checked against jellyfish==1.2.1 bit-for-bit for these inputs — see
    VaultManager._jaro_winkler's docstring for why this is hand-rolled rather than a
    dependency."""
    jw = VaultManager._jaro_winkler
    assert jw("gabriela", "gabriela") == 1.0
    assert jw("balmo", "latluma") == 0.565079365079365
    assert jw("procedoa", "proceo") == 0.95
    assert jw("meridian", "meridiam") == 0.95


def test_find_project_duplicates_flags_near_matches(tmp_path: Path):
    vault = VaultManager(vault_path=tmp_path)
    vault.initialize_vault_structure()
    vault.create_project_note(title="Projeto Meridian")
    vault.create_project_note(title="Projeto Meridiam")  # typo, distinct file
    vault.create_project_note(title="Projeto Alfa")

    pairs = find_project_duplicates(vault, cutoff=0.9)
    names = {(p["a"], p["b"]) for p in pairs} | {(p["b"], p["a"]) for p in pairs}
    assert ("Projeto Meridian", "Projeto Meridiam") in names
    assert not any("Projeto Alfa" in pair for pair in names)


def test_find_resource_duplicates_flags_near_matches(tmp_path: Path):
    vault = VaultManager(vault_path=tmp_path)
    vault.initialize_vault_structure()
    vault.create_resource_note(title="Contrato XYZ")
    vault.create_resource_note(title="Minuta Contratual XYZ")
    vault.create_resource_note(title="Business Plan Nimbus")

    pairs = find_resource_duplicates(vault, cutoff=0.6)
    names = {(p["a"], p["b"]) for p in pairs} | {(p["b"], p["a"]) for p in pairs}
    assert ("Contrato XYZ", "Minuta Contratual XYZ") in names
    assert not any("Business Plan Nimbus" in pair for pair in names)


def test_find_resource_duplicates_rejects_shared_generic_word(tmp_path: Path):
    """2026-09-17 /dedup false-positive incident: two different companies' products
    sharing only the generic word "Plataforma" must not match, even though the raw
    whole-string ratio (~0.70) clears a loose cutoff."""
    vault = VaultManager(vault_path=tmp_path)
    vault.initialize_vault_structure()
    vault.create_resource_note(title="Plataforma Meridian")
    vault.create_resource_note(title="Plataforma iHealth")

    pairs = find_resource_duplicates(vault, cutoff=0.6)
    names = {(p["a"], p["b"]) for p in pairs} | {(p["b"], p["a"]) for p in pairs}
    assert ("Plataforma Meridian", "Plataforma iHealth") not in names


def test_find_diary_backlink_candidates_flags_plain_text_mention(tmp_path: Path):
    """Replicates this session's manual fix (Heitor Sakamoto / Vinicius Oliveira,
    2026-09-16): a 05_People note with no incoming wikilink, whose name appears as
    plain text inside a Cockpit Daily's Notas Livres free-text block."""
    vault = VaultManager(vault_path=tmp_path)
    vault.initialize_vault_structure()
    vault.create_person_note(name="Heitor Sakamoto")

    daily_dir = tmp_path / "00_Cockpit" / "Daily"
    daily_dir.mkdir(parents=True, exist_ok=True)
    (daily_dir / "2026-09-16.md").write_text(
        "# Cockpit 2026-09-16\n\n"
        "<!-- notas-livres:start -->\n"
        "Conversei com Heitor Sakamoto sobre o roadmap.\n"
        "<!-- notas-livres:end -->\n",
        encoding="utf-8",
    )

    md_files, _all_paths = collect_vault_files(vault)
    orphans = ["05_People/Heitor Sakamoto.md"]
    candidates = find_diary_backlink_candidates(vault, md_files, orphans)
    assert len(candidates) == 1
    assert candidates[0]["orphan_path"] == "05_People/Heitor Sakamoto.md"
    assert len(candidates[0]["mentions"]) == 1
    assert candidates[0]["mentions"][0]["cockpit_date"] == "2026-09-16"
    assert "Heitor Sakamoto" in candidates[0]["mentions"][0]["matched_text"]


def test_find_diary_backlink_candidates_groups_multiple_mentions_per_orphan(tmp_path: Path):
    vault = VaultManager(vault_path=tmp_path)
    vault.initialize_vault_structure()
    vault.create_person_note(name="Heitor Sakamoto")

    daily_dir = tmp_path / "00_Cockpit" / "Daily"
    daily_dir.mkdir(parents=True, exist_ok=True)
    for d in ("2026-09-15", "2026-09-16"):
        (daily_dir / f"{d}.md").write_text(
            "<!-- notas-livres:start -->\nFalei com Heitor Sakamoto de novo.\n"
            "<!-- notas-livres:end -->\n",
            encoding="utf-8",
        )

    md_files, _all_paths = collect_vault_files(vault)
    candidates = find_diary_backlink_candidates(
        vault, md_files, ["05_People/Heitor Sakamoto.md"]
    )
    assert len(candidates) == 1
    assert len(candidates[0]["mentions"]) == 2


def test_find_diary_backlink_candidates_ignores_non_orphan_and_no_mention(tmp_path: Path):
    vault = VaultManager(vault_path=tmp_path)
    vault.initialize_vault_structure()

    daily_dir = tmp_path / "00_Cockpit" / "Daily"
    daily_dir.mkdir(parents=True, exist_ok=True)
    (daily_dir / "2026-09-16.md").write_text(
        "<!-- notas-livres:start -->\nNada relevante hoje.\n<!-- notas-livres:end -->\n",
        encoding="utf-8",
    )

    md_files, _all_paths = collect_vault_files(vault)
    assert find_diary_backlink_candidates(vault, md_files, []) == []
    assert find_diary_backlink_candidates(vault, md_files, ["05_People/Ninguem.md"]) == []


def test_find_diary_backlink_candidates_uses_word_boundaries(tmp_path: Path):
    """A short name must not match inside an unrelated longer word after normalization."""
    vault = VaultManager(vault_path=tmp_path)
    vault.initialize_vault_structure()
    vault.create_person_note(name="Ana")

    daily_dir = tmp_path / "00_Cockpit" / "Daily"
    daily_dir.mkdir(parents=True, exist_ok=True)
    (daily_dir / "2026-09-16.md").write_text(
        "<!-- notas-livres:start -->\nRevisei o planejamento anual.\n<!-- notas-livres:end -->\n",
        encoding="utf-8",
    )

    md_files, _all_paths = collect_vault_files(vault)
    candidates = find_diary_backlink_candidates(vault, md_files, ["05_People/Ana.md"])
    assert candidates == []


def test_scan_vault_reports_empty_when_clean(tmp_path: Path):
    # A dedicated subdir, not bare tmp_path: conftest's autouse isolation fixture
    # already initializes its own vault at tmp_path/"vault" for every test, and
    # scan_vault's rglob would otherwise pick up that nested, unrelated structure too.
    vault = VaultManager(vault_path=tmp_path / "myvault")
    vault.initialize_vault_structure()
    # A linked pair, not a lone note: a genuinely unreferenced note IS an orphan by
    # design (that's the point of the check) — a "clean" vault here means every note
    # is reachable from another, not that no notes exist.
    (vault.vault_path / "05_People" / "Pessoa Unica.md").write_text(
        "# Pessoa Unica\nProjeto: [[Projeto Teste]]\n", encoding="utf-8"
    )
    (vault.vault_path / "01_Projects" / "Projeto Teste.md").write_text(
        "# Projeto Teste\nStakeholder: [[Pessoa Unica]]\n", encoding="utf-8"
    )

    report = scan_vault(vault, decisions=[])
    assert report.is_empty()


def test_rewrite_wikilinks_repoints_bare_link():
    content = "Reunião com [[João Silva]] sobre o projeto."
    out = rewrite_wikilinks(content, old_stem="João Silva", new_stem="João da Silva")
    assert out == "Reunião com [[João da Silva]] sobre o projeto."


def test_rewrite_wikilinks_preserves_alias_and_heading():
    content = "Contato: [[João Silva|Joãozinho]] via [[João Silva#Contato]]."
    out = rewrite_wikilinks(content, old_stem="João Silva", new_stem="João da Silva")
    assert out == "Contato: [[João da Silva|Joãozinho]] via [[João da Silva#Contato]]."


def test_rewrite_wikilinks_preserves_folder_prefix():
    content = "Ver [[05_People/João Silva]]."
    out = rewrite_wikilinks(content, old_stem="João Silva", new_stem="João da Silva")
    assert out == "Ver [[05_People/João da Silva]]."


def test_rewrite_wikilinks_leaves_unrelated_links_untouched():
    content = "[[João Silva]] e [[Maria Souza]]."
    out = rewrite_wikilinks(content, old_stem="João Silva", new_stem="João da Silva")
    assert out == "[[João da Silva]] e [[Maria Souza]]."


def test_rewrite_wikilinks_is_idempotent():
    content = "[[João Silva]]"
    once = rewrite_wikilinks(content, old_stem="João Silva", new_stem="João da Silva")
    twice = rewrite_wikilinks(once, old_stem="João Silva", new_stem="João da Silva")
    assert once == twice == "[[João da Silva]]"


def test_rewrite_wikilinks_resolves_literal_bracket_title():
    # Same case as the 1864409 phantom-link fix: a title containing `]` must still
    # resolve as a wikilink target and be repointable.
    content = "Ver [[2026-09-15 - Meridian [Estratégia]|ata]]."
    out = rewrite_wikilinks(
        content, old_stem="2026-09-15 - Meridian [Estratégia]", new_stem="2026-09-15 - Meridian Consolidado",
    )
    assert out == "Ver [[2026-09-15 - Meridian Consolidado|ata]]."
