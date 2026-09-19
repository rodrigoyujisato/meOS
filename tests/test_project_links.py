"""Tests for the project <-> meeting/decision/resource wikilink edges."""

from pathlib import Path

from rysos.vault.manager import VaultManager

LEGACY_PROJECT = """---
id: PROJ-1
title: "Meridian"
type: project
---

# 🚀 Projeto: Meridian

---

## 🧠 Registro de Contexto & Reuniões
```dataview
TABLE date, summary
FROM "04_Meetings"
WHERE contains(file.outlinks, this.file.link)
SORT file.name DESC
```

---

## ⚖️ Decisões Vinculadas
```dataview
TABLE status
FROM "03_Decisions"
WHERE contains(file.outlinks, this.file.link)
```
"""


def _vault(tmp_path: Path) -> VaultManager:
    vault = VaultManager(vault_path=tmp_path)
    vault.initialize_vault_structure()
    return vault


def test_new_project_note_has_static_sections_and_no_dataview(tmp_path: Path):
    vault = _vault(tmp_path)
    content = vault.create_project_note("Meridian").read_text(encoding="utf-8")
    assert "dataview" not in content
    for heading in vault._PROJECT_SECTION_HEADINGS.values():
        assert heading in content


def test_append_project_link_lands_inside_its_section(tmp_path: Path):
    vault = _vault(tmp_path)
    proj = vault.create_project_note("Meridian")

    assert vault.append_project_link(proj, "decision", "03_Decisions/DEC-1 - X", "X") is True
    assert vault.append_project_link(proj, "decision", "03_Decisions/DEC-1 - X", "X") is False
    assert vault.append_project_link(proj, "resource", "06_Resources/A/R", "R") is True

    content = proj.read_text(encoding="utf-8")
    dec = content.index(vault._PROJECT_SECTION_HEADINGS["decision"])
    res = content.index(vault._PROJECT_SECTION_HEADINGS["resource"])
    assert dec < content.index("[[03_Decisions/DEC-1 - X|X]]") < res
    assert content.index("[[06_Resources/A/R|R]]") > res


def test_append_project_link_replaces_dead_dataview_and_adds_missing_section(tmp_path: Path):
    vault = _vault(tmp_path)
    proj = tmp_path / "01_Projects" / "Meridian.md"
    proj.write_text(LEGACY_PROJECT, encoding="utf-8")

    vault.append_project_link(proj, "meeting", "04_Meetings/2026-09-15 - A", "A")
    vault.append_project_link(proj, "resource", "06_Resources/A/R", "R")

    content = proj.read_text(encoding="utf-8")
    assert content.count("```dataview") == 1  # only the untouched decisions block remains
    m_head = vault._PROJECT_SECTION_HEADINGS["meeting"]
    assert content.index(m_head) < content.index("[[04_Meetings/2026-09-15 - A|A]]") \
        < content.index(vault._PROJECT_SECTION_HEADINGS["decision"])
    assert content.rstrip().endswith("- [[06_Resources/A/R|R]]")  # heading created at EOF


def test_add_related_project_fills_decision_and_is_idempotent(tmp_path: Path):
    vault = _vault(tmp_path)
    note = vault.create_decision_note("DEC-1", "Algo")
    assert vault.add_related_project(note, "Meridian") is True
    assert vault.add_related_project(note, "Meridian") is False
    assert vault.add_related_project(note, "Zenith") is True
    content = note.read_text(encoding="utf-8")
    fm = content.split("---")[1]
    assert content.count("[[01_Projects/Meridian|Meridian]]") == 1
    assert "[[01_Projects/Zenith|Zenith]]" in fm
    assert fm.index("related_projects:") < fm.index("Meridian") < fm.index("decision_date:")


def test_add_related_project_handles_resource_and_legacy_note_without_key(tmp_path: Path):
    vault = _vault(tmp_path)
    res = vault.create_resource_note("Plataforma")
    assert vault.add_related_project(res, "Meridian") is True
    assert "[[01_Projects/Meridian|Meridian]]" in res.read_text(encoding="utf-8").split("---")[1]

    legacy = tmp_path / "06_Resources" / "Leituras_e_Pesquisas" / "Velha.md"
    legacy.write_text('---\nid: R-1\ntype: resource\ntags:\n  - x\n---\n\n# Velha\n', encoding="utf-8")
    assert vault.add_related_project(legacy, "Meridian") is True
    text = legacy.read_text(encoding="utf-8")
    assert 'related_projects:\n  - "[[01_Projects/Meridian|Meridian]]"\n---\n\n# Velha' in text


def test_add_related_project_leaves_no_frontmatter_alone(tmp_path: Path):
    vault = _vault(tmp_path)
    note = tmp_path / "03_Decisions" / "sem.md"
    note.write_text("# só corpo\n", encoding="utf-8")
    assert vault.add_related_project(note, "Meridian") is False
    assert note.read_text(encoding="utf-8") == "# só corpo\n"



def test_remove_helpers_undo_the_edges_and_leave_other_items(tmp_path: Path):
    vault = _vault(tmp_path)
    proj = vault.create_project_note("Meridian")
    note = vault.create_decision_note("DEC-1", "Algo")
    vault.add_related_project(note, "Meridian")
    vault.add_related_project(note, "Zenith")
    vault.append_project_link(proj, "decision", "03_Decisions/DEC-1", "Algo")
    vault.append_project_link(proj, "decision", "03_Decisions/DEC-2", "Outra")

    assert vault.remove_related_project(note, "Meridian") is True
    assert vault.remove_related_project(note, "Meridian") is False
    fm = note.read_text(encoding="utf-8").split("---")[1]
    assert "Meridian" not in fm and "[[01_Projects/Zenith|Zenith]]" in fm

    assert vault.remove_project_link(proj, "03_Decisions/DEC-1") is True
    assert vault.remove_project_link(proj, "03_Decisions/DEC-1") is False
    text = proj.read_text(encoding="utf-8")
    assert "DEC-1" not in text and "[[03_Decisions/DEC-2|Outra]]" in text
    assert vault.append_project_link(proj, "decision", "03_Decisions/DEC-1", "Algo") is True  # re-linkable
