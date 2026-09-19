"""Tests for Token Observability and Cost Governance module."""

import ast
from pathlib import Path

import pytest
from httpx import AsyncClient, ASGITransport
import rysos
from rysos.observability import (
    calculate_token_cost,
    record_ai_token_usage,
    get_token_usage_stats,
)
from rysos.web.app import app
from rysos.ai.gemini import gemini_ai


def test_calculate_token_cost():
    # Flash lite pricing: $0.075 / 1M prompt, $0.30 / 1M output
    cost_lite = calculate_token_cost("gemini-3.5-flash-lite", 1_000_000, 1_000_000)
    assert round(cost_lite, 3) == 0.375

    # Flash pricing: $0.15 / 1M prompt, $0.60 / 1M output
    cost_flash = calculate_token_cost("gemini-2.5-flash", 1_000_000, 1_000_000)
    assert round(cost_flash, 3) == 0.750

    # Small tokens
    cost_small = calculate_token_cost("gemini-3.5-flash-lite", 1000, 500)
    assert cost_small > 0.0


def test_record_and_get_token_stats():
    # Record test token consumption
    res = record_ai_token_usage(
        model="gemini-3.5-flash-lite",
        operation="test_op",
        prompt_tokens=1500,
        candidate_tokens=300,
        total_tokens=1800,
        details="Unit test call",
    )
    assert res["total_tokens"] == 1800
    assert res["cost_usd"] > 0

    stats = get_token_usage_stats(days=7)
    assert stats["today"]["total_tokens"] >= 1800
    assert stats["today"]["calls_count"] >= 1
    assert any(op["operation"] == "test_op" for op in stats["by_operation"])


def test_every_generate_content_call_site_records_token_usage():
    """Guarantees every Gemini generate_content call anywhere in rysos is covered by
    observability — not just today's known call sites (ai/gemini.py, ai/classifier.py).
    Walks every .py file under the rysos package; any function/method body that mentions
    generate_content (direct call, or a bound-method reference passed to
    asyncio.to_thread) must also call _record_usage in the same body, so a future LLM
    call site can't ship untracked.
    """
    pkg_root = Path(rysos.__file__).parent
    checked_any = False
    for py_file in sorted(pkg_root.rglob("*.py")):
        source = py_file.read_text(encoding="utf-8")
        if "generate_content" not in source:
            continue
        tree = ast.parse(source, filename=str(py_file))
        for node in ast.walk(tree):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                body_src = ast.get_source_segment(source, node) or ""
                if "generate_content" in body_src:
                    checked_any = True
                    assert "_record_usage(" in body_src, (
                        f"{py_file.relative_to(pkg_root)}::{node.name} calls "
                        "generate_content but never calls _record_usage — token usage "
                        "would go untracked"
                    )
    assert checked_any, "no generate_content call site found — test may be broken"


@pytest.mark.asyncio
async def test_api_token_metrics_endpoint():
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.get("/api/metrics/tokens")
        assert response.status_code == 200
        data = response.json()
        assert "today" in data
        assert "total" in data
        assert "by_operation" in data
        assert "current_model" in data
