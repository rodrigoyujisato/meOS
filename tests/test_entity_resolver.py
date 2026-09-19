"""Tests for the deterministic transcript entity resolver."""

from pathlib import Path

from rysos.connectors.entity_resolver import EntityResolver, AUTO_LINK, CANDIDATE, DROP
from rysos.vault.manager import VaultManager


def _vault(tmp_path: Path) -> VaultManager:
    v = VaultManager(vault_path=tmp_path)
    v.initialize_vault_structure()
    return v


def _rich(**kw):
    base = {"people": [], "projects": [], "decisions": [], "resources": []}
    base.update(kw)
    return base


def _resolve(v, rich, *, raw_text=None, **kw):
    """Resolve with a synthesised transcript that mentions every person twice, so
    the salience filter passes and the matching path is exercised. Pass an explicit
    `raw_text` to test the salience filter itself."""
    if raw_text is None:
        names = [p.get("name", "") for p in rich.get("people", [])]
        raw_text = " ".join(f"{n} disse algo. Depois {n} concordou." for n in names)
    return EntityResolver(v).resolve(rich, raw_text=raw_text, **kw)


def test_exact_person_autolinks(tmp_path):
    v = _vault(tmp_path)
    v.create_person_note(name="Mário César Prado Bernardes", role="Dir", organization="XYZ")
    out = _resolve(v, _rich(people=[
        {"name": "Mário César Prado Bernardes", "role": "", "org": "", "mentions": "cobrou paridade"}
    ]))
    assert len(out) == 1
    assert out[0].disposition == AUTO_LINK
    assert out[0].link_target.endswith("Mário César Prado Bernardes.md")


def test_fuzzy_person_becomes_candidate_with_suggestions(tmp_path):
    v = _vault(tmp_path)
    v.create_person_note(name="Nilton Zeich", role="", organization="")
    out = _resolve(v, _rich(people=[
        {"name": "Nilton Tait", "role": "ex-ministro", "org": "", "mentions": "posicionamento institucional"}
    ]))
    assert out[0].disposition == CANDIDATE
    assert any(m["name"] == "Nilton Zeich" for m in out[0].suggested_matches)
    assert out[0].context_sentence == "posicionamento institucional"


def test_unmatched_salient_person_is_candidate_no_suggestions(tmp_path):
    v = _vault(tmp_path)
    out = _resolve(v, _rich(people=[
        {"name": "Gilberto Sarmento", "role": "Consultor", "org": "", "mentions": ""}
    ]))
    assert out[0].disposition == CANDIDATE
    assert out[0].suggested_matches == []


# -- salience filter (Phase B) ----------------------------------------------

def test_one_shot_roleless_name_is_dropped(tmp_path):
    v = _vault(tmp_path)
    out = _resolve(
        v, _rich(people=[{"name": "Heit", "role": "", "org": "", "mentions": "citado de passagem"}]),
        raw_text="A reunião foi sobre o produto. Alguém falou 'Heit' uma vez e seguiu.",
    )
    assert out[0].disposition == DROP
    assert out[0].suggested_matches == []


def test_role_makes_a_name_queueable(tmp_path):
    v = _vault(tmp_path)
    out = _resolve(
        v, _rich(people=[{"name": "Marina", "role": "Gerente Jurídica", "org": "", "mentions": ""}]),
        raw_text="Marina apareceu só uma vez aqui.",
    )
    assert out[0].disposition == CANDIDATE


def test_two_mentions_make_a_name_queueable(tmp_path):
    v = _vault(tmp_path)
    out = _resolve(
        v, _rich(people=[{"name": "Galvão", "role": "", "org": "", "mentions": ""}]),
        raw_text="Galvão trouxe o tema. No fim, Galvão ficou de enviar o resumo.",
    )
    assert out[0].disposition == CANDIDATE


def test_ownership_sentence_makes_a_name_queueable(tmp_path):
    v = _vault(tmp_path)
    out = _resolve(
        v, _rich(people=[{"name": "Tati", "role": "", "org": "",
                          "mentions": "ficou responsável por revisar o contrato"}]),
        raw_text="Tati aparece uma vez.",
    )
    assert out[0].disposition == CANDIDATE


def test_dropped_person_still_carries_identity_key(tmp_path):
    v = _vault(tmp_path)
    out = _resolve(
        v, _rich(people=[{"name": "Elintor", "role": "", "org": "", "mentions": ""}]),
        raw_text="Elintor foi citado uma vez.",
    )
    assert out[0].disposition == DROP
    assert out[0].identity_key.startswith("person:")


# -- decisions / resources -------------------------------------------------

def test_decisions_and_resources_always_candidates(tmp_path):
    v = _vault(tmp_path)
    out = EntityResolver(v).resolve(_rich(
        decisions=[{"title": "Governança em dois níveis", "status": "em_analise", "rationale": "paridade", "owner": "XYZ"}],
        resources=[{"type": "contrato", "name": "Minuta LOE", "note": "parceria"}],
    ))
    assert {r.kind for r in out} == {"decision", "resource"}
    assert all(r.disposition == CANDIDATE for r in out)
    by_kind = {r.kind: r for r in out}
    assert by_kind["decision"].payload["owner"] == "XYZ"


def test_project_exact_autolink_and_fuzzy_candidate(tmp_path):
    v = _vault(tmp_path)
    v.create_project_note(title="Altamar Health")
    out = EntityResolver(v).resolve(_rich(projects=[
        {"name": "Altamar Health", "status": "ativo", "summary": "x"},
        {"name": "Altamar Helth", "status": "", "summary": "typo"},
    ]))
    by_surface = {r.surface_form: r for r in out}
    assert by_surface["Altamar Health"].disposition == AUTO_LINK
    assert by_surface["Altamar Helth"].disposition == CANDIDATE
    assert any(m["title"] == "Altamar Health" for m in by_surface["Altamar Helth"].suggested_matches)


def test_resolver_dedupes_repeated_surface(tmp_path):
    v = _vault(tmp_path)
    out = _resolve(v, _rich(people=[
        {"name": "Nilton", "role": "Diretor", "org": "", "mentions": "a"},
        {"name": "nilton", "role": "Diretor", "org": "", "mentions": "b"},
    ]))
    assert len([r for r in out if r.kind == "person"]) == 1


# -- learned-concept short-circuit (Phase 3) -----------------------------------

def _learned_target(tmp_path, stem="Nilton Zeich"):
    # Deliberately NOT under 05_People, so the vault match can't see it — the point
    # of these tests is the learned-link path, isolated from the vault-match path.
    p = tmp_path / "08_Archive" / f"{stem}.md"
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text("# " + stem, encoding="utf-8")
    return str(p)


def test_learned_multitoken_exact_autolinks_without_candidate(tmp_path):
    v = _vault(tmp_path)
    tgt = _learned_target(tmp_path)
    out = _resolve(
        v, _rich(people=[{"name": "Nilton Zeich", "role": "Ex-diretor", "org": "", "mentions": "citou"}]),
        learned={"nilton zeich": tgt},
    )
    assert out[0].disposition == AUTO_LINK and out[0].link_target == tgt


def test_learned_single_token_never_autolinks(tmp_path):
    v = _vault(tmp_path)
    out = _resolve(
        v, _rich(people=[{"name": "Nilton", "role": "Diretor", "org": "", "mentions": ""}]),
        learned={"nilton": _learned_target(tmp_path, "Nilton")},
    )
    assert out[0].disposition == CANDIDATE


def test_learned_first_token_collision_blocks_autolink(tmp_path):
    v = _vault(tmp_path)
    out = _resolve(
        v, _rich(people=[{"name": "Nilton Zeich", "role": "Diretor", "org": "", "mentions": ""}]),
        learned={
            "nilton zeich": _learned_target(tmp_path, "Nilton Zeich"),
            "nilton mendes": _learned_target(tmp_path, "Nilton Mendes"),
        },
    )
    assert out[0].disposition == CANDIDATE  # ambiguous first name -> stays for review


def test_learned_ignored_when_vault_match_exists(tmp_path):
    v = _vault(tmp_path)
    v.create_person_note(name="Nilton Zeich", role="Dir", organization="")
    real = str(tmp_path / "05_People" / "Nilton Zeich.md")
    out = _resolve(
        v, _rich(people=[{"name": "Nilton Zeich", "role": "Diretor", "org": "", "mentions": ""}]),
        learned={"nilton zeich": "/somewhere/else/Stale.md"},
    )
    assert out[0].disposition == AUTO_LINK and out[0].link_target == real
