"""Focused tests for the one-off backlog migration's rendering logic."""

import importlib.util
import sys
from pathlib import Path
from types import SimpleNamespace

_SPEC = importlib.util.spec_from_file_location(
    "migrate_entity_candidates",
    Path(__file__).resolve().parent.parent / "scripts" / "migrate_entity_candidates.py",
)
mig = importlib.util.module_from_spec(_SPEC)
sys.modules["migrate_entity_candidates"] = mig
_SPEC.loader.exec_module(mig)


def _row(**kw):
    base = dict(id=1, transcript_sha="a" * 64, kind="person", surface_form="Moreira",
                normalized_form="moreira", context_sentence="apresentou a proposta",
                payload_json='{"role": "Diretor", "org": "rysOS"}',
                suggested_matches_json=None, status="pending")
    base.update(kw)
    return SimpleNamespace(**base)


_OLD = """---
type: triagem
meeting: "[[04_Meetings/2026-09-02 - X|X]]"
date: 2026-09-02
status: pending
---
<!-- triagem:aaaaaaaaaaaa -->
# 🗂️ Triagem: X

> Nenhuma entidade abaixo foi criada ou vinculada automaticamente. Resolva cada item.

## 👤 Pessoas
- [ ] **Moreira**
      frase: "apresentou a proposta"
      parecido com: nenhum
      resolução: NOVO | =<nome exato> | IGNORAR
- [ ] **Mário**
      frase: "conduziu"
      parecido com: Mário César (1.0)
      resolução: NOVO | =<nome exato> | IGNORAR

## ⚖️ Decisões
- [ ] **Seguir com A** — decidido
      frase: "preço"
      parecido com: nenhum
      resolução: NOVO | =<nome exato> | IGNORAR
"""


def test_render_consolidates_and_keeps_only_pending_rows():
    rows = [
        _row(id=1, surface_form="Moreira", normalized_form="moreira",
             suggested_matches_json='[{"name": "Rafael Moreira", "path": "/v/05_People/Rafael Moreira.md", "score": 0.9}]'),
        _row(id=2, kind="decision", surface_form="Seguir com A", normalized_form="seguir com a",
             payload_json='{"status": "decidido"}', context_sentence="preço"),
    ]
    out = mig._render(_OLD, "a" * 64, rows)

    assert "## ⚡ Ações em massa" in out
    assert out.index("## 🚀 Projetos" if "## 🚀" in out else "## 👤 Pessoas") < out.index("## ⚖️ Decisões")
    assert "**Moreira**" in out and "Rafael Moreira (0.9)" in out
    assert "**Mário**" not in out                       # already resolved -> dropped
    assert "resolução: REGISTRAR | =<título exato> | IGNORAR" in out  # decision stub
    assert "status: pending" in out


def test_render_marks_resolved_when_no_rows_survive():
    out = mig._render(_OLD, "a" * 64, [])
    assert "status: resolved" in out and "status: pending" not in out
    assert "Nenhuma entidade pendente após a migração" in out
    assert "<!-- triagem:aaaaaaaaaaaa -->" in out       # marker preserved


def test_to_resolved_carries_identity_key_and_payload():
    r = mig._to_resolved(_row(surface_form="Rafaelão", payload_json='{"org": "rysOS"}'))
    assert r.kind == "person" and r.disposition == "candidate"
    assert r.identity_key.startswith("person:")
    assert r.payload["org"] == "rysOS"
