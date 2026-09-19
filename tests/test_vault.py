"""Tests for Obsidian Vault Manager and Templates."""

from pathlib import Path
from datetime import datetime, date, timedelta
from rysos.vault.manager import VaultManager


def test_initialize_vault(tmp_path: Path):
    vault = VaultManager(vault_path=tmp_path)
    res = vault.initialize_vault_structure()

    assert res["status"] == "initialized"
    assert (tmp_path / "00_Cockpit" / "Daily").exists()
    assert (tmp_path / "01_Projects").exists()
    assert (tmp_path / "02_Areas").exists()
    assert (tmp_path / "02_Areas" / "Negocios_e_Governanca.md").exists()
    assert (tmp_path / "02_Areas" / "Saude_e_Vitalidade.md").exists()
    assert (tmp_path / "02_Areas" / "Patrimonio_e_Financas.md").exists()
    assert (tmp_path / "02_Areas" / "Familia_e_Relacionamentos.md").exists()
    assert (tmp_path / "07_Inbox_Agent").exists()


def test_create_daily_cockpit(tmp_path: Path):
    vault = VaultManager(vault_path=tmp_path)
    vault.initialize_vault_structure()

    today = date(2026, 9, 4)
    cockpit_path = vault.create_daily_cockpit(
        target_date=today,
        calendar_events=[
            {
                "summary": "Reunião de Diretoria",
                "start_time": datetime(2026, 9, 4, 10, 0),
                "end_time": datetime(2026, 9, 4, 11, 0),
                "attendees_str": "Carlos, Ana",
                "meet_link": "https://meet.google.com/abc-defg-hij",
                "context_brief": "Alinhamento de orçamento",
                "meeting_note_name": "2026-09-04 - Reuniao de Diretoria.md",
            }
        ],
        actionable_emails=[
            {
                "sender": "investidor@empresa.com",
                "subject": "Due Diligence M&A",
                "suggested_action": "Assinar termo de confidencialidade",
                "priority": "alta",
            }
        ],
    )

    assert cockpit_path.exists()
    content = cockpit_path.read_text(encoding="utf-8")
    assert "Reunião de Diretoria" in content
    assert "Due Diligence M&A" in content
    assert "priority: alta" in content


def test_create_flat_project(tmp_path: Path):
    vault = VaultManager(vault_path=tmp_path)
    proj_path = vault.create_project_note(
        title="Expansão M&A Alfa",
        outcome_description="Concluir aquisição estratégica da empresa Alfa até Q4.",
        front="estrategia_ma",
        priority="alta",
        deadline="2026-12-15",
    )

    assert proj_path.exists()
    content = proj_path.read_text(encoding="utf-8")
    assert "priority: alta" in content
    assert "Negocios_e_Governanca" in content
    assert "Expansão M&A Alfa" in content


def test_create_flat_project_resolves_related_decision_links(tmp_path: Path):
    vault = VaultManager(vault_path=tmp_path)
    vault.create_decision_note(
        decision_id="DEC-2026-001", title="Escolha do Provedor de Cloud",
        file_stem="DEC-2026-001 - Escolha do Provedor de Cloud",
    )
    proj_path = vault.create_project_note(
        title="Migração Cloud", related_decisions=["DEC-2026-001", "DEC-2026-999"],
    )
    content = proj_path.read_text(encoding="utf-8")
    # resolvable id -> real filename stem, so the link actually resolves
    assert "[[03_Decisions/DEC-2026-001 - Escolha do Provedor de Cloud|" in content
    # unresolvable id -> falls back to the bare id rather than erroring
    assert "[[03_Decisions/DEC-2026-999|" in content


def test_create_decision_note(tmp_path: Path):
    vault = VaultManager(vault_path=tmp_path)
    dec_path = vault.create_decision_note(
        decision_id="DEC-2026-001",
        title="Escolha do Provedor de Cloud",
        context="Avaliação de migração de infraestrutura",
        outcome="Opção por Google Cloud devido à integração com Gemini",
        priority="alta",
        status="decidido",
    )

    assert dec_path.exists()
    content = dec_path.read_text(encoding="utf-8")
    assert "DEC-2026-001" in content
    assert "Escolha do Provedor de Cloud" in content
    assert "priority: alta" in content

    # Cleanup: apagar a nota de decisão de teste criada
    dec_path.unlink()
    assert not dec_path.exists()


def test_archive_garbage_decisions_moves_only_descartado(tmp_path: Path):
    vault = VaultManager(vault_path=tmp_path)
    junk = vault.create_decision_note(
        decision_id="DEC-2026-900", title="Decisão duplicada", status="descartado",
        file_stem="DEC-2026-900 - Decisão duplicada",
    )
    keep = vault.create_decision_note(
        decision_id="DEC-2026-901", title="Decisão válida", status="decidido",
        file_stem="DEC-2026-901 - Decisão válida",
    )

    archived = vault.archive_garbage_decisions()

    assert archived == [("DEC-2026-900", tmp_path / "08_Archive" / "Decisions" / junk.name)]
    assert not junk.exists()
    assert (tmp_path / "08_Archive" / "Decisions" / junk.name).exists()
    assert keep.exists()  # untouched

    archived_content = (tmp_path / "08_Archive" / "Decisions" / junk.name).read_text(encoding="utf-8")
    assert "_Arquivada em " in archived_content


def test_archive_garbage_decisions_is_idempotent(tmp_path: Path):
    vault = VaultManager(vault_path=tmp_path)
    vault.create_decision_note(
        decision_id="DEC-2026-902", title="Lixo", status="descartado",
        file_stem="DEC-2026-902 - Lixo",
    )
    first = vault.archive_garbage_decisions()
    assert len(first) == 1

    second = vault.archive_garbage_decisions()
    assert second == []  # nothing left in 03_Decisions to re-archive

    archived_content = first[0][1].read_text(encoding="utf-8")
    assert archived_content.count("_Arquivada em ") == 1


def test_create_and_ensure_person_note(tmp_path: Path):
    vault = VaultManager(vault_path=tmp_path)
    vault.initialize_vault_structure()

    # 1. Create person note
    person_path = vault.create_person_note(
        name="Jonas Andrade",
        organization="Acme Corp",
        role="Vice-Presidente",
        email="vp@acme-corp.example",
    )
    assert person_path.exists()
    content = person_path.read_text(encoding="utf-8")
    assert "Jonas Andrade" in content
    assert "Acme Corp" in content
    assert "Vice-Presidente" in content
    assert "vp@acme-corp.example" in content
    assert "type: person" in content

    # 2. Ensure existing person note does not overwrite
    ensured_path = vault.ensure_person_note(
        name="Jonas Andrade",
        organization="Other Org",
    )
    assert ensured_path == person_path
    content2 = ensured_path.read_text(encoding="utf-8")
    assert "Acme Corp" in content2

    # 3. List people
    people = vault.list_people()
    assert len(people) == 1
    assert people[0]["name"] == "Jonas Andrade"
    assert people[0]["organization"] == "Acme Corp"
    assert people[0]["email"] == "vp@acme-corp.example"


def test_schedule_bill_reminder(tmp_path: Path):
    vault = VaultManager(vault_path=tmp_path)
    vault.initialize_vault_structure()

    due_date = date(2026, 9, 15)

    # 1. Schedule on a future date when cockpit does not yet exist
    note_path = vault.schedule_bill_reminder(
        due_date=due_date,
        beneficiary="Luzsul Distribuição",
        amount="R$ 189,50",
        sender="fatura@luzsul.example",
    )
    assert note_path.exists()
    content = note_path.read_text(encoding="utf-8")
    assert "Luzsul Distribuição" in content
    assert "R$ 189,50" in content
    assert "## 🎯 Focos de Alta Prioridade do Dia" in content

    # 2. Schedule another bill on the same date (cockpit already exists)
    vault.schedule_bill_reminder(
        due_date=due_date,
        beneficiary="Condomínio Edifício Aurora Park",
        amount="R$ 850,00",
        sender="adm@condominio.com",
    )
    content2 = note_path.read_text(encoding="utf-8")
    assert "Condomínio Edifício Aurora Park" in content2
    assert "R$ 850,00" in content2
    assert "Luzsul Distribuição" in content2

    # 3. Prevent duplicate insertion
    vault.schedule_bill_reminder(
        due_date=due_date,
        beneficiary="Luzsul Distribuição",
        amount="R$ 189,50",
    )
    content3 = note_path.read_text(encoding="utf-8")
    assert content3.count("Luzsul Distribuição") == 1


def test_schedule_bill_reminder_dedupes_across_label_drift(tmp_path: Path):
    """The same obligation re-arriving with a drifted LLM label / a relay sender must not
    stack a second line. Mirrors the real 2026-09-10 pollution."""
    vault = VaultManager(vault_path=tmp_path)
    vault.initialize_vault_structure()
    due = date(2026, 9, 20)

    # Construtora Modelo: three label variants, no amount, two different sender domains.
    p = vault.schedule_bill_reminder(
        due_date=due, beneficiary="Construtora Modelo Construtora e Imobiliária Ltda",
        sender='"Construtora Modelo Construtora e Imobiliária Ltda" <sendmail@mailer.example>')
    vault.schedule_bill_reminder(
        due_date=due, beneficiary="Construtora Modelo / Condomínio Bosque Verde",
        sender='"Construtora Modelo Construtora e Imobiliária \\"Ltda.\\"" <boletos@modelo.example>')
    vault.schedule_bill_reminder(
        due_date=due, beneficiary="Construtora Modelo Construtora e Imobiliária",
        sender="boletos@modelo.example")

    # Grupo Orion: same sender relay, accent/case/truncation drift on the label.
    vault.schedule_bill_reminder(
        due_date=due, beneficiary="GRUPO ORION EDUCACAO INFANTIL LTDA",
        amount="R$ 3.224,35", sender="GRUPO ORION <noreply@erp.example>")
    vault.schedule_bill_reminder(
        due_date=due, beneficiary="Grupo Escola Girassol Educação Infantil",
        amount="R$ 3.224,35", sender="GRUPO ORION <noreply@erp.example>")

    body = p.read_text(encoding="utf-8")
    bill_lines = [ln for ln in body.splitlines() if "Pagar boleto:" in ln]
    assert sum("modelo" in ln.lower() for ln in bill_lines) == 1
    assert sum("orion" in ln.lower() for ln in bill_lines) == 1

    # A genuinely different creditor sharing a leading token but with its own amount stays.
    vault.schedule_bill_reminder(
        due_date=due, beneficiary="Grupo Aurora assinatura",
        amount="R$ 79,90", sender="cobranca@grupoaurora.example")
    body2 = p.read_text(encoding="utf-8")
    assert "Grupo Aurora" in body2


def _seed_cockpit(vault: VaultManager, foci: list[dict]) -> Path:
    return vault.create_daily_cockpit(target_date=date.today(), high_priority_foci=foci)


def test_complete_cockpit_focus_matches_despite_accents_and_shape(tmp_path: Path):
    vault = VaultManager(vault_path=tmp_path)
    vault.initialize_vault_structure()
    cockpit = _seed_cockpit(vault, [
        {"title": "💳 Pagar boleto: Gasnorte", "area": "Patrimônio & Finanças", "description": "R$ 333,86 — Vencimento HOJE"},
        {"title": "Revisão orçamentária", "area": "Negócios & Governança", "description": "Conferir planilha com financeiro"},
    ])

    # "Gasnorte" (no accent), natural phrasing, different line shape than the file
    res = vault.complete_cockpit_focus("Boleto da Gasnorte pago hoje em débito automático")
    assert res["status"] == "completed"
    assert "Gasnorte" in res["item"]

    content = cockpit.read_text(encoding="utf-8")
    assert "- [x] **💳 Pagar boleto: Gasnorte**" in content
    assert "- [ ] **Revisão orçamentária**" in content  # untouched

    # Second identical report: already done, no error
    res2 = vault.complete_cockpit_focus("gásnorte já foi pago")
    assert res2["status"] == "already_done"


def test_complete_cockpit_focus_not_found_and_no_cockpit(tmp_path: Path):
    vault = VaultManager(vault_path=tmp_path)
    vault.initialize_vault_structure()

    assert vault.complete_cockpit_focus("qualquer coisa")["status"] == "no_cockpit"

    _seed_cockpit(vault, [{"title": "Contrato Acervo", "area": "Negócios & Governança", "description": "Assinar com comitê"}])
    res = vault.complete_cockpit_focus("paguei a conta de luz da Luzsul")
    assert res["status"] == "not_found"
    assert res["pending"] == ["Contrato Acervo"]


def test_complete_cockpit_focus_strict_needs_a_strong_match(tmp_path: Path):
    vault = VaultManager(vault_path=tmp_path)
    vault.initialize_vault_structure()
    _seed_cockpit(vault, [
        {"title": "Revisar minuta do contrato com o jurídico", "area": "Negócios & Governança", "description": "Exclusividade"},
        {"title": "💳 Pagar boleto: Gasnorte", "area": "Patrimônio & Finanças", "description": "Vencimento hoje"},
    ])

    # A phrase that only incidentally shares one word ("jurídico") with a focus:
    # the lenient path (explicit COMPLETE_FOCUS intent) acts on it...
    assert vault.complete_cockpit_focus("já resolvi com o jurídico", apply=False)["status"] == "completed"
    # ...but the strict path (mid-draft escape hatch) must not divert a draft on it.
    assert vault.complete_cockpit_focus("já resolvi com o jurídico", apply=False, strict=True)["status"] == "not_found"
    # A strong, unambiguous match still passes strict.
    assert vault.complete_cockpit_focus("boleto da Gasnorte foi pago", apply=False, strict=True)["status"] == "completed"


def test_complete_cockpit_focus_ambiguous(tmp_path: Path):
    vault = VaultManager(vault_path=tmp_path)
    vault.initialize_vault_structure()
    _seed_cockpit(vault, [
        {"title": "💳 Pagar boleto: Gasnorte", "area": "Patrimônio & Finanças", "description": "Gás de cozinha"},
        {"title": "💳 Pagar boleto: Gasnorte Residencial", "area": "Patrimônio & Finanças", "description": "Imóvel alugado"},
        {"title": "Contrato Acervo", "area": "Negócios & Governança", "description": "Assinar com comitê"},
    ])

    res = vault.complete_cockpit_focus("gásnorte pago")
    assert res["status"] == "ambiguous"
    assert set(res["candidates"]) == {"💳 Pagar boleto: Gasnorte", "💳 Pagar boleto: Gasnorte Residencial"}


def test_complete_cockpit_focus_bulk_marks_every_payment(tmp_path: Path):
    vault = VaultManager(vault_path=tmp_path)
    vault.initialize_vault_structure()
    cockpit = vault.create_daily_cockpit(
        target_date=date.today(),
        high_priority_foci=[
            {"title": "💳 Pagar boleto: Gazeta", "area": "Patrimônio & Finanças", "description": "Valor: A consultar — Conferir data de vencimento"},
            {"title": "💳 Pagar boleto: Banco Digital", "area": "Patrimônio & Finanças", "description": "Valor: A consultar — Conferir data de vencimento"},
            {"title": "Revisar minuta do contrato", "area": "Negócios & Governança", "description": "com o jurídico"},
        ],
        actionable_emails=[
            {"sender": '"Gazeta"', "subject": "[Pessoal - Financeiro] Atenção! Seu acesso foi suspenso", "suggested_action": "pagar", "priority": "alta"},
            {"sender": "Banco Digital", "subject": "[Pessoal - Financeiro] Sua fatura entrou em atraso", "suggested_action": "pagar", "priority": "alta"},
        ],
    )

    res = vault.complete_cockpit_focus("Todos os boletos e faturas estão pagos")
    assert res["status"] == "completed_bulk"
    assert len(res["items"]) == 4  # 2 bill foci + 2 bill-tagged critical e-mails

    content = cockpit.read_text(encoding="utf-8")
    assert "- [x] **💳 Pagar boleto: Gazeta**" in content
    assert "- [x] **💳 Pagar boleto: Banco Digital**" in content
    assert '- [x] **De:** "Gazeta"' in content
    assert "- [ ] **Revisar minuta do contrato**" in content  # non-payment focus untouched

    # Idempotent: saying it again finds nothing open, reports already-done
    assert vault.complete_cockpit_focus("todas as contas já foram pagas")["status"] == "already_done_bulk"


def test_complete_cockpit_focus_bulk_disabled_in_strict_mode(tmp_path: Path):
    vault = VaultManager(vault_path=tmp_path)
    vault.initialize_vault_structure()
    _seed_cockpit(vault, [
        {"title": "💳 Pagar boleto: Gazeta", "area": "Patrimônio & Finanças", "description": "Vencimento hoje"},
    ])
    # The mid-draft escape hatch must not sweep a whole class on a loose bulk phrase.
    assert vault.complete_cockpit_focus("todos pagos", apply=False, strict=True)["status"] == "not_found"


def test_complete_cockpit_focus_bulk_also_clears_future_reminders(tmp_path: Path):
    from datetime import timedelta
    vault = VaultManager(vault_path=tmp_path)
    vault.initialize_vault_structure()
    _seed_cockpit(vault, [
        {"title": "💳 Pagar boleto: Gazeta", "area": "Patrimônio & Finanças", "description": "Vencimento HOJE"},
    ])
    due = date.today() + timedelta(days=2)
    vault.schedule_bill_reminder(due_date=due, beneficiary="Banco Prime Exclusive", amount="R$ 28,25", sender="bancoprime")
    vault.schedule_bill_reminder(due_date=due, beneficiary="GRUPO ORION", amount="R$ 3.224,35", sender="erp")

    res = vault.complete_cockpit_focus("Todos os boletos estão pagos, inclusive os futuros")
    assert res["status"] == "completed_bulk"
    assert due.strftime("%Y-%m-%d") in res["future"]
    assert len(res["future"][due.strftime("%Y-%m-%d")]) == 2

    future_note = (tmp_path / "00_Cockpit" / "Daily" / f"{due.strftime('%Y-%m-%d')}.md").read_text(encoding="utf-8")
    assert "- [ ]" not in future_note.replace("- [ ] *Defina", "")  # every reminder ticked
    assert vault.complete_cockpit_focus("todas as contas quitadas")["status"] == "already_done_bulk"


def test_complete_cockpit_focus_bulk_leaves_far_future_untouched(tmp_path: Path):
    from datetime import timedelta
    vault = VaultManager(vault_path=tmp_path)
    vault.initialize_vault_structure()
    _seed_cockpit(vault, [
        {"title": "💳 Pagar boleto: Gazeta", "area": "Patrimônio & Finanças", "description": "Vencimento HOJE"},
    ])
    far = date.today() + timedelta(days=500)
    vault.schedule_bill_reminder(due_date=far, beneficiary="Seguro anual", amount="R$ 1.000", sender="porto")

    res = vault.complete_cockpit_focus("todos os boletos pagos")
    assert far.strftime("%Y-%m-%d") not in res.get("future", {})  # beyond the 400-day horizon
    far_note = (tmp_path / "00_Cockpit" / "Daily" / f"{far.strftime('%Y-%m-%d')}.md").read_text(encoding="utf-8")
    assert "[ ]" in far_note and "Seguro anual" in far_note  # still pending, untouched


def test_daily_cockpit_regen_keeps_paid_bill_when_render_tail_changes(tmp_path: Path):
    vault = VaultManager(vault_path=tmp_path)
    vault.initialize_vault_structure()
    _seed_cockpit(vault, [
        {"title": "💳 Pagar boleto: Gazeta", "area": "Patrimônio & Finanças", "description": "Valor: A consultar — Conferir data de vencimento"},
    ])
    vault.complete_cockpit_focus("boleto do Gazeta pago")

    # Re-sync: the same bill now renders with a resolved amount and a due-date phrasing.
    cockpit = _seed_cockpit(vault, [
        {"title": "💳 Pagar boleto: Gazeta", "area": "Patrimônio & Finanças", "description": "Valor: R$ 89,90 — Vencimento HOJE (2026-09-10)"},
    ])
    content = cockpit.read_text(encoding="utf-8")
    assert "- [x] **💳 Pagar boleto: Gazeta**" in content
    assert content.count("Pagar boleto: Gazeta") == 1  # not duplicated


def test_daily_cockpit_regen_preserves_completed_checkbox(tmp_path: Path):
    vault = VaultManager(vault_path=tmp_path)
    vault.initialize_vault_structure()
    focus = {"title": "💳 Pagar boleto: Gasnorte", "area": "Patrimônio & Finanças", "description": "R$ 333,86 — Vencimento HOJE"}
    _seed_cockpit(vault, [focus])
    vault.complete_cockpit_focus("gásnorte pago")

    # A re-sync regenerates the file from the same template/data
    cockpit = _seed_cockpit(vault, [focus])
    content = cockpit.read_text(encoding="utf-8")
    assert "- [x] **💳 Pagar boleto: Gasnorte**" in content
    assert content.count("Pagar boleto: Gasnorte") == 1


def test_daily_cockpit_regen_keeps_manually_captured_focus(tmp_path: Path):
    vault = VaultManager(vault_path=tmp_path)
    vault.initialize_vault_structure()
    today_str = date.today().strftime("%Y-%m-%d")

    _seed_cockpit(vault, [])  # cockpit exists, no email-derived foci
    vault.save_quick_capture(
        category="today", title="Ligar para o cartório", content="x", target_date=today_str,
        fields={"title": "Ligar para o cartório", "description": "Confirmar a lavratura da escritura", "date": today_str},
    )
    vault.complete_cockpit_focus("liguei para o cartório")

    # Next /sync regenerates with NO high_priority_foci at all
    cockpit = vault.create_daily_cockpit(target_date=date.today(), high_priority_foci=[])
    content = cockpit.read_text(encoding="utf-8")
    assert "- [x] **Ligar para o cartório**" in content
    assert content.count("Ligar para o cartório") == 1


def test_create_daily_cockpit_carries_forward_unfinished_focus(tmp_path: Path):
    vault = VaultManager(vault_path=tmp_path)
    vault.initialize_vault_structure()
    yesterday = date(2026, 9, 14)
    today = date(2026, 9, 15)

    vault.create_daily_cockpit(
        target_date=yesterday,
        high_priority_foci=[
            {"title": "Agendar reunião com Vanessa", "area": "Follow-up", "description": "governança"},
        ],
    )
    cockpit = vault.create_daily_cockpit(target_date=today, high_priority_foci=[])
    content = cockpit.read_text(encoding="utf-8")
    assert "Agendar reunião com Vanessa" in content
    assert "⏳ *(pendente desde 14/09/2026)*" in content
    assert "- [ ]" in content


def test_create_daily_cockpit_does_not_carry_forward_completed_focus(tmp_path: Path):
    vault = VaultManager(vault_path=tmp_path)
    vault.initialize_vault_structure()
    yesterday = date(2026, 9, 14)
    today = date(2026, 9, 15)

    vault.create_daily_cockpit(
        target_date=yesterday,
        high_priority_foci=[
            {"title": "Item resolvido", "area": "Follow-up", "description": "x"},
        ],
    )
    vault.complete_cockpit_focus("item resolvido", target_date=yesterday)

    cockpit = vault.create_daily_cockpit(target_date=today, high_priority_foci=[])
    content = cockpit.read_text(encoding="utf-8")
    assert "Item resolvido" not in content


def test_create_daily_cockpit_carry_forward_keeps_original_open_date(tmp_path: Path):
    vault = VaultManager(vault_path=tmp_path)
    vault.initialize_vault_structure()
    day1 = date(2026, 9, 13)
    day2 = date(2026, 9, 14)
    day3 = date(2026, 9, 15)

    vault.create_daily_cockpit(
        target_date=day1,
        high_priority_foci=[
            {"title": "Pendência antiga", "area": "Follow-up", "description": "x"},
        ],
    )
    vault.create_daily_cockpit(target_date=day2, high_priority_foci=[])
    cockpit = vault.create_daily_cockpit(target_date=day3, high_priority_foci=[])
    content = cockpit.read_text(encoding="utf-8")
    assert content.count("Pendência antiga") == 1
    assert "⏳ *(pendente desde 13/09/2026)*" in content
    assert "14/09/2026" not in content


def test_create_daily_cockpit_carry_forward_skips_duplicate(tmp_path: Path):
    vault = VaultManager(vault_path=tmp_path)
    vault.initialize_vault_structure()
    yesterday = date(2026, 9, 14)
    today = date(2026, 9, 15)

    vault.create_daily_cockpit(
        target_date=yesterday,
        high_priority_foci=[
            {"title": "💳 Pagar boleto: Luzsul", "area": "Patrimônio & Finanças", "description": "R$ 100"},
        ],
    )
    cockpit = vault.create_daily_cockpit(
        target_date=today,
        high_priority_foci=[
            {"title": "💳 Pagar boleto: Luzsul", "area": "Patrimônio & Finanças", "description": "R$ 100"},
        ],
    )
    content = cockpit.read_text(encoding="utf-8")
    assert content.count("Pagar boleto: Luzsul") == 1


def test_create_daily_cockpit_resync_does_not_re_trigger_rollover(tmp_path: Path):
    vault = VaultManager(vault_path=tmp_path)
    vault.initialize_vault_structure()
    yesterday = date(2026, 9, 14)
    today = date(2026, 9, 15)

    vault.create_daily_cockpit(
        target_date=yesterday,
        high_priority_foci=[
            {"title": "Pendência de ontem", "area": "Follow-up", "description": "x"},
        ],
    )
    cockpit = vault.create_daily_cockpit(target_date=today, high_priority_foci=[])
    assert "Pendência de ontem" in cockpit.read_text(encoding="utf-8")

    # Re-sync today again — must not duplicate the already-carried item.
    cockpit = vault.create_daily_cockpit(target_date=today, high_priority_foci=[])
    content = cockpit.read_text(encoding="utf-8")
    assert content.count("Pendência de ontem") == 1


def test_create_daily_cockpit_has_free_notes_section(tmp_path: Path):
    vault = VaultManager(vault_path=tmp_path)
    vault.initialize_vault_structure()
    cockpit = vault.create_daily_cockpit(target_date=date(2026, 9, 15))
    content = cockpit.read_text(encoding="utf-8")
    assert "## ✍️ Notas Livres do Dia" in content
    assert "<!-- notas-livres:start -->" in content
    assert "<!-- notas-livres:end -->" in content
    assert vault.extract_free_notes(content) == ""


def test_free_notes_survive_same_day_resync(tmp_path: Path):
    vault = VaultManager(vault_path=tmp_path)
    vault.initialize_vault_structure()
    today = date(2026, 9, 15)
    cockpit = vault.create_daily_cockpit(target_date=today)
    content = cockpit.read_text(encoding="utf-8")
    written = content.replace(
        "<!-- notas-livres:start -->\n\n<!-- notas-livres:end -->",
        "<!-- notas-livres:start -->\nHoje conversei com a Gisele sobre o projeto X.\n<!-- notas-livres:end -->",
    )
    assert written != content
    cockpit.write_text(written, encoding="utf-8")

    # Re-sync the same day (e.g. a manual /sync) must not wipe the free text.
    cockpit = vault.create_daily_cockpit(target_date=today, high_priority_foci=[])
    content2 = cockpit.read_text(encoding="utf-8")
    assert "Hoje conversei com a Gisele sobre o projeto X." in content2
    assert vault.extract_free_notes(content2) == "Hoje conversei com a Gisele sobre o projeto X."


def test_create_resource_note(tmp_path: Path):
    vault = VaultManager(vault_path=tmp_path)
    vault.initialize_vault_structure()
    path = vault.create_resource_note(
        title="Artigo sobre biobancos", summary="Resumo do artigo.", source="https://example.com",
    )
    assert path.exists()
    assert path.parent.name == "Leituras_e_Pesquisas"
    content = path.read_text(encoding="utf-8")
    assert "type: resource" in content
    assert "resource_type: generic" in content
    assert "Resumo do artigo." in content
    assert "https://example.com" in content


def test_ensure_resource_note_reuses_exact_title_match(tmp_path: Path):
    vault = VaultManager(vault_path=tmp_path)
    vault.initialize_vault_structure()
    first = vault.ensure_resource_note(title="Contrato XYZ", summary="Primeira menção.")
    second = vault.ensure_resource_note(title="contrato xyz", summary="Segunda menção, nome diferente.")
    assert first == second
    content = first.read_text(encoding="utf-8")
    assert "Primeira menção." in content
    assert "Segunda menção" not in content
    assert len(list((tmp_path / "06_Resources" / "Leituras_e_Pesquisas").glob("*.md"))) == 1


def test_resolve_area_folds_all_spellings():
    assert VaultManager._resolve_area("Patrimônio & Finanças") == ("Patrimonio_e_Financas", "Patrimônio & Finanças")
    assert VaultManager._resolve_area("Patrimonio_e_Financas") == ("Patrimonio_e_Financas", "Patrimônio & Finanças")
    assert VaultManager._resolve_area("saúde & vitalidade") == ("Saude_e_Vitalidade", "Saúde & Vitalidade")
    assert VaultManager._resolve_area("Família e Relacionamentos") == ("Familia_e_Relacionamentos", "Família & Relacionamentos")
    assert VaultManager._resolve_area(None) == ("Negocios_e_Governanca", "Negócios & Governança")
    assert VaultManager._resolve_area("algo desconhecido") == ("Negocios_e_Governanca", "Negócios & Governança")


def test_save_quick_capture_project_renders_template_from_fields(tmp_path: Path):
    vault = VaultManager(vault_path=tmp_path)
    vault.initialize_vault_structure()

    path = vault.save_quick_capture(
        category="project",
        title="Expansão Acervo",
        content="prosa livre do LLM que NÃO deve ser gravada",
        fields={
            "title": "Expansão Acervo",
            "area": "Patrimônio & Finanças",
            "deadline": "2026-12-31",
            "outcome_description": "Estruturar o veículo de equity do biobanco.",
            "stakeholders": ["Dra. Renata", "Jonas Andrade"],
            "priority": "alta",
        },
    )
    assert path == tmp_path / "01_Projects" / "Expansão Acervo.md"
    body = path.read_text(encoding="utf-8")
    assert "type: project" in body
    assert "[[02_Areas/Patrimonio_e_Financas|Patrimônio & Finanças]]" in body
    assert "2026-12-31" in body
    assert "Dra. Renata" in body and "Jonas Andrade" in body
    assert "prosa livre do LLM" not in body


def test_save_quick_capture_decision_template_keeps_id_clean(tmp_path: Path):
    vault = VaultManager(vault_path=tmp_path)
    vault.initialize_vault_structure()

    path = vault.save_quick_capture(
        category="decision",
        title="Modelo Societário Brasil-China",
        content="ignore",
        fields={
            "area": "Negócios & Governança",
            "context": "Definir se a parceria entra via equity ou joint venture.",
            "outcome": "Em discussão com o comitê.",
            "status": "em_analise",
            "priority": "alta",
        },
    )
    assert path.parent == tmp_path / "03_Decisions"
    assert path.name.startswith("DEC-") and "Modelo Societário Brasil-China" in path.name
    body = path.read_text(encoding="utf-8")
    assert "type: decision" in body
    assert "status: em_analise" in body
    # frontmatter id stays the bare DEC id, without the title appended
    import re as _re
    assert _re.search(r"^id: DEC-\d{4}-\d{5}$", body, _re.MULTILINE)


def test_save_quick_capture_idea_and_framework_use_templates(tmp_path: Path):
    vault = VaultManager(vault_path=tmp_path)
    vault.initialize_vault_structure()

    idea = vault.save_quick_capture(
        category="idea", title="Cockpit por Voz", content="x",
        fields={"overview": "Operar o cockpit falando.", "rationale": "Menos fricção.",
                "next_steps": ["Mapear intents de voz", "Prototipar no Telegram"]},
    )
    ibody = idea.read_text(encoding="utf-8")
    assert "resource_type: idea" in ibody
    assert "- [ ] Mapear intents de voz" in ibody

    fw = vault.save_quick_capture(
        category="framework", title="Fast Forward Design", content="x",
        fields={"description": "Ideação divergente e convergente.", "components": "Divergir; Convergir; Prototipar"},
    )
    fbody = fw.read_text(encoding="utf-8")
    assert "resource_type: framework" in fbody
    assert "- Divergir" in fbody and "- Prototipar" in fbody


def test_save_quick_capture_degraded_without_fields_is_freeform(tmp_path: Path):
    vault = VaultManager(vault_path=tmp_path)
    vault.initialize_vault_structure()

    raw = "# Expansão Acervo\n\nRascunho bruto capturado offline."
    path = vault.save_quick_capture(
        category="project", title="Expansão Acervo", content=raw,
        fields={"title": "Expansão Acervo"},  # title only, no body fields
    )
    assert path == tmp_path / "01_Projects" / "Expansão Acervo.md"
    body = path.read_text(encoding="utf-8")
    assert body == raw
    assert "type: project" not in body  # no hollow template


def test_save_quick_capture_person_never_clobbers_existing_note(tmp_path: Path):
    vault = VaultManager(vault_path=tmp_path)
    vault.initialize_vault_structure()

    existing = vault.create_person_note(name="Jonas Andrade", organization="Acme Corp", role="VP")
    enriched = existing.read_text(encoding="utf-8") + "\n## 🤝 Reuniões & Interações Recentes\n- 2026-08-01 Kickoff\n"
    vault.write_file_atomic(existing, enriched)

    path = vault.save_quick_capture(
        category="person", title="Jonas Andrade", content="nova prosa",
        fields={"name": "Jonas Andrade", "organization": "Acme Corp", "role": "VP de Novos Negócios"},
    )
    assert path == existing
    assert path.read_text(encoding="utf-8") == enriched  # untouched


def test_save_quick_capture_today_dedups_focus_line(tmp_path: Path):
    vault = VaultManager(vault_path=tmp_path)
    vault.initialize_vault_structure()
    today_str = date.today().strftime("%Y-%m-%d")
    fields = {"title": "Assinar contrato do Acervo", "area": "Negócios & Governança",
              "description": "Revisar cláusulas com o jurídico", "date": today_str}

    p1 = vault.save_quick_capture(category="today", title=fields["title"], content="x", target_date=today_str, fields=fields)
    vault.save_quick_capture(category="today", title=fields["title"], content="x", target_date=today_str, fields=fields)
    body = p1.read_text(encoding="utf-8")
    assert body.count("Assinar contrato do Acervo") == 1


# --- create_meeting_note: idempotent re-sync + slug-collision (workbench gaps G19/G22) ---

def _meeting(vault: VaultManager, summary: str, brief: str, *, event_id: str | None = None, hour: int = 10):
    return vault.create_meeting_note(
        summary=summary,
        start_time=datetime(2026, 9, 8, hour, 0),
        end_time=datetime(2026, 9, 8, hour + 1, 0),
        attendees=["Carlos", "Ana"],
        meet_link="https://meet.google.com/abc-defg-hij",
        context_brief=brief,
        priority="alta",
        event_id=event_id,
    )


def test_create_meeting_note_fresh_has_all_sections(tmp_path: Path):
    vault = VaultManager(vault_path=tmp_path)
    vault.initialize_vault_structure()
    p = _meeting(vault, "Comitê Acervo", "Alinhar equity split", event_id="evt-1")
    body = p.read_text(encoding="utf-8")
    assert p.name == "2026-09-08 - Comitê Acervo.md"
    assert 'event_id: "evt-1"' in body
    assert "## 🎯 Pauta & Contexto Prévio (rysOS Brief)" in body
    assert "Alinhar equity split" in body
    assert "## 📝 Notas & Discussão" in body
    assert "## ✅ Ações & Follow-ups Decididos" in body


def test_create_meeting_note_resync_preserves_human_sections(tmp_path: Path):
    vault = VaultManager(vault_path=tmp_path)
    vault.initialize_vault_structure()
    p = _meeting(vault, "Comitê Acervo", "Brief antigo", event_id="evt-1")

    edited = p.read_text(encoding="utf-8").replace(
        "## 📝 Notas & Discussão\n- ",
        "## 📝 Notas & Discussão\n- Jonas topou o modelo via equity\n- Rediscutir valuation em out",
    ).replace(
        "## ✅ Ações & Follow-ups Decididos\n- [ ] ",
        "## ✅ Ações & Follow-ups Decididos\n- [x] Enviar minuta\n- [ ] Agendar due diligence",
    )
    p.write_text(edited, encoding="utf-8")

    # Re-sync: same event, a refreshed brief and a changed priority.
    p2 = vault.create_meeting_note(
        summary="Comitê Acervo",
        start_time=datetime(2026, 9, 8, 10, 0),
        end_time=datetime(2026, 9, 8, 11, 0),
        attendees=["Carlos", "Ana", "Jonas"],
        meet_link="https://meet.google.com/abc-defg-hij",
        context_brief="Brief NOVO com a pauta atualizada",
        priority="media",
        event_id="evt-1",
    )
    assert p2 == p
    body = p.read_text(encoding="utf-8")
    assert "Jonas topou o modelo via equity" in body          # human notes kept
    assert "- [x] Enviar minuta" in body                       # decided actions kept
    assert "- [ ] Agendar due diligence" in body
    assert "Brief NOVO com a pauta atualizada" in body         # machine brief refreshed
    assert "Brief antigo" not in body
    assert "priority/media" in body


def test_create_meeting_note_slug_collision_keeps_distinct_events_apart(tmp_path: Path):
    vault = VaultManager(vault_path=tmp_path)
    vault.initialize_vault_structure()
    a = _meeting(vault, "Rafael Moreira", "Convite DLAB manhã", event_id="evt-A", hour=10)
    b = _meeting(vault, "Rafael Moreira", "Convite DLAB remarcado", event_id="evt-B", hour=15)
    assert a != b
    assert a.name == "2026-09-08 - Rafael Moreira.md"
    assert b.name == "2026-09-08 - Rafael Moreira (2).md"
    assert "Convite DLAB manhã" in a.read_text(encoding="utf-8")
    assert "Convite DLAB remarcado" in b.read_text(encoding="utf-8")

    # The same event re-syncs into its own file, not a third one.
    b2 = _meeting(vault, "Rafael Moreira", "Convite DLAB remarcado (v2)", event_id="evt-B", hour=15)
    assert b2 == b
    assert not (tmp_path / "04_Meetings" / "2026-09-08 - Rafael Moreira (3).md").exists()


def test_create_meeting_note_same_meeting_different_event_id_merges(tmp_path: Path):
    """The same real meeting arriving under a second calendar event_id (duplicate invite,
    organizer + attendee copies) merges into one note — same start time + same attendee
    set adopts the existing file instead of spawning `... (2).md`."""
    vault = VaultManager(vault_path=tmp_path)
    vault.initialize_vault_structure()
    a = vault.create_meeting_note(
        summary="Rafael Moreira",
        start_time=datetime(2026, 9, 9, 10, 0),
        end_time=datetime(2026, 9, 9, 11, 0),
        attendees=["Rafael Moreira", "Karina Guedes", "Antônio Arruda"],
        context_brief="cópia A", priority="alta", event_id="acc@gmail.com_aaa111",
    )
    b = vault.create_meeting_note(
        summary="Rafael Moreira",
        start_time=datetime(2026, 9, 9, 10, 0),
        end_time=datetime(2026, 9, 9, 11, 0),
        attendees=["Antônio Arruda", "Rafael Moreira", "Karina Guedes"],  # same set, reordered
        context_brief="cópia B", priority="alta", event_id="acc@gmail.com_bbb222",
    )
    assert b == a
    assert not (tmp_path / "04_Meetings" / "2026-09-09 - Rafael Moreira (2).md").exists()


def test_create_meeting_note_same_slug_different_attendees_stays_apart(tmp_path: Path):
    """Same title + start but a genuinely different attendee set is a different meeting."""
    vault = VaultManager(vault_path=tmp_path)
    vault.initialize_vault_structure()
    a = vault.create_meeting_note(
        summary="Sync Semanal", start_time=datetime(2026, 9, 9, 9, 0),
        end_time=datetime(2026, 9, 9, 9, 30), attendees=["Rafael", "Jonas"],
        context_brief="com Jonas", priority="media", event_id="evt-A",
    )
    b = vault.create_meeting_note(
        summary="Sync Semanal", start_time=datetime(2026, 9, 9, 9, 0),
        end_time=datetime(2026, 9, 9, 9, 30), attendees=["Rafael", "Marina"],
        context_brief="com Marina", priority="media", event_id="evt-B",
    )
    assert b != a
    assert b.name == "2026-09-09 - Sync Semanal (2).md"


def test_find_matching_calendar_note_prefers_filename_over_attendee_overlap(tmp_path: Path):
    """Rafael's convention: the dropped .txt is named after the note's exact title.
    That deterministic filename match must win even when attendee overlap alone
    would be ambiguous or point elsewhere (2026-09-15 Terapia/Helix incident)."""
    vault = VaultManager(vault_path=tmp_path)
    vault.initialize_vault_structure()
    _meeting(vault, "Terapia com Fabiana", "brief", event_id="evt-1")
    _meeting(vault, "Outra Reunião", "brief", event_id="evt-2", hour=15)

    match = vault.find_matching_calendar_note(
        "2026-09-08", attendee_names=[], filename_stem="2026-09-08 Terapia com Fabiana",
    )
    assert match is not None
    assert match.name == "2026-09-08 - Terapia com Fabiana.md"


def test_find_matching_calendar_note_attendee_overlap_matches_first_names(tmp_path: Path):
    """A transcript citing first names/nicknames ("Boris", "Vivi") must still overlap
    against the calendar invite's full names ("Boris Lemos", "Vanessa Salgado") — exact
    whole-string equality between a first name and a full name never matches."""
    vault = VaultManager(vault_path=tmp_path)
    vault.initialize_vault_structure()
    helix = vault.create_meeting_note(
        summary="Helix Saúde",
        start_time=datetime(2026, 9, 8, 11, 0),
        end_time=datetime(2026, 9, 8, 11, 45),
        attendees=["Rafael Moreira", "Boris Lemos", "Vanessa Salgado"],
        context_brief="brief", priority="baixa", event_id="evt-helix",
    )
    _meeting(vault, "Psicopedagoga Be", "brief", event_id="evt-other", hour=9)

    match = vault.find_matching_calendar_note(
        "2026-09-08", attendee_names=["Boris", "Gabi"], filename_stem="2026-09-08 Alinhamento de Sócios",
    )
    assert match == helix


def test_create_meeting_note_unknown_layout_is_never_clobbered(tmp_path: Path):
    vault = VaultManager(vault_path=tmp_path)
    vault.initialize_vault_structure()
    p = _meeting(vault, "Sessão livre", "brief", event_id="evt-1")
    p.write_text("# Minhas anotações totalmente reescritas\n\nsem marcadores do template\n", encoding="utf-8")

    _meeting(vault, "Sessão livre", "brief novo", event_id="evt-1")
    assert p.read_text(encoding="utf-8") == "# Minhas anotações totalmente reescritas\n\nsem marcadores do template\n"



def test_rename_meeting_note_wikilink_safe_updates_backlinks_and_frontmatter(tmp_path: Path):
    vault = VaultManager(vault_path=tmp_path)
    vault.initialize_vault_structure()
    mp = _meeting(vault, "Kickoff Lia", "brief", event_id="evt-1")
    assert mp.name == "2026-09-08 - Kickoff Lia.md"
    old_stem = mp.stem

    # a person note and a project note that link to the meeting by its stem
    person = tmp_path / "05_People" / "Rafael Moreira.md"
    person.write_text(f"# Rafael\n\nVer [[04_Meetings/{old_stem}|Kickoff Lia]] e [[{old_stem}]].\n", encoding="utf-8")
    triage = tmp_path / "07_Inbox_Agent" / "Triagem" / f"{old_stem}.md"
    triage.parent.mkdir(parents=True, exist_ok=True)
    triage.write_text(f'meeting: "[[04_Meetings/{old_stem}|Kickoff Lia]]"\n', encoding="utf-8")

    new_path = vault.rename_meeting_note_wikilink_safe(mp, "2026-08-15")
    assert new_path is not None and new_path.name == "2026-08-15 - Kickoff Lia.md"
    assert not mp.exists()

    body = new_path.read_text(encoding="utf-8")
    assert "date: 2026-08-15" in body
    assert "id: MEET-2026-08-15-Kickoff Lia" in body

    pbody = person.read_text(encoding="utf-8")
    assert "[[04_Meetings/2026-08-15 - Kickoff Lia|Kickoff Lia]]" in pbody
    assert "[[2026-08-15 - Kickoff Lia]]" in pbody
    assert old_stem not in pbody
    assert "[[04_Meetings/2026-08-15 - Kickoff Lia|" in triage.read_text(encoding="utf-8")


def test_rename_meeting_note_noop_when_date_already_matches(tmp_path: Path):
    vault = VaultManager(vault_path=tmp_path)
    vault.initialize_vault_structure()
    mp = _meeting(vault, "Kickoff Lia", "brief", event_id="evt-1")
    assert vault.rename_meeting_note_wikilink_safe(mp, "2026-09-08") == mp
    assert mp.exists()


def test_rename_meeting_note_bails_on_name_collision(tmp_path: Path):
    vault = VaultManager(vault_path=tmp_path)
    vault.initialize_vault_structure()
    mp = _meeting(vault, "Kickoff Lia", "brief", event_id="evt-1", hour=10)
    (tmp_path / "04_Meetings" / "2026-08-15 - Kickoff Lia.md").write_text("ocupado", encoding="utf-8")
    assert vault.rename_meeting_note_wikilink_safe(mp, "2026-08-15") is None
    assert mp.exists()  # untouched


def _meeting_at(vault: VaultManager, summary: str, start_time: datetime, *, event_id: str):
    return vault.create_meeting_note(
        summary=summary,
        start_time=start_time,
        end_time=start_time + timedelta(hours=1),
        attendees=["Carlos"],
        context_brief="brief",
        priority="alta",
        event_id=event_id,
    )


def test_find_stale_meeting_notes_prunes_by_start_time_not_creation_time(tmp_path: Path):
    vault = VaultManager(vault_path=tmp_path)
    vault.initialize_vault_structure()
    now = datetime.now()

    old_no_ata = _meeting_at(vault, "Reuniao Antiga", now - timedelta(hours=30), event_id="evt-old")
    recent_no_ata = _meeting_at(vault, "Reuniao Recente", now - timedelta(hours=2), event_id="evt-recent")
    future = _meeting_at(vault, "Reuniao Futura", now + timedelta(hours=3), event_id="evt-future")

    stale = vault.find_stale_meeting_notes(hours=24)
    assert stale == [old_no_ata]
    assert recent_no_ata not in stale
    assert future not in stale


def test_find_stale_meeting_notes_keeps_notes_with_transcript(tmp_path: Path):
    vault = VaultManager(vault_path=tmp_path)
    vault.initialize_vault_structure()
    now = datetime.now()
    path = _meeting_at(vault, "Com Ata", now - timedelta(hours=30), event_id="evt-ata")
    content = vault.read_file(path)
    vault.write_file_atomic(path, content + "\n<!-- transcript:abc123 -->\nAta real.\n")

    assert vault.find_stale_meeting_notes(hours=24) == []


def test_find_stale_meeting_notes_keeps_hand_typed_notes(tmp_path: Path):
    vault = VaultManager(vault_path=tmp_path)
    vault.initialize_vault_structure()
    now = datetime.now()
    path = _meeting_at(vault, "Com Anotacao Manual", now - timedelta(hours=30), event_id="evt-manual")
    content = vault.read_file(path)
    patched = content.replace(
        "## 📝 Notas & Discussão\n- ",
        "## 📝 Notas & Discussão\n- Conversamos sobre o escopo.",
    )
    vault.write_file_atomic(path, patched)

    assert vault.find_stale_meeting_notes(hours=24) == []


def test_archive_stale_meeting_notes_moves_to_archive(tmp_path: Path):
    vault = VaultManager(vault_path=tmp_path)
    vault.initialize_vault_structure()
    now = datetime.now()
    old_no_ata = _meeting_at(vault, "Reuniao Antiga 2", now - timedelta(hours=30), event_id="evt-old2")

    archived = vault.archive_stale_meeting_notes(hours=24)

    assert archived == [tmp_path / "08_Archive" / "Meetings" / old_no_ata.name]
    assert not old_no_ata.exists()
    assert archived[0].exists()
