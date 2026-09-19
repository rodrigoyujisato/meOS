"""Tests for Adaptive Semantic Intent Classifier."""

import pytest
from types import SimpleNamespace
from rysos.ai.classifier import (
    AdaptiveClassifier,
    IntentType,
    IntentResult,
    looks_interrogative,
    normalize_text,
)


@pytest.mark.asyncio
async def test_adaptive_classifier_heuristics():
    classifier = AdaptiveClassifier()

    # 1. Cockpit query
    res_cockpit = await classifier.classify("quais as pendencias de hoje?", user_id=1)
    assert res_cockpit.intent == IntentType.QUERY_COCKPIT

    # 2. Agenda query
    res_agenda = await classifier.classify("qual minha agenda de hoje?", user_id=1)
    assert res_agenda.intent == IntentType.QUERY_AGENDA

    # 3. Decisions query
    res_dec = await classifier.classify("quais as decisões em aberto?", user_id=1)
    assert res_dec.intent == IntentType.QUERY_DECISIONS

    # 4. Recap query
    res_recap = await classifier.classify("fechamento do dia", user_id=1)
    assert res_recap.intent == IntentType.QUERY_RECAP

    # 5. Greeting
    res_greet = await classifier.classify("Olá bom dia", user_id=1)
    assert res_greet.intent == IntentType.GREETING

    # 6. Capture Note
    res_note = await classifier.classify("Projeto de fusão com a empresa Beta prazo outubro", user_id=1)
    assert res_note.intent == IntentType.CAPTURE_NOTE

    # 7. Focus completion report (existing Cockpit item finished/paid)
    res_done = await classifier.classify("Boleto da Gasnorte pago hoje", user_id=1)
    assert res_done.intent == IntentType.COMPLETE_FOCUS

    res_done2 = await classifier.classify("já resolvi o contrato do Acervo", user_id=1)
    assert res_done2.intent == IntentType.COMPLETE_FOCUS


@pytest.mark.asyncio
async def test_natural_day_questions_route_to_cockpit_not_capture():
    """The exact phrasings that were misrouted into the note-draft loop."""
    classifier = AdaptiveClassifier()
    for phrase in (
        "O que temos para o dia de hj",
        "Quero saber o que eu preciso fazer hoje",
        "o que tem pra hoje",
        "o que eu tenho que fazer hoje?",
        "como está meu dia",
    ):
        res = await classifier.classify(phrase, user_id=1)
        assert res.intent == IntentType.QUERY_COCKPIT, f"{phrase!r} -> {res.intent}"


def test_looks_interrogative_distinguishes_questions_from_statements():
    assert looks_interrogative("o que temos para hoje")
    assert looks_interrogative("me diz minhas reuniões")
    assert looks_interrogative("tudo certo com o contrato?")
    assert not looks_interrogative("lista de compras para o churrasco")
    assert not looks_interrogative("novo projeto de fusão com a Beta prazo outubro")


def _mock_gemini(monkeypatch, classifier, json_text: str):
    fake_response = SimpleNamespace(text=json_text)
    fake_client = SimpleNamespace(
        models=SimpleNamespace(generate_content=lambda *a, **k: fake_response)
    )
    monkeypatch.setattr(classifier.ai, "_client", fake_client)
    monkeypatch.setattr(classifier.ai, "api_key", "test-key-1234567890")
    monkeypatch.setattr(classifier.ai, "is_available", lambda: True)
    monkeypatch.setattr(classifier.ai, "_record_usage", lambda *a, **k: None)


@pytest.mark.asyncio
async def test_llm_capture_on_a_question_is_overridden(monkeypatch):
    """Guardrail: if the model tags an interrogative message CAPTURE_NOTE, demote it."""
    classifier = AdaptiveClassifier()
    _mock_gemini(monkeypatch, classifier, '{"intent": "CAPTURE_NOTE", "is_question": true, "confidence": 0.95}')

    # no keyword cue -> falls back to QUERY_GENERAL
    res_general = await classifier.classify("e aquilo que a gente conversou ontem, deu certo?", user_id=1)
    assert res_general.intent == IntentType.QUERY_GENERAL

    # a cued one -> the specific QUERY_*
    res_cockpit = await classifier.classify("me lembra o que preciso fazer hoje?", user_id=1)
    assert res_cockpit.intent == IntentType.QUERY_COCKPIT


@pytest.mark.asyncio
async def test_low_confidence_capture_is_answered_as_a_question(monkeypatch):
    classifier = AdaptiveClassifier()
    _mock_gemini(monkeypatch, classifier, '{"intent": "CAPTURE_NOTE", "is_question": false, "confidence": 0.30}')

    res = await classifier.classify("biobanco equity veículo", user_id=1)
    assert res.intent == IntentType.QUERY_GENERAL


@pytest.mark.asyncio
async def test_llm_high_confidence_capture_is_kept(monkeypatch):
    classifier = AdaptiveClassifier()
    _mock_gemini(monkeypatch, classifier, '{"intent": "CAPTURE_NOTE", "is_question": false, "confidence": 0.9}')

    res = await classifier.classify("nova ideia: comitê de inovação trimestral com os diretores", user_id=1)
    assert res.intent == IntentType.CAPTURE_NOTE
