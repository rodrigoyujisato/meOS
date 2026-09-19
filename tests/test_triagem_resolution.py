"""Tests for the 'NOVO =<nome>' custom-name grammar and the '=<nome exato>' link
overwrite behavior in the Triagem resolution flow."""

from pathlib import Path

from rysos.connectors.entity_apply import apply_resolution, LINK, NEW
from rysos.db import EntityCandidate
from rysos.vault.manager import VaultManager
from rysos.vault.triagem import read_triagem_resolutions


# --- parser grammar ----------------------------------------------------------

def test_read_resolutions_parses_novo_with_desired_name():
    text = (
        "<!-- triagem:abc123456789 -->\n"
        "- [ ] **Gisele**\n"
        "      resolução: NOVO =Gisele Andrade e Silva Palma\n"
    )
    _, out = read_triagem_resolutions(text)
    assert out == [{
        "surface": "Gisele", "action": "new", "target": None,
        "desired_name": "Gisele Andrade e Silva Palma",
    }]


def test_read_resolutions_parses_registrar_and_criar_with_name():
    text = (
        "<!-- triagem:abc123456789 -->\n"
        "- [ ] **decisao x**\n"
        "      resolução: REGISTRAR =Decisão sobre o modelo societário\n"
        "- [ ] **recurso y**\n"
        "      resolução: CRIAR =Artigo sobre biobancos\n"
    )
    _, out = read_triagem_resolutions(text)
    assert {r["surface"]: r["desired_name"] for r in out} == {
        "decisao x": "Decisão sobre o modelo societário",
        "recurso y": "Artigo sobre biobancos",
    }


def test_read_resolutions_bare_novo_still_works_without_name():
    text = (
        "<!-- triagem:abc123456789 -->\n"
        "- [ ] **Fulano**\n"
        "      resolução: NOVO\n"
    )
    _, out = read_triagem_resolutions(text)
    assert out == [{"surface": "Fulano", "action": "new", "target": None}]


def test_read_resolutions_link_syntax_unaffected():
    text = (
        "<!-- triagem:abc123456789 -->\n"
        "- [ ] **Fulano**\n"
        "      resolução: =Fulano de Tal\n"
    )
    _, out = read_triagem_resolutions(text)
    assert out == [{"surface": "Fulano", "action": "link", "target": "Fulano de Tal"}]


# --- apply_resolution: desired_name overrides the note title -----------------

def _cand(kind: str, surface: str, payload_json: str = "{}") -> EntityCandidate:
    return EntityCandidate(
        transcript_sha="b" * 64,
        meeting_note_path="/tmp/does-not-need-to-exist.md",
        kind=kind, surface_form=surface, normalized_form=surface.lower(),
        payload_json=payload_json,
    )


def test_apply_new_person_uses_desired_name(tmp_path: Path):
    vault = VaultManager(vault_path=tmp_path)
    vault.initialize_vault_structure()
    cand = _cand("person", "Gisele")

    res = apply_resolution(vault, cand, NEW, desired_name="Gisele Andrade e Silva Palma")

    assert res.status == "confirmed_new"
    path = tmp_path / "05_People" / "Gisele Andrade e Silva Palma.md"
    assert path.exists()
    assert not (tmp_path / "05_People" / "Gisele.md").exists()


def test_apply_new_without_desired_name_falls_back_to_surface_form(tmp_path: Path):
    vault = VaultManager(vault_path=tmp_path)
    vault.initialize_vault_structure()
    cand = _cand("project", "Projeto Altamar")

    apply_resolution(vault, cand, NEW)

    assert (tmp_path / "01_Projects" / "Projeto Altamar.md").exists()


def test_apply_new_resource_uses_desired_name(tmp_path: Path):
    vault = VaultManager(vault_path=tmp_path)
    vault.initialize_vault_structure()
    cand = _cand("resource", "artigo x")

    apply_resolution(vault, cand, NEW, desired_name="Artigo sobre biobancos")

    assert (tmp_path / "06_Resources" / "Leituras_e_Pesquisas" / "Artigo sobre biobancos.md").exists()


# --- apply_resolution LINK: overwrite flag -----------------------------------

def test_apply_link_default_does_not_overwrite_existing_role(tmp_path: Path):
    vault = VaultManager(vault_path=tmp_path)
    vault.initialize_vault_structure()
    vault.create_person_note(name="Ana Reis", role="CEO", organization="Acme")
    cand = _cand("person", "Ana Reis", payload_json='{"role": "Diretora", "org": "Acme"}')
    match = {"name": "Ana Reis", "path": str(tmp_path / "05_People" / "Ana Reis.md")}

    apply_resolution(vault, cand, LINK, target=match)  # overwrite defaults to False

    content = (tmp_path / "05_People" / "Ana Reis.md").read_text(encoding="utf-8")
    assert 'role: "CEO"' in content  # unchanged


def test_apply_link_overwrite_true_replaces_existing_role(tmp_path: Path):
    vault = VaultManager(vault_path=tmp_path)
    vault.initialize_vault_structure()
    vault.create_person_note(name="Ana Reis", role="CEO", organization="Acme")
    cand = _cand("person", "Ana Reis", payload_json='{"role": "Diretora", "org": "Acme"}')
    match = {"name": "Ana Reis", "path": str(tmp_path / "05_People" / "Ana Reis.md")}

    apply_resolution(vault, cand, LINK, target=match, overwrite=True)

    content = (tmp_path / "05_People" / "Ana Reis.md").read_text(encoding="utf-8")
    assert 'role: "Diretora"' in content
