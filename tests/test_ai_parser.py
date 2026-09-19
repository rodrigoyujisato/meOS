"""Tests for AI Parser and Heuristic Fallbacks."""

from datetime import datetime, timezone
from rysos.ai.gemini import GeminiAIClient
from rysos.connectors.gmail import EmailData
from rysos.connectors.calendar import CalendarEvent


def test_heuristic_email_analysis_priority():
    ai = GeminiAIClient(api_key="")  # Force empty key to test fallback

    emails = [
        EmailData(
            id="1",
            thread_id="t1",
            subject="Urgente: Aprovação de Contrato M&A",
            sender="juridico@empresa.com",
            date=datetime.now(timezone.utc),
            snippet="Precisamos da sua decisão executiva até às 17h.",
            body_text="Texto do contrato...",
            is_starred=False,
        ),
        EmailData(
            id="2",
            thread_id="t2",
            subject="Newsletter Semanal de Tecnologia",
            sender="news@tech.com",
            date=datetime.now(timezone.utc),
            snippet="Confira as novidades da semana no mundo tech.",
            body_text="Conteúdo...",
            is_starred=False,
        ),
    ]

    analysis = ai._heuristic_email_analysis(emails)
    assert len(analysis) == 2
    assert analysis[0]["priority"] == "alta"
    assert analysis[1]["priority"] == "baixa"


def test_heuristic_meeting_briefs():
    ai = GeminiAIClient(api_key="")

    meetings = [
        CalendarEvent(
            id="ev1",
            summary="Comitê Executivo de Estratégia",
            start_time=datetime(2026, 9, 4, 14, 0),
            end_time=datetime(2026, 9, 4, 15, 0),
            attendees=["carlos@empresa.com", "ana@empresa.com", "bruno@empresa.com"],
            description="Revisão trimestral de metas.",
        )
    ]

    briefs = ai._heuristic_meeting_briefs(meetings)
    assert len(briefs) == 1
    assert briefs[0]["priority"] == "alta"
    # Echoes the invite's own description verbatim — never a guessed purpose.
    assert briefs[0]["context_brief"] == "Revisão trimestral de metas."

    # An invite with no agenda gets a factual non-answer, not an invented purpose.
    bare = ai.generate_meeting_briefs([CalendarEvent(
        id="ev2", summary="Terapia Be",
        start_time=datetime(2026, 9, 9, 16, 10), end_time=datetime(2026, 9, 9, 17, 10),
        attendees=["owner@x.com", "renata@x.com"], description="",
    )])
    assert len(bare) == 1
    assert bare[0]["id"] == "ev2"
    assert "sem pauta" in bare[0]["context_brief"].lower()
    assert "renata" not in bare[0]["context_brief"].lower()


def test_heuristic_email_analysis_bill_detection():
    ai = GeminiAIClient(api_key="")

    emails = [
        EmailData(
            id="bill1",
            thread_id="t_bill1",
            subject="Fatura Luzsul - Vencimento 15/09/2026",
            sender="atendimento@luzsul.example",
            account="owner.pessoal@gmail.com",
            account_type="pessoal",
            date=datetime.now(timezone.utc),
            snippet="Sua fatura de energia no valor de R$ 245,80 está disponível. Vencimento: 15/09/2026.",
            body_text="...",
            is_starred=False,
        ),
        EmailData(
            id="bill2",
            thread_id="t_bill2",
            subject="Boleto Condomínio Edifício Aurora Park",
            sender="financeiro@administradora.com.br",
            account="owner.pessoal@gmail.com",
            account_type="pessoal",
            date=datetime.now(timezone.utc),
            snippet="Segue anexo o boleto referente ao condomínio deste mês no valor de R$ 920,00 com vencimento até 10/10/2026.",
            body_text="...",
            is_starred=False,
        ),
    ]

    analysis = ai._heuristic_email_analysis(emails)
    assert len(analysis) == 2

    # First bill
    assert analysis[0]["is_bill"] is True
    assert analysis[0]["priority"] == "alta"
    assert analysis[0]["category"] == "pagamento"
    assert analysis[0]["related_area"] == "Patrimonio_e_Financas"
    assert analysis[0]["due_date"] == "2026-09-15"
    assert "245,80" in analysis[0]["amount"]
    assert "luzsul" in analysis[0]["beneficiary"].lower()

    # Second bill
    assert analysis[1]["is_bill"] is True
    assert analysis[1]["priority"] == "alta"
    assert analysis[1]["category"] == "pagamento"
    assert analysis[1]["related_area"] == "Patrimonio_e_Financas"
    assert analysis[1]["due_date"] == "2026-10-10"
    assert "920,00" in analysis[1]["amount"]


def test_conversational_note_prompt_rendering_no_keyerror():
    """Verify prompt formatting does not crash on curly braces or JSON structures."""
    ai = GeminiAIClient(api_key="")
    # Call with empty API key to trigger local fallback without crashing on prompt formatting
    res = ai.process_conversational_note(
        user_input="Ideia com chaves {exemplo} e categoria {category}",
        current_draft={"category": "idea", "title": "Nota {teste}"},
        history=[{"role": "user", "text": "Texto com {chaves}"}],
    )
    assert res is not None
    assert "category" in res

