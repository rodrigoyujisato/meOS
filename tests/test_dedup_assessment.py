"""Tests for GeminiAIClient.assess_dedup_finding — the AI recommendation layer behind
the /dedup triage workflow. Nothing here writes to the vault; this only checks the
prompt/response contract."""

from types import SimpleNamespace

from rysos.ai.gemini import gemini_ai


def _mock_gemini(monkeypatch, json_text: str):
    fake_response = SimpleNamespace(text=json_text, usage_metadata=None)
    fake_client = SimpleNamespace(
        models=SimpleNamespace(generate_content=lambda *a, **k: fake_response)
    )
    monkeypatch.setattr(gemini_ai, "_client", fake_client, raising=False)
    monkeypatch.setattr(gemini_ai, "api_key", "test-key-1234567890")
    monkeypatch.setattr(gemini_ai, "is_available", lambda: True)
    monkeypatch.setattr(gemini_ai, "_record_usage", lambda *a, **k: None)


def test_assess_dedup_finding_parses_merge_recommendation(monkeypatch):
    _mock_gemini(
        monkeypatch,
        '{"recommendation": "merge", "rationale": "Mesma iniciativa, títulos diferentes.", '
        '"survivor_path": "01_Projects/A.md", "consolidated_summary": "Resumo consolidado."}',
    )
    context = {
        "a_path": "01_Projects/A.md", "a_name": "A", "a_content": "conteudo A",
        "a_backlinks": [],
        "b_path": "01_Projects/B.md", "b_name": "B", "b_content": "conteudo B",
        "b_backlinks": [],
        "score": 0.4,
    }
    result = gemini_ai.assess_dedup_finding("projects", context)
    assert result["recommendation"] == "merge"
    assert result["survivor_path"] == "01_Projects/A.md"
    assert "Resumo" in result["consolidated_summary"]


def test_assess_dedup_finding_rejects_unknown_recommendation_value(monkeypatch):
    _mock_gemini(monkeypatch, '{"recommendation": "delete_everything", "rationale": "x"}')
    context = {
        "a_path": "05_People/A.md", "a_name": "A", "a_content": "x", "a_backlinks": [],
        "b_path": "05_People/B.md", "b_name": "B", "b_content": "y", "b_backlinks": [],
        "score": 0.9,
    }
    result = gemini_ai.assess_dedup_finding("people", context)
    assert result["recommendation"] == "needs_review"


def test_assess_dedup_finding_falls_back_when_unavailable(monkeypatch):
    monkeypatch.setattr(gemini_ai, "is_available", lambda: False)
    context = {
        "orphan_path": "05_People/X.md", "orphan_name": "X", "orphan_content": "x",
        "mentions": [{
            "cockpit_path": "00_Cockpit/Daily/2026-09-16.md",
            "cockpit_date": "2026-09-16",
            "matched_text": "X",
        }],
    }
    result = gemini_ai.assess_dedup_finding("diary_candidates", context)
    assert result["recommendation"] == "needs_review"
    assert result["survivor_path"] is None
