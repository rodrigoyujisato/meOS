"""Adaptive Semantic Intent Classifier for rysOS.

Classifies natural language inputs into structured intents and dynamic categories
grounded by the user's Obsidian Vault taxonomy and accumulated learning.

Design (see plan wise-snacking-popcorn): intent detection is LLM-always but lightweight
and context-aware. Deterministic pieces are guardrails on the model's output (a question
never becomes a note; a low-confidence capture is answered instead), a keyword map used
by those guardrails and by the offline fallback, and an escalation pass when the user
keeps rephrasing.
"""

import asyncio
import json
import logging
import re
import unicodedata
from enum import Enum
from typing import Any, Optional
from pydantic import BaseModel, Field
from google.genai import types

from rysos.identity import personalize
from rysos.config import settings
from rysos.ai.gemini import gemini_ai
from rysos.ai.learning import adaptive_kb

logger = logging.getLogger("rysos.ai.classifier")

# A CAPTURE_NOTE the model is this unsure about is treated as a question instead of
# spinning up a sticky draft.
CAPTURE_MIN_CONFIDENCE = 0.55

# Deterministic markers that a message is reporting the completion of an existing
# Cockpit focus ("boleto pago", "reunião feita", "conclui a revisão"). Used by the
# offline heuristic fallback and by the Telegram mid-draft escape hatch.
FOCUS_COMPLETION_MARKERS = (
    "conclui", "concluí", "concluido", "concluído", "concluida", "concluída",
    "finalizei", "finalizado", "finalizada", "terminei", "resolvi", "resolvido", "resolvida",
    "feito", "feita", "ja fiz", "já fiz",
    "pago", "paga", "paguei", "quitado", "quitada", "quitei",
    "ja paguei", "já paguei", "ja foi pago", "já foi pago",
    "marca como concluido", "marcar como concluido", "marca como concluído",
    "marcar como concluído", "marca como feito", "pode marcar", "dar baixa", "baixado",
)

# Deterministic markers that a message is reporting a CANCELLATION or a RESCHEDULE of an
# existing Cockpit focus, not its completion. Both are status changes, but neither means
# the item is done, so a message matching these must never tick a checkbox (2026-09-15
# incident: "X foi CANCELADO" and "Y foi para <data>" were both mis-ticked as done).
FOCUS_CANCEL_OR_RESCHEDULE_MARKERS = (
    "cancelado", "cancelada", "cancelei", "cancelou", "foi cancelado", "foi cancelada",
    "adiado", "adiada", "adiei", "adiou", "remarcado", "remarcada", "remarquei", "remarcou",
    "postergado", "postergada", "postergou", "postergei",
    "foi para", "mudou para", "mudei para", "ficou para", "vai ficar para", "passou para",
)

# Deterministic keyword cues, matched against accent-stripped text. Shared by the
# offline fallback and by the interrogative guardrail.
_COCKPIT_CUES = (
    "pendencia", "pendencias", "cockpit", "resumo do dia", "resumo de hoje", "meu dia",
    "o que tenho hoje", "o que tem para hoje", "o que tem hoje", "o que tem pra hoje",
    "o que temos hoje", "o que temos para hoje", "o que temos pra hoje",
    "o que temos para o dia", "o que temos pro dia", "o que tem no meu dia",
    "o que eu preciso fazer hoje", "o que preciso fazer hoje", "o que eu tenho que fazer hoje",
    "o que tenho que fazer hoje", "preciso fazer hoje", "tenho que fazer hoje", "tenho pra fazer hoje",
    "foco do dia", "focos do dia", "focos de hoje", "prioridades de hoje", "prioridades do dia",
    "como esta meu dia", "como esta o dia", "o que ta pendente", "o que esta pendente",
)
_AGENDA_CUES = (
    "agenda", "reuniao", "reunioes", "compromisso", "compromissos", "meus horarios", "minha agenda",
)
# Imperative "create an event" phrasing. Checked BEFORE _AGENDA_CUES in keyword_query_intent
# so "marca uma reunião com o Carlos amanhã às 15h" doesn't fall into QUERY_AGENDA just
# because it contains "reunião" — the same trap the LLM prompt has to be told about.
_EVENT_CREATE_CUES = (
    "marca uma reuniao", "marcar uma reuniao", "marca reuniao", "marcar reuniao",
    "agenda uma reuniao", "agendar uma reuniao", "agenda reuniao", "agendar reuniao",
    "marca um compromisso", "marcar um compromisso", "agenda um compromisso", "agendar um compromisso",
    "marca uma call", "marcar uma call", "agenda uma call", "agendar uma call",
    "cria um evento", "criar um evento", "cria evento", "criar evento",
    "coloca na agenda", "colocar na agenda", "bota na agenda", "por na agenda",
    "marca na agenda",
)
_DECISION_CUES = ("decisao", "decisoes")
_RECAP_CUES = ("recap", "fechamento", "balanco")
_GREETING_EXACT = {
    "ola", "oi", "opa", "eai", "e ai", "bom dia", "boa tarde", "boa noite", "ola bom dia",
    "ola boa tarde", "ola boa noite", "oi bom dia",
}

# Interrogative cues — used only as a guardrail on the model's output (never to skip
# the model). Matched against normalize_text() output. Kept conservative: strong
# question openers only, so a statement like "lista de compras" is not swept in.
_QUESTION_PREFIX_RE = re.compile(
    r"^(o\s*que|oque|que\s|qual|quais|quando|quem|onde|cade|pra\s+que|por\s*que|porque|"
    r"quanto|quantos|quantas|"
    r"como\s+(esta|estao|anda|vai|funciona|faco)|"
    r"me\s+(diz|fala|mostra|mostre|manda|lembra|explica|conta|informa)|"
    r"quero\s+saber|queria\s+saber|preciso\s+saber|gostaria\s+de\s+saber|"
    r"tem\s+(algo|alguma|reuni|compromiss))",
)


def normalize_text(text: str) -> str:
    """Lowercased, accent-stripped, punctuation-collapsed form for keyword matching."""
    t = unicodedata.normalize("NFKD", (text or "").lower()).encode("ascii", "ignore").decode("ascii")
    t = re.sub(r"[^a-z0-9\s]", " ", t)
    return re.sub(r"\s+", " ", t).strip()


def looks_like_cancel_or_reschedule(text: str) -> bool:
    """True when a message reports a cancellation or a date change, not a completion.

    Guards COMPLETE_FOCUS: it must only ever tick a checkbox for genuine completion.
    """
    n = normalize_text(text)
    if not n:
        return False
    return any(k in n for k in (normalize_text(m) for m in FOCUS_CANCEL_OR_RESCHEDULE_MARKERS))


def looks_interrogative(text: str) -> bool:
    """True when a message reads as a question/info request rather than a statement."""
    t = (text or "").strip()
    if not t:
        return False
    if t.endswith("?"):
        return True
    return bool(_QUESTION_PREFIX_RE.match(normalize_text(t)))


class IntentType(str, Enum):
    QUERY_COCKPIT = "QUERY_COCKPIT"      # Focos, pendências de hoje, resumo do cockpit
    QUERY_AGENDA = "QUERY_AGENDA"        # Agenda, reuniões, horários, links
    QUERY_DECISIONS = "QUERY_DECISIONS"  # Decisões executivas sob análise ou resolvidas
    QUERY_RECAP = "QUERY_RECAP"          # Balanço noturno, fechamento do dia
    CREATE_EVENT = "CREATE_EVENT"        # Criar um novo evento/reunião no Google Calendar
    COMPLETE_FOCUS = "COMPLETE_FOCUS"    # Marcar um foco/checklist do Cockpit de hoje como concluído
    SYNC_WORKSPACE = "SYNC_WORKSPACE"    # Forçar sincronização imediata Google Calendar/Gmail
    CAPTURE_NOTE = "CAPTURE_NOTE"        # Anotação, ideia, projeto, decisão, pessoa a registrar
    GREETING = "GREETING"                # Saudações, cortesia ou dúvidas de uso
    QUERY_GENERAL = "QUERY_GENERAL"      # Perguntas abertas sobre conhecimento do Segundo Cérebro


class IntentResult(BaseModel):
    intent: IntentType = IntentType.CAPTURE_NOTE
    confidence: float = Field(default=1.0, ge=0.0, le=1.0)
    reasoning: str = ""
    suggested_category: Optional[str] = None
    detected_entities: list[str] = Field(default_factory=list)


CLASSIFIER_SYSTEM_PROMPT = """Você é o Classificador de Intenções Executivo do rysOS.
Determine, com rigor analítico (temperatura 0.0), a intenção de @@USER_NAME@@.

REGRA DE DECISÃO (aplique nesta ordem):
1. Se a mensagem é uma PERGUNTA ou um PEDIDO DE INFORMAÇÃO sobre o dia, a agenda, as
   pendências, os focos, as reuniões ou as decisões  ->  é uma intenção QUERY_*.
   NUNCA classifique uma pergunta como CAPTURE_NOTE.
   Sinais de pergunta: começa com "o que / qual / quais / quando / quem / como está / cadê",
   termina com "?", ou usa "quero saber / me diz / me mostra / preciso saber".
2. Se a mensagem é um PEDIDO IMPERATIVO para CRIAR/MARCAR/AGENDAR um evento, reunião ou
   compromisso novo (ex: "marca uma reunião com...", "agenda uma call...", "cria um evento...",
   "coloca na agenda...")  ->  CREATE_EVENT.  Cuidado: contém palavras como "reunião" ou
   "agenda", mas NÃO é uma pergunta sobre a agenda existente — é um pedido para criar algo
   novo nela. Nunca confunda com QUERY_AGENDA.
3. Se a mensagem RELATA que algo já foi concluído / pago / resolvido  ->  COMPLETE_FOCUS.
   ATENÇÃO: um CANCELAMENTO ("foi cancelado", "cancelei") ou uma REMARCAÇÃO/ADIAMENTO
   ("foi adiado", "ficou para o dia X", "foi para 21/09") NÃO é conclusão — é uma
   AFIRMAÇÃO que registra uma mudança de status, então vai para CAPTURE_NOTE, nunca
   para COMPLETE_FOCUS. Marcar algo cancelado ou adiado como concluído é um erro grave.
4. Se a mensagem é uma AFIRMAÇÃO que registra informação NOVA (uma ideia, um projeto,
   uma decisão a tomar, um contato, um lembrete a guardar)  ->  CAPTURE_NOTE.
5. Saudação social pura  ->  GREETING.   Pedido de sincronizar  ->  SYNC_WORKSPACE.
6. Pergunta conceitual / aberta sobre o Segundo Cérebro ou conhecimento passado  ->  QUERY_GENERAL.

Na dúvida entre QUERY_* e CAPTURE_NOTE: se NÃO há informação nova sendo registrada,
escolha QUERY_* (ou QUERY_GENERAL). CAPTURE_NOTE é só quando há conteúdo novo para guardar.

INTENÇÕES:
- QUERY_COCKPIT: o dia, pendências, focos, "o que tenho/temos para hoje", "o que preciso fazer hoje", resumo do cockpit.
- QUERY_AGENDA: compromissos, reuniões, horários de hoje (LEITURA, não criação).
- QUERY_DECISIONS: decisões executivas em aberto ou sob análise.
- QUERY_RECAP: balanço / fechamento do dia.
- CREATE_EVENT: pedido para CRIAR um evento/reunião/compromisso novo no Google Calendar.
- COMPLETE_FOCUS: relato de conclusão de um item que já está no Cockpit ("boleto da Gasnorte pago", "já resolvi o contrato").
- SYNC_WORKSPACE: "sincronizar", "atualizar cockpit", "sync".
- GREETING: "olá", "bom dia", "oi".
- QUERY_GENERAL: perguntas conceituais / abertas ("o que decidimos sobre X?", "quem é fulano?").
- CAPTURE_NOTE: afirmações que registram algo novo no Obsidian. NUNCA para perguntas.

EXEMPLOS:
"o que temos para o dia de hoje?" -> QUERY_COCKPIT
"quero saber o que eu preciso fazer hoje" -> QUERY_COCKPIT
"o que tem pra hoje" -> QUERY_COCKPIT
"como está meu dia?" -> QUERY_COCKPIT
"minhas reuniões de hoje" -> QUERY_AGENDA
"tenho algo às 15h?" -> QUERY_AGENDA
"quais decisões estão paradas?" -> QUERY_DECISIONS
"fechamento do dia" -> QUERY_RECAP
"marca uma reunião com o Carlos amanhã às 15h" -> CREATE_EVENT
"agenda uma call com o time de produto na sexta 10h" -> CREATE_EVENT
"cria um evento de revisão orçamentária dia 20 às 9h" -> CREATE_EVENT
"boleto da Luzsul pago hoje" -> COMPLETE_FOCUS
"a inscrição no evento de mobilidade elétrica foi cancelada" -> CAPTURE_NOTE
"a apresentação da Meridian foi para 21/09/2026" -> CAPTURE_NOTE
"ideia: criar um comitê de inovação trimestral" -> CAPTURE_NOTE
"novo projeto de fusão com a Beta, prazo outubro, stakeholder Carlos" -> CAPTURE_NOTE
"lembra que o Jonas é o VP de novos negócios da Acme Corp" -> CAPTURE_NOTE
"bom dia" -> GREETING
"o que a gente decidiu sobre o biobanco?" -> QUERY_GENERAL
"sincroniza o cockpit agora" -> SYNC_WORKSPACE

CATEGORIAS DINÂMICAS DISPONÍVEIS:
{categories_json}

CONCEITOS E ENTIDADES APRENDIDOS:
{learned_concepts_json}

HISTÓRICO RECENTE DA CONVERSA (mais antigo -> mais recente; use para resolver follow-ups curtos):
{recent_turns_text}
{reconsider_block}
Responda estritamente em JSON:
{{
  "intent": "QUERY_COCKPIT" | "QUERY_AGENDA" | "QUERY_DECISIONS" | "QUERY_RECAP" | "CREATE_EVENT" | "COMPLETE_FOCUS" | "SYNC_WORKSPACE" | "CAPTURE_NOTE" | "GREETING" | "QUERY_GENERAL",
  "is_question": true | false,
  "confidence": 0.0 a 1.0,
  "reasoning": "1 frase curta do porquê",
  "suggested_category": "categoria_slug" ou null,
  "detected_entities": ["Entidade1", "Entidade2"]
}}
"""

_RECONSIDER_BLOCK = (
    "\nATENÇÃO: @@USER_FIRST@@ parece estar repetindo ou reformulando o mesmo pedido, o que "
    "indica que a classificação anterior provavelmente ERROU. Reavalie com cuidado — é "
    "muito provável que ele esteja PERGUNTANDO algo (QUERY_COCKPIT / QUERY_AGENDA / "
    "QUERY_GENERAL), não pedindo para registrar uma nota.\n"
)


class AdaptiveClassifier:
    """Classifies user messages semantically using dynamic vault taxonomy and learned memories."""

    def __init__(self):
        self.ai = gemini_ai

    def keyword_query_intent(self, text: str) -> Optional[IntentType]:
        """Deterministic map from unambiguous phrasing to a QUERY_*/SYNC/GREETING intent.

        Returns None when nothing matches strongly — those go to the LLM. Used as a
        guardrail on the model output and by the offline fallback, never to skip the LLM.
        """
        n = normalize_text(text)
        if not n:
            return None
        if n in _GREETING_EXACT:
            return IntentType.GREETING
        if n == "sync" or "sincroniz" in n or "atualizar cockpit" in n or "atualiza o cockpit" in n:
            return IntentType.SYNC_WORKSPACE
        if any(k in n for k in _COCKPIT_CUES):
            return IntentType.QUERY_COCKPIT
        if any(k in n for k in _EVENT_CREATE_CUES):
            return IntentType.CREATE_EVENT
        if any(k in n for k in _AGENDA_CUES):
            return IntentType.QUERY_AGENDA
        if any(k in n for k in _DECISION_CUES):
            return IntentType.QUERY_DECISIONS
        if any(k in n for k in _RECAP_CUES):
            return IntentType.QUERY_RECAP
        return None

    @staticmethod
    def _format_recent_turns(recent_turns: Optional[list[dict[str, Any]]]) -> str:
        if not recent_turns:
            return "(sem histórico recente)"
        lines = []
        for t in recent_turns[-8:]:
            age = t.get("age_min")
            age_s = f"{age}min atrás" if isinstance(age, int) else "?"
            txt = (t.get("text") or "").replace("\n", " ")[:120]
            lines.append(f'- ({age_s}) "{txt}" -> {t.get("intent", "?")}')
        return "\n".join(lines)

    async def classify(
        self,
        user_input: str,
        user_id: int = 0,
        recent_turns: Optional[list[dict[str, Any]]] = None,
        escalate: bool = False,
    ) -> IntentResult:
        """Classifies input with Gemini at temperature 0.0, with deterministic guardrails."""
        text = user_input.strip()
        if not text:
            return IntentResult(intent=IntentType.GREETING, confidence=1.0, reasoning="Entrada vazia")

        # Fallback if Gemini offline
        if not self.ai.is_available():
            return self._heuristic_fallback(text)

        # 1. Gather dynamic categories and learned concepts. The category scan can hit
        # the Google Drive mount on a cold cache, so keep it off the event loop.
        categories = await asyncio.to_thread(adaptive_kb.discover_vault_categories)
        cat_summary = {
            k: f"{v['label']}: {v['description']}" for k, v in categories.items()
        }
        learned_concepts = await adaptive_kb.get_learned_concepts(limit=15)

        prompt = (
            CLASSIFIER_SYSTEM_PROMPT
            .replace("{categories_json}", json.dumps(cat_summary, ensure_ascii=False, indent=2))
            .replace("{learned_concepts_json}", json.dumps(learned_concepts, ensure_ascii=False, indent=2))
            .replace("{recent_turns_text}", self._format_recent_turns(recent_turns))
            .replace("{reconsider_block}", _RECONSIDER_BLOCK if escalate else "")
        )

        try:
            response = await asyncio.to_thread(
                self.ai.client.models.generate_content,
                model=self.ai.lite_model_name,
                contents=f"INPUT DO USUÁRIO:\n{text}",
                config=types.GenerateContentConfig(
                    system_instruction=prompt,
                    response_mime_type="application/json",
                    temperature=0.0,
                ),
            )
            self.ai._record_usage(response, "intent_classification", model=self.ai.lite_model_name)
            raw_json = response.text or "{}"
            data = json.loads(raw_json)

            intent_str = str(data.get("intent", "CAPTURE_NOTE")).upper()
            try:
                intent_enum = IntentType(intent_str)
            except ValueError:
                intent_enum = IntentType.CAPTURE_NOTE

            confidence = float(data.get("confidence", 0.9))
            reasoning = data.get("reasoning", "")
            is_question = bool(data.get("is_question", False))

            # Guardrail 1: a question must never become a note. Remap to the specific
            # QUERY_* the phrasing implies, else answer it conversationally.
            if intent_enum == IntentType.CAPTURE_NOTE and (is_question or looks_interrogative(text)):
                remapped = self.keyword_query_intent(text)
                intent_enum = remapped or IntentType.QUERY_GENERAL
                reasoning = f"Guardrail: pergunta reclassificada de CAPTURE_NOTE para {intent_enum.value}"
                confidence = max(confidence, 0.7)

            # Guardrail 2: a capture the model is unsure about is answered as a question
            # instead of starting a sticky draft.
            elif intent_enum == IntentType.CAPTURE_NOTE and confidence < CAPTURE_MIN_CONFIDENCE:
                reasoning = f"Guardrail: CAPTURE_NOTE de baixa confiança ({confidence:.2f}) -> QUERY_GENERAL"
                intent_enum = IntentType.QUERY_GENERAL

            # Guardrail 3: a cancellation or a reschedule must never tick a Cockpit checkbox
            # as done. Reroute to CAPTURE_NOTE so the status change is recorded, not silently
            # marked complete (2026-09-15 incident: cancelled/postponed items were ticked done).
            elif intent_enum == IntentType.COMPLETE_FOCUS and looks_like_cancel_or_reschedule(text):
                reasoning = "Guardrail: cancelamento/remarcação reclassificado de COMPLETE_FOCUS para CAPTURE_NOTE"
                intent_enum = IntentType.CAPTURE_NOTE

            result = IntentResult(
                intent=intent_enum,
                confidence=confidence,
                reasoning=reasoning,
                suggested_category=data.get("suggested_category"),
                detected_entities=data.get("detected_entities", []),
            )

            # Record audit log (also the source of the conversation-context window)
            await adaptive_kb.record_feedback(
                user_id=user_id,
                raw_input=text,
                predicted_intent=result.intent.value,
                predicted_category=result.suggested_category,
                confidence=result.confidence,
                reasoning=result.reasoning,
                feedback_type="auto",
            )

            # Track detected entities for emergence detection
            for ent in result.detected_entities:
                await adaptive_kb.record_concept_observation(
                    term=ent,
                    concept_type="entity",
                    preferred_category=result.suggested_category,
                )

            return result

        except Exception as e:
            logger.error(f"Erro na classificação semântica Gemini: {e}")
            return self._heuristic_fallback(text)

    def _heuristic_fallback(self, text: str) -> IntentResult:
        """Deterministic safety fallback if Gemini API is unreachable."""
        mapped = self.keyword_query_intent(text)
        if mapped is not None:
            return IntentResult(intent=mapped, confidence=0.95, reasoning=f"{mapped.value} (heurística offline)")

        norm = normalize_text(text)
        completion_hit = any(k in norm for k in FOCUS_COMPLETION_MARKERS) or any(k in text.lower() for k in FOCUS_COMPLETION_MARKERS)
        if completion_hit and not looks_like_cancel_or_reschedule(text):
            return IntentResult(intent=IntentType.COMPLETE_FOCUS, confidence=0.9, reasoning="Conclusão de foco do Cockpit (fallback)")

        return IntentResult(intent=IntentType.CAPTURE_NOTE, confidence=0.8, reasoning="Captura de nota executiva (fallback)")


adaptive_classifier = AdaptiveClassifier()


# Fill the owner placeholders (@@USER_NAME@@ etc.) from settings once, at import time.
CLASSIFIER_SYSTEM_PROMPT = personalize(CLASSIFIER_SYSTEM_PROMPT)
_RECONSIDER_BLOCK = personalize(_RECONSIDER_BLOCK)
