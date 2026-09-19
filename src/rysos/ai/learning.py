"""Adaptive Knowledge Base & Emergence Engine for rysOS.

Continuously learns user preferences, entity mappings, dynamic vault categories,
and detects emerging recurring concepts over time.
"""

import json
import logging
import re
import time
from datetime import datetime, timezone
from typing import Any, Optional
from sqlalchemy import select, update

from rysos.identity import user_first_name
from rysos.vault.manager import VaultManager
from rysos.db import get_db_session, AdaptiveConcept, AdaptiveFeedbackLog

logger = logging.getLogger("rysos.ai.learning")

DEFAULT_CATEGORIES: dict[str, dict[str, Any]] = {
    "project": {
        "label": "🚀 Projeto",
        "folder": "01_Projects",
        "description": "Projetos estratégicos com escopo, prazo, objetivo e stakeholders envolvidos",
        "required_fields": ["title", "deadline", "stakeholders", "outcome_description"],
    },
    "decision": {
        "label": "⚖️ Decisão",
        "folder": "03_Decisions",
        "description": "Decisões executivas sob análise ou decididas com trade-offs e métricas de sucesso",
        "required_fields": ["title", "context", "assumptions", "outcome"],
    },
    "person": {
        "label": "👤 Pessoa",
        "folder": "05_People",
        "description": "Pessoas, contatos de networking, executivos, sócios e stakeholders",
        "required_fields": ["name", "organization", "role"],
    },
    "today": {
        "label": "🎯 Foco Hoje",
        "folder": "00_Cockpit/Daily",
        "description": "Metas executivas imediatas e focos de alta prioridade para o dia",
        "required_fields": ["title", "area", "description"],
    },
    "idea": {
        "label": "💡 Ideia",
        "folder": "06_Resources/Ideias_e_Criatividade",
        "description": "Ideias inovadoras, teses de investimento, insights e conceitos criativos",
        "required_fields": ["title", "overview", "rationale", "next_steps"],
    },
    "framework": {
        "label": "📐 Framework",
        "folder": "06_Resources/Frameworks_e_Metodos",
        "description": "Modelos mentais, frameworks de negócios, métodos e processos estruturados",
        "required_fields": ["title", "description", "components"],
    },
}


class AdaptiveKnowledgeBase:
    """Manages dynamic taxonomy discovery, feedback memory, and emergent patterns."""

    _CATEGORIES_TTL = 300.0  # seconds; the template set changes rarely

    def __init__(self, vault_manager: Optional[VaultManager] = None):
        self.vault = vault_manager or VaultManager()
        self._cat_cache: Optional[dict[str, dict[str, Any]]] = None
        self._cat_cache_at: float = 0.0

    def discover_vault_categories(self) -> dict[str, dict[str, Any]]:
        """Scans Obsidian Vault for base and custom categories/templates.

        Cached for `_CATEGORIES_TTL` because the scan reads several template files
        off the Google Drive mount and runs on every message classification.
        """
        if self._cat_cache is not None and (time.monotonic() - self._cat_cache_at) < self._CATEGORIES_TTL:
            return self._cat_cache

        categories = dict(DEFAULT_CATEGORIES)

        try:
            templates_dir = self.vault.vault_path / "08_Templates"
            if templates_dir.exists():
                for tmpl_file in templates_dir.glob("*.md"):
                    content = self.vault.read_file(tmpl_file) or ""
                    # Discover custom category if template specifies type/category
                    m_type = re.search(r"^type:\s*[\"']?([a-zA-Z0-9_\-]+)[\"']?", content, re.MULTILINE)
                    if m_type:
                        cat_slug = m_type.group(1).lower().strip()
                        if cat_slug not in categories:
                            categories[cat_slug] = {
                                "label": f"📁 {cat_slug.capitalize()}",
                                "folder": f"06_Resources/{cat_slug.capitalize()}",
                                "description": f"Categoria dinâmica identificada no template {tmpl_file.name}",
                                "required_fields": ["title"],
                            }
        except Exception as e:
            logger.warning(f"Erro ao escanear templates do vault: {e}")

        self._cat_cache = categories
        self._cat_cache_at = time.monotonic()
        return categories

    async def record_concept_observation(
        self,
        term: str,
        concept_type: str = "entity",
        preferred_category: Optional[str] = None,
        preferred_area: Optional[str] = None,
        metadata: Optional[dict[str, Any]] = None,
    ) -> AdaptiveConcept:
        """Observes an entity/topic, tracks occurrence frequency, and flags emergent concepts."""
        clean_term = term.strip()
        if not clean_term or len(clean_term) < 3:
            return None  # ignore trivial terms

        async with get_db_session() as session:
            stmt = select(AdaptiveConcept).where(AdaptiveConcept.term.ilike(clean_term))
            concept = (await session.execute(stmt)).scalar_one_or_none()

            if concept:
                concept.occurrence_count += 1
                if preferred_category:
                    concept.preferred_category = preferred_category
                if preferred_area:
                    concept.preferred_area = preferred_area
                if metadata:
                    existing_meta = json.loads(concept.metadata_json or "{}")
                    incoming = dict(metadata)
                    # surface_forms accumulates every spelling we've seen resolve to this
                    # concept — merge (dedup, order-stable), never overwrite.
                    if "surface_forms" in incoming or "surface_forms" in existing_meta:
                        merged = list(existing_meta.get("surface_forms") or [])
                        for sf in incoming.pop("surface_forms", []) or []:
                            if sf and sf not in merged:
                                merged.append(sf)
                        existing_meta["surface_forms"] = merged
                    existing_meta.update(incoming)
                    concept.metadata_json = json.dumps(existing_meta, ensure_ascii=False)

                # Emerging topic threshold: 3+ mentions
                if concept.occurrence_count >= 3:
                    concept.is_emergent = True
            else:
                concept = AdaptiveConcept(
                    term=clean_term,
                    concept_type=concept_type,
                    preferred_category=preferred_category,
                    preferred_area=preferred_area,
                    occurrence_count=1,
                    is_emergent=False,
                    metadata_json=json.dumps(metadata or {}, ensure_ascii=False) if metadata else None,
                )
                session.add(concept)

            await session.commit()
            return concept

    async def record_feedback(
        self,
        user_id: int,
        raw_input: str,
        predicted_intent: str,
        predicted_category: Optional[str],
        feedback_type: str,
        reasoning: Optional[str] = None,
        corrected_intent: Optional[str] = None,
        corrected_category: Optional[str] = None,
        confidence: float = 1.0,
    ) -> AdaptiveFeedbackLog:
        """Records classification decision, human corrections, and extracts learned entities."""
        async with get_db_session() as session:
            log_entry = AdaptiveFeedbackLog(
                user_id=user_id,
                raw_input=raw_input,
                predicted_intent=predicted_intent,
                predicted_category=predicted_category,
                corrected_intent=corrected_intent,
                corrected_category=corrected_category,
                confidence=confidence,
                reasoning=reasoning,
                feedback_type=feedback_type,
            )
            session.add(log_entry)
            await session.commit()

        # If user explicitly corrected category, reinforce the learned mapping
        final_cat = corrected_category or predicted_category
        if final_cat and feedback_type in ("confirmed", "corrected"):
            # Extract potential proper nouns / capitalized entity phrases
            candidates = re.findall(r"\b[A-Z][a-z0-9]+(?:\s+[A-Z][a-z0-9]+)*\b", raw_input)
            for cand in candidates:
                if len(cand) > 3 and cand.lower() not in {user_first_name().lower(), "obsidian", "segundo", "cérebro"}:
                    await self.record_concept_observation(
                        term=cand,
                        concept_type="entity",
                        preferred_category=final_cat,
                    )

        return log_entry

    async def get_recent_turns(self, user_id: int, limit: int = 8) -> list[dict[str, Any]]:
        """Returns the user's most recent classified messages (oldest first) as a short
        conversation-context window for the intent classifier. Sourced from the audit log
        that `record_feedback` already writes on every classification."""
        async with get_db_session() as session:
            stmt = (
                select(AdaptiveFeedbackLog)
                .where(AdaptiveFeedbackLog.user_id == user_id)
                .order_by(AdaptiveFeedbackLog.id.desc())
                .limit(max(1, limit))
            )
            rows = (await session.execute(stmt)).scalars().all()

        now = datetime.now(timezone.utc)
        turns: list[dict[str, Any]] = []
        for r in reversed(rows):
            created = r.created_at
            if created is not None and created.tzinfo is None:
                created = created.replace(tzinfo=timezone.utc)
            age_min = int((now - created).total_seconds() // 60) if created else None
            turns.append({
                "text": (r.raw_input or "")[:200],
                "intent": r.predicted_intent,
                "feedback": r.feedback_type,
                "age_min": age_min,
            })
        return turns

    async def get_learned_concepts(self, limit: int = 20) -> list[dict[str, Any]]:
        """Returns top learned concepts and entity mappings for prompt grounding."""
        async with get_db_session() as session:
            stmt = select(AdaptiveConcept).order_by(AdaptiveConcept.occurrence_count.desc()).limit(limit)
            concepts = (await session.execute(stmt)).scalars().all()
            return [
                {
                    "term": c.term,
                    "type": c.concept_type,
                    "preferred_category": c.preferred_category,
                    "preferred_area": c.preferred_area,
                    "occurrences": c.occurrence_count,
                    "is_emergent": c.is_emergent,
                }
                for c in concepts
            ]

    async def get_emergent_patterns(self) -> list[dict[str, Any]]:
        """Returns recurring concepts that have emerged but don't yet have dedicated projects."""
        async with get_db_session() as session:
            stmt = select(AdaptiveConcept).where(AdaptiveConcept.is_emergent == True).order_by(AdaptiveConcept.occurrence_count.desc()).limit(5)
            concepts = (await session.execute(stmt)).scalars().all()
            return [
                {
                    "term": c.term,
                    "occurrences": c.occurrence_count,
                    "preferred_category": c.preferred_category or "project",
                }
                for c in concepts
            ]


adaptive_kb = AdaptiveKnowledgeBase()
