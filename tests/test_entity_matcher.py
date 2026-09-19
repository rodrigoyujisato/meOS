"""Truth table for the robust identity matcher (`rysos.connectors.entity_match`).

Written before the implementation (Phase A of the entity-matcher-HITL plan). The
matcher has to satisfy two pulling-apart requirements:

  1. idiomatic variation — "Rafael", "Rafaelão", "Moreira", "R. Moreira" are all the
     same person as the vault note "Rafael Moreira";
  2. homonym safety — two different "Nilton"s must NOT merge on the first name
     alone; only a composite key (org / role / email) or a surname-position token
     is allowed to auto-link.

Pure module: no vault, no DB. `match_against` takes an in-memory candidate list of
`{name|title, path, organization, role, email}` dicts.
"""

import pytest

from rysos.connectors.entity_match import (
    canonical_tokens,
    grouping_key,
    identity_key,
    same_token,
    match_against,
    should_queue_person,
)


# --- token canonicalization -------------------------------------------------

@pytest.mark.parametrize("name, expected_len", [
    ("Rafael Moreira", 2),
    ("R. Moreira", 2),
    ("Dr. Nilton", 1),          # honorific dropped
    ("Sra. Marina Cardoso", 2),   # honorific dropped
    ("", 0),
])
def test_canonical_tokens_shape(name, expected_len):
    assert len(canonical_tokens(name)) == expected_len


@pytest.mark.parametrize("a, b", [
    ("Rafael", "Rafaelão"),
    ("Rafael", "rafael"),
    ("Marcelo", "Marcelão"),
    ("Beto", "Betão"),
    ("Carlos", "Carlão"),
    ("João", "Joãozinho"),
    ("Zeich", "Zeich"),
])
def test_same_token_idiomatic_variation(a, b):
    (ta,), (tb,) = canonical_tokens(a), canonical_tokens(b)
    assert same_token(ta, tb), f"{a!r} and {b!r} should fold to the same person-token"


@pytest.mark.parametrize("a, b", [
    ("Nilton", "Wellington"),
    ("Marina", "Marcelo"),
    ("Moreira", "Souza"),
    ("Zeich", "Tait"),          # ASR-close but NOT auto-mergeable without other signal
])
def test_same_token_rejects_distinct_names(a, b):
    (ta,), (tb,) = canonical_tokens(a), canonical_tokens(b)
    assert not same_token(ta, tb), f"{a!r} and {b!r} must stay distinct tokens"


# --- identity_key (cross-transcript grouping, computed - no DB column) -----

def test_identity_key_groups_honorific_and_bare_first_name():
    assert identity_key("person", "Dr Nilton") == identity_key("person", "Nilton")


def test_identity_key_groups_augmentative_with_base():
    assert identity_key("person", "Rafaelão Moreira") == identity_key("person", "Rafael Moreira")


def test_identity_key_splits_same_first_name_by_org():
    a = identity_key("person", "Nilton", org="Zeich Health")
    b = identity_key("person", "Nilton", org="Acme Capital")
    assert a != b, "two different-org Niltons must not share a review group"


def test_identity_key_bare_first_name_no_org_is_stable():
    assert identity_key("person", "Nilton") == identity_key("person", "nilton")


# --- grouping_key: a confident suggestion overrides the computed key ---------

def test_grouping_key_lone_surname_variants_group_via_suggestion():
    """'Moreira' with different (or no) orgs would org-namespace apart under
    identity_key; a strong suggestion to the same vault note re-unites them."""
    sug = {"name": "Rafael Moreira", "path": "/v/05_People/Rafael Moreira.md", "score": 0.9}
    k1 = grouping_key("person", "Moreira", org="Aliança", top_suggestion=sug)
    k2 = grouping_key("person", "Moreira", org="Meridian", top_suggestion=sug)
    k3 = grouping_key("person", "Rafael", org="", top_suggestion=sug)
    assert k1 == k2 == k3 == "person:=Rafael Moreira"
    # without the suggestion the org split stands
    assert grouping_key("person", "Moreira", org="Aliança") != grouping_key("person", "Moreira", org="Meridian")


def test_grouping_key_weak_suggestion_does_not_override():
    sug = {"name": "Rafael Moreira", "path": "/v/05_People/Rafael Moreira.md", "score": 0.7}
    assert grouping_key("person", "Moreira", top_suggestion=sug) == identity_key("person", "Moreira")


# --- should_queue_person: whole-word occurrence count -----------------------

def test_should_queue_person_counts_whole_words_not_substrings():
    # "Su" appears as a substring of assunto/sua/consultor but is named only once
    raw = "O assunto principal foi discutido. Su trouxe uma ideia. A sua proposta e do consultor."
    assert should_queue_person("Su", {}, raw) is False
    # two real mentions -> queued
    assert should_queue_person("Su", {}, raw + " Su tambem vai revisar.") is True


# --- match_against: vault resolution ---------------------------------------

def _person(name, path="/v/05_People/%s.md", **kw):
    d = {"name": name, "path": path % name, "organization": "", "role": "", "email": ""}
    d.update(kw)
    return d


OWNER = _person("Rafael Moreira", organization="rysOS", role="Diretor")
NILTON_ZEICH = _person("Nilton Zeich", organization="Zeich Health")


@pytest.mark.parametrize("surface", ["Rafael Moreira", "Moreira", "R. Moreira", "Rafaelão Moreira"])
def test_multitoken_or_surname_autolinks(surface):
    r = match_against(surface, [OWNER])
    assert r["decision"] == "auto"
    assert r["target"] == OWNER["path"]


def test_bare_first_name_without_composite_key_is_review_with_suggestion():
    r = match_against("Rafael", [OWNER])
    assert r["decision"] == "review"
    assert r["target"] is None
    assert any(m["name"] == "Rafael Moreira" for m in r["suggested_matches"])


def test_bare_first_name_with_matching_org_is_promoted_to_auto():
    r = match_against("Rafael", [OWNER], org="rysOS")
    assert r["decision"] == "auto"
    assert r["target"] == OWNER["path"]


def test_bare_first_name_with_wrong_org_stays_review():
    r = match_against("Rafael", [OWNER], org="Outra Empresa")
    assert r["decision"] == "review"


def test_surname_position_token_bridges_to_full_note():
    r = match_against("Zeich", [NILTON_ZEICH])
    assert r["decision"] == "auto"
    assert r["target"] == NILTON_ZEICH["path"]


def test_first_name_bridges_only_as_suggestion():
    r = match_against("Nilton", [NILTON_ZEICH])
    assert r["decision"] == "review"
    assert any(m["name"] == "Nilton Zeich" for m in r["suggested_matches"])


def test_first_name_with_matching_org_bridges_to_auto():
    r = match_against("Nilton", [NILTON_ZEICH], org="Zeich Health")
    assert r["decision"] == "auto"
    assert r["target"] == NILTON_ZEICH["path"]


def test_two_vault_ties_never_autolink():
    nelson_a = _person("Nilton Zeich", organization="Zeich Health")
    nelson_b = _person("Nilton Mendes", organization="Mendes Adv")
    r = match_against("Nilton", [nelson_a, nelson_b])
    assert r["decision"] == "review"
    assert len(r["suggested_matches"]) == 2


def test_no_vault_match_is_review_no_suggestion():
    r = match_against("Gilberto Sarmento", [OWNER, NILTON_ZEICH])
    assert r["decision"] == "review"
    assert r["suggested_matches"] == []


def test_exact_normalized_match_is_auto():
    r = match_against("rafael moreira", [OWNER])
    assert r["decision"] == "auto" and r["target"] == OWNER["path"]


# --- projects: err toward review -----------------------------------------

def _proj(title):
    return {"title": title, "path": f"/v/01_Projects/{title}.md", "organization": "", "role": "", "email": ""}


def test_project_exact_autolinks():
    r = match_against("Altamar Health", [_proj("Altamar Health")], kind="project")
    assert r["decision"] == "auto"


def test_project_single_shared_token_is_review():
    r = match_against("Meridian EXH", [_proj("Meridian")], kind="project")
    assert r["decision"] == "review"
    assert any(m["title"] == "Meridian" for m in r["suggested_matches"])


def test_project_typo_is_review_with_suggestion():
    r = match_against("Altamar Helth", [_proj("Altamar Health")], kind="project")
    assert r["decision"] == "review"
    assert any(m["title"] == "Altamar Health" for m in r["suggested_matches"])
