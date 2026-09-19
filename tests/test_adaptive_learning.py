"""Tests for Adaptive Knowledge Base & Emergence Engine."""

import pytest
from pathlib import Path
from rysos.ai.learning import AdaptiveKnowledgeBase
from rysos.vault.manager import VaultManager
from rysos.db import get_db_session, AdaptiveConcept, AdaptiveFeedbackLog


@pytest.mark.asyncio
async def test_adaptive_concept_learning_and_emergence(tmp_path: Path):
    vault = VaultManager(vault_path=tmp_path)
    vault.initialize_vault_structure()
    kb = AdaptiveKnowledgeBase(vault_manager=vault)

    # 1. First observation of a new concept
    term = "Acervo Cantonal"
    c1 = await kb.record_concept_observation(term=term, concept_type="entity", preferred_category="project")
    assert c1 is not None
    assert c1.term == term
    assert c1.occurrence_count == 1
    assert c1.is_emergent is False

    # 2. Second observation
    c2 = await kb.record_concept_observation(term=term, preferred_category="project")
    assert c2.occurrence_count == 2
    assert c2.is_emergent is False

    # 3. Third observation -> should become emergent!
    c3 = await kb.record_concept_observation(term=term, preferred_category="project")
    assert c3.occurrence_count == 3
    assert c3.is_emergent is True

    # 4. Verify in emergent list
    emergents = await kb.get_emergent_patterns()
    assert any(e["term"] == term for e in emergents)


@pytest.mark.asyncio
async def test_record_concept_observation_merges_surface_forms(tmp_path: Path):
    import json
    kb = AdaptiveKnowledgeBase(vault_manager=VaultManager(vault_path=tmp_path))
    term = "nilton zeich"
    await kb.record_concept_observation(
        term=term, concept_type="entity",
        metadata={"resolved_path": "/v/05_People/Nilton Zeich.md", "surface_forms": ["Nilton Zeich"]},
    )
    c = await kb.record_concept_observation(
        term=term, concept_type="entity",
        metadata={"surface_forms": ["Nilton Tait", "Nilton Zeich"]},  # one new, one dup
    )
    meta = json.loads(c.metadata_json)
    assert meta["surface_forms"] == ["Nilton Zeich", "Nilton Tait"]  # merged, deduped, order-stable
    assert meta["resolved_path"] == "/v/05_People/Nilton Zeich.md"   # untouched


@pytest.mark.asyncio
async def test_adaptive_feedback_logging(tmp_path: Path):
    vault = VaultManager(vault_path=tmp_path)
    vault.initialize_vault_structure()
    kb = AdaptiveKnowledgeBase(vault_manager=vault)

    log = await kb.record_feedback(
        user_id=12345,
        raw_input="ideia de parceria com Hospital Central Alfa",
        predicted_intent="CAPTURE_NOTE",
        predicted_category="idea",
        feedback_type="confirmed",
        confidence=0.98,
        reasoning="Ideia de parceria",
    )
    assert log is not None
    assert log.predicted_category == "idea"

    # Verify concept was extracted and recorded
    learned = await kb.get_learned_concepts()
    assert len(learned) >= 1
