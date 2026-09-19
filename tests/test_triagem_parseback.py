"""Phase 3: resolving transcript entities by editing the Triagem note (parse-back)."""

import json
from datetime import date

from rysos.ai.gemini import _coerce_minutes
from rysos.core import rysos_core
from rysos.db import get_db_session, EntityCandidate, DecisionRecord
from rysos.vault.triagem import read_triagem_resolutions, apply_pending_triagem


def _body(marker="reuniao"):
    return (
        f"Rafael: bom dia pessoal, vamos comecar a {marker}. "
        "Hoje precisamos alinhar o escopo do projeto, os proximos passos e quem fica responsavel por cada frente. "
        "Maria: perfeito, do meu lado ja adiantei a parte de contrato e mando o rascunho ainda esta semana. "
        "Rafael: otimo, entao fechamos que voce envia o rascunho e eu reviso antes da proxima call. "
        "Joao: eu cuido da integracao tecnica e trago um status na sexta. "
        "Rafael: combinado, obrigado a todos, encerramos por aqui."
    )


def _rich(**over):
    base = {
        "title": "Kickoff Parseback",
        "people": [
            {"name": "Rafael Moreira", "role": "Diretor", "org": "rysOS", "mentions": "conduziu"},
            {"name": "MarIa Lyma", "role": "Gerente", "org": "Fornecedor A", "mentions": "contrato"},
        ],
        "decisions": [{"title": "Seguir com fornecedor A", "status": "decidido", "rationale": "preço", "owner": "R"}],
    }
    base.update(over)
    return _coerce_minutes(base, meeting_year=2026)


def _patch_ai(monkeypatch, rich):
    monkeypatch.setattr(
        rysos_core.ai, "summarize_meeting_transcript",
        lambda text, filename="", *, mtime_date=None, today=None: dict(rich),
    )


async def _run(monkeypatch, name="kickoff 2026-09-05.txt", rich=None, body_marker="kickoff"):
    vault = rysos_core.vault
    inbox = vault.vault_path / "07_Inbox_Agent" / "Transcricoes"
    inbox.mkdir(parents=True, exist_ok=True)
    (inbox / name).write_text(_body(body_marker), encoding="utf-8")
    _patch_ai(monkeypatch, rich or _rich())
    await rysos_core.process_pending_transcripts(target_date=date(2026, 9, 6))
    return vault


def _triagem_path(vault):
    return next((vault.vault_path / "07_Inbox_Agent" / "Triagem").glob("*.md"))


def _set_resolucao(text: str, surface: str, value: str) -> str:
    """Edit the `resolução:` line under a candidate heading, the way a human would."""
    out, hit = [], False
    for ln in text.splitlines():
        if ln.startswith(f"- [ ] **{surface}**") or ln.startswith(f"- [x] **{surface}**"):
            hit = True
        elif hit and ln.strip().startswith("resolução:"):
            indent = ln[: len(ln) - len(ln.lstrip())]
            ln = f"{indent}resolução: {value}"
            hit = False
        out.append(ln)
    return "\n".join(out) + "\n"


async def _cands():
    from sqlalchemy import select
    async with get_db_session() as s:
        return (await s.execute(select(EntityCandidate))).scalars().all()


# --- read_triagem_resolutions -------------------------------------------------

def test_read_resolutions_parses_edited_lines_only():
    text = (
        "---\nstatus: pending\n---\n<!-- triagem:abcdef012345 -->\n"
        "## 👤 Pessoas\n"
        "- [ ] **Rafael Moreira**\n      resolução: NOVO | =<nome exato> | IGNORAR\n"
        "- [ ] **MarIa Lyma**\n      resolução: = Maria Lima\n"
        "- [ ] **Joao**\n      resolução: IGNORAR\n"
        "- [x] **Ana**\n      resolução: NOVO\n"
    )
    sha, res = read_triagem_resolutions(text)
    assert sha == "abcdef012345"
    by = {r["surface"]: r for r in res}
    assert "Rafael Moreira" not in by                       # untouched stub -> skipped
    assert by["MarIa Lyma"] == {"surface": "MarIa Lyma", "action": "link", "target": "Maria Lima"}
    assert by["Joao"]["action"] == "skip"
    assert by["Ana"]["action"] == "new"


# --- apply_pending_triagem --------------------------------------------------

async def test_parseback_new_link_ignore(tmp_path, monkeypatch):
    vault = await _run(monkeypatch)
    vault.create_person_note(name="Maria Lima", role="", organization="")  # link target
    tp = _triagem_path(vault)
    text = tp.read_text(encoding="utf-8")
    text = _set_resolucao(text, "Rafael Moreira", "NOVO")
    text = _set_resolucao(text, "MarIa Lyma", "= Maria Lima")
    text = _set_resolucao(text, "Seguir com fornecedor A", "IGNORAR")
    tp.write_text(text, encoding="utf-8")

    n = await apply_pending_triagem(vault)
    assert n == 3

    assert (vault.vault_path / "05_People" / "Rafael Moreira.md").exists()  # NOVO
    cands = {(c.kind, c.normalized_form): c for c in await _cands()}
    assert cands[("person", "rafael moreira")].status == "confirmed_new"
    assert cands[("person", "maria lyma")].status == "linked"
    assert cands[("person", "maria lyma")].resolved_to.endswith("Maria Lima.md")
    assert cands[("decision", "seguir com fornecedor a")].status == "dismissed"

    # nothing left pending -> the note is swept out of the inbox into the archive
    assert not tp.exists()
    archived = next((vault.vault_path / "08_Archive" / "Triagem").glob("*.md"))
    after = archived.read_text(encoding="utf-8")
    assert "status: resolved" in after and "status: pending" not in after
    assert "Triagem concluída em" in after
    assert "✅ confirmed_new" in after and "✅ linked" in after and "✅ dismissed" in after

    # meeting note: Rafael + Maria lines upgraded to wikilinks, tag gone
    body = next((vault.vault_path / "04_Meetings").glob("*.md")).read_text(encoding="utf-8")
    assert "[[05_People/Rafael Moreira|Rafael Moreira]]" in body
    assert "[[05_People/Maria Lima|MarIa Lyma]]" in body


async def test_parseback_archives_despite_lingering_extraction_failed_row(tmp_path, monkeypatch):
    """A bare `extraction_failed` flag row has no `resolução:` line, so it must not
    pin the note open once every resolvable item is done."""
    from sqlalchemy import select
    vault = await _run(monkeypatch)
    tp = _triagem_path(vault)
    sha = None
    async with get_db_session() as s:
        c = (await s.execute(select(EntityCandidate))).scalars().first()
        sha = c.transcript_sha
        s.add(EntityCandidate(
            transcript_sha=sha, meeting_note_path=c.meeting_note_path, status="pending",
            kind="extraction_failed", surface_form="x", normalized_form="extraction_failed",
        ))
        await s.commit()

    text = tp.read_text(encoding="utf-8")
    text = _set_resolucao(text, "Rafael Moreira", "IGNORAR")
    text = _set_resolucao(text, "MarIa Lyma", "IGNORAR")
    text = _set_resolucao(text, "Seguir com fornecedor A", "IGNORAR")
    tp.write_text(text, encoding="utf-8")

    await apply_pending_triagem(vault)
    assert not tp.exists()
    assert next((vault.vault_path / "08_Archive" / "Triagem").glob("*.md")).exists()
    async with get_db_session() as s:
        ef = (await s.execute(select(EntityCandidate).where(
            EntityCandidate.kind == "extraction_failed"))).scalar_one()
    assert ef.status == "pending"  # untouched in the DB, just not blocking the note


async def test_parseback_is_idempotent_on_rescan(tmp_path, monkeypatch):
    vault = await _run(monkeypatch)
    tp = _triagem_path(vault)
    tp.write_text(_set_resolucao(tp.read_text(encoding="utf-8"), "Rafael Moreira", "NOVO"), encoding="utf-8")
    assert await apply_pending_triagem(vault) == 1
    # only 1 of 3 resolved -> note stays in the inbox, status: pending
    resolved_text = tp.read_text(encoding="utf-8")
    assert "status: pending" in resolved_text and "✅ confirmed_new" in resolved_text
    # second pass: the acted line now reads ✅... -> not re-parsed, the rest still on
    # stubs -> no resolutions -> byte-identical, nothing re-applied
    assert await apply_pending_triagem(vault) == 0
    assert tp.read_text(encoding="utf-8") == resolved_text


async def test_parseback_link_target_not_found_stays_pending(tmp_path, monkeypatch):
    vault = await _run(monkeypatch)
    tp = _triagem_path(vault)
    tp.write_text(_set_resolucao(tp.read_text(encoding="utf-8"), "MarIa Lyma", "= Pessoa Inexistente"), encoding="utf-8")
    assert await apply_pending_triagem(vault) == 0
    cands = {(c.kind, c.normalized_form): c for c in await _cands()}
    assert cands[("person", "maria lyma")].status == "pending"
    assert "não encontrado" in tp.read_text(encoding="utf-8")


async def test_parseback_redate_renames_meeting_note_and_repoints_rows(tmp_path, monkeypatch):
    # filename carries no date -> date_source "mtime" -> Triagem renders a date block
    vault = await _run(monkeypatch, name="kickoff sem data.txt")
    mp_old = next((vault.vault_path / "04_Meetings").glob("*.md"))
    tp = _triagem_path(vault)
    text = tp.read_text(encoding="utf-8")
    assert "## 📅 Data da reunião" in text
    old_date = mp_old.stem.split(" - ")[0]

    tp.write_text(_set_resolucao(text, old_date, "2026-07-01"), encoding="utf-8")
    assert await apply_pending_triagem(vault) == 1

    assert not mp_old.exists()
    mp_new = vault.vault_path / "04_Meetings" / f"2026-07-01 - {mp_old.stem.split(' - ', 1)[1]}.md"
    assert mp_new.exists()
    assert "date: 2026-07-01" in mp_new.read_text(encoding="utf-8")

    # every EntityCandidate row for this transcript now points at the new note path
    cands = await _cands()
    assert cands and all(c.meeting_note_path == str(mp_new) for c in cands)
    md_row = next(c for c in cands if c.kind == "meeting_date")
    assert md_row.status == "linked"

    # the Triagem note moved alongside its meeting note; entity candidates are still
    # pending so it stays in the inbox as status: pending
    new_tp = vault.vault_path / "07_Inbox_Agent" / "Triagem" / f"{mp_new.stem}.md"
    assert new_tp.exists() and not tp.exists()
    assert "status: pending" in new_tp.read_text(encoding="utf-8")


def test_read_resolutions_confirmar_on_date_line_is_redate_to_same_date():
    text = (
        "---\nstatus: pending\n---\n<!-- triagem:abcdef012345 -->\n"
        "## 📅 Data da reunião\n"
        "- [ ] **2026-09-05** (derivada da data do arquivo)\n"
        "      resolução: confirmar\n"
    )
    _sha, res = read_triagem_resolutions(text)
    assert res == [{"surface": "2026-09-05", "action": "redate", "target": "2026-09-05"}]


async def test_parseback_confirmar_resolves_meeting_date_without_renaming(tmp_path, monkeypatch):
    # filename carries no date -> date_source "mtime" -> Triagem renders the date block
    vault = await _run(monkeypatch, name="kickoff sem data.txt")
    mp_old = next((vault.vault_path / "04_Meetings").glob("*.md"))
    old_date = mp_old.stem.split(" - ")[0]
    tp = _triagem_path(vault)

    tp.write_text(_set_resolucao(tp.read_text(encoding="utf-8"), old_date, "confirmar"),
                  encoding="utf-8")
    assert await apply_pending_triagem(vault) == 1

    # meeting note untouched, no rename
    assert mp_old.exists()
    md_row = next(c for c in await _cands() if c.kind == "meeting_date")
    assert md_row.status == "linked"
    assert md_row.resolved_to == str(mp_old)
    # the note stays in the inbox (other candidates still pending) and shows the confirm
    assert tp.exists()
    assert "✅ data confirmada → " + old_date in tp.read_text(encoding="utf-8")


async def test_apply_selfheals_legacy_note_missing_date_block(tmp_path, monkeypatch):
    vault = await _run(monkeypatch, name="kickoff sem data.txt")
    tp = _triagem_path(vault)
    old_date = next((vault.vault_path / "04_Meetings").glob("*.md")).stem.split(" - ")[0]

    # simulate a note written before the ## 📅 block existed: strip the block out
    lines = tp.read_text(encoding="utf-8").splitlines()
    start = next(i for i, ln in enumerate(lines) if ln == "## 📅 Data da reunião")
    end = next(i for i in range(start + 1, len(lines)) if lines[i] == "")
    tp.write_text("\n".join(lines[:start] + lines[end + 1:]) + "\n", encoding="utf-8")
    assert "## 📅 Data da reunião" not in tp.read_text(encoding="utf-8")

    # a scan with no edited resolutions splices the block back in
    assert await apply_pending_triagem(vault) == 0
    healed = tp.read_text(encoding="utf-8")
    assert "## 📅 Data da reunião" in healed
    assert f"- [ ] **{old_date}** (derivada da data do arquivo)" in healed
    assert tp.exists()  # still pending, still in the inbox


async def test_parseback_decision_new_creates_record(tmp_path, monkeypatch):
    vault = await _run(monkeypatch)
    tp = _triagem_path(vault)
    tp.write_text(_set_resolucao(tp.read_text(encoding="utf-8"), "Seguir com fornecedor A", "REGISTRAR"), encoding="utf-8")
    await apply_pending_triagem(vault)
    from sqlalchemy import select
    async with get_db_session() as s:
        decs = (await s.execute(select(DecisionRecord))).scalars().all()
    assert len(decs) == 1 and decs[0].title == "Seguir com fornecedor A"


async def test_parseback_decision_link_to_existing_decision(tmp_path, monkeypatch):
    """2026-09-17 bug: linking a decision candidate to an existing decision via
    `=<título exato>` always reported "não encontrado", even for an exact title match,
    because `_exact_decision_match` didn't exist and the link branch had no `decision`
    case — it always fell through to `None`. Decision notes are filed as `{id} - {title}
    .md`, so matching must go through the `title:` frontmatter, not the filename stem."""
    vault = await _run(monkeypatch)
    existing_path = vault.create_decision_note(
        decision_id="DEC-2026-00001", title="Transição para atuação independente",
        file_stem="DEC-2026-00001 - Transição para atuação independente",
    )
    tp = _triagem_path(vault)
    tp.write_text(
        _set_resolucao(
            tp.read_text(encoding="utf-8"), "Seguir com fornecedor A",
            "= Transição para atuação independente",
        ),
        encoding="utf-8",
    )
    n = await apply_pending_triagem(vault)
    assert n == 1
    cands = {(c.kind, c.normalized_form): c for c in await _cands()}
    cand = cands[("decision", "seguir com fornecedor a")]
    assert cand.status == "linked"
    assert cand.resolved_to == str(existing_path)


async def test_parseback_resolution_propagates_across_transcripts(tmp_path, monkeypatch):
    from sqlalchemy import select
    vault = await _run(monkeypatch, name="reuniao-um 2026-09-05.txt", body_marker="reuniao um")
    await _run(monkeypatch, name="reuniao-dois 2026-09-06.txt", rich=_rich(title="Reunião Dois"),
               body_marker="reuniao dois distinta")

    # both transcripts have a pending "Rafael Moreira" person candidate
    async with get_db_session() as s:
        rows = (await s.execute(select(EntityCandidate).where(
            EntityCandidate.kind == "person", EntityCandidate.normalized_form == "rafael moreira"))).scalars().all()
    assert len(rows) == 2 and all(r.status == "pending" for r in rows)

    # resolve it once, in the first Triagem note
    tp = sorted((vault.vault_path / "07_Inbox_Agent" / "Triagem").glob("*.md"))[0]
    tp.write_text(_set_resolucao(tp.read_text(encoding="utf-8"), "Rafael Moreira", "NOVO"), encoding="utf-8")
    await apply_pending_triagem(vault)

    async with get_db_session() as s:
        rows = (await s.execute(select(EntityCandidate).where(
            EntityCandidate.kind == "person", EntityCandidate.normalized_form == "rafael moreira"))).scalars().all()
    # one confirmed_new (the one acted on) + one linked (propagated to the other transcript)
    statuses = sorted(r.status for r in rows)
    assert statuses == ["confirmed_new", "linked"]
    linked = next(r for r in rows if r.status == "linked")
    assert linked.resolved_to and linked.resolved_to.endswith("Rafael Moreira.md")


async def test_parseback_lone_surname_propagates_via_suggestion(tmp_path, monkeypatch):
    """Phase D headline: a lone surname 'Moreira' would org-namespace apart under
    identity_key, but a strong suggestion to the same vault note re-unites the
    group so resolving one applies to the other transcript."""
    from sqlalchemy import select
    rich = _rich(people=[{"name": "Moreira", "role": "Diretor", "org": "Meridian", "mentions": "conduziu"}])
    vault = await _run(monkeypatch, name="s-um 2026-09-05.txt", rich=rich, body_marker="reuniao um")
    await _run(monkeypatch, name="s-dois 2026-09-06.txt",
               rich=_rich(title="R2", people=[{"name": "Moreira", "role": "Diretor", "org": "Aliança", "mentions": "conduziu"}]),
               body_marker="reuniao dois distinta")

    vault.create_person_note(name="Rafael Moreira", role="", organization="")
    target = str(vault.vault_path / "05_People" / "Rafael Moreira.md")
    async with get_db_session() as s:
        rows = (await s.execute(select(EntityCandidate).where(
            EntityCandidate.kind == "person", EntityCandidate.normalized_form == "moreira"))).scalars().all()
        assert len(rows) == 2 and all(r.status == "pending" for r in rows)
        for r in rows:
            r.suggested_matches_json = json.dumps([{"name": "Rafael Moreira", "path": target, "score": 0.9}])
        await s.commit()

    tp = sorted((vault.vault_path / "07_Inbox_Agent" / "Triagem").glob("*.md"))[0]
    tp.write_text(_set_resolucao(tp.read_text(encoding="utf-8"), "Moreira", "= Rafael Moreira"), encoding="utf-8")
    await apply_pending_triagem(vault)

    async with get_db_session() as s:
        rows = (await s.execute(select(EntityCandidate).where(
            EntityCandidate.kind == "person", EntityCandidate.normalized_form == "moreira"))).scalars().all()
    assert sorted(r.status for r in rows) == ["linked", "linked"]
    assert all((r.resolved_to or "").endswith("Rafael Moreira.md") for r in rows)


async def test_parseback_legacy_bulk_block_still_parses(tmp_path, monkeypatch):
    """Fresh notes no longer carry the ``## ⚡ Ações em massa`` block, but notes written
    before that change (and the migrate script's output) still do — a ``SIM`` on one of
    its lines must keep working."""
    from sqlalchemy import select
    from rysos.vault.triagem import _bulk_block_lines
    vault = await _run(monkeypatch)
    vault.create_person_note(name="Maria Lima", role="", organization="")
    target = str(vault.vault_path / "05_People" / "Maria Lima.md")
    async with get_db_session() as s:
        row = (await s.execute(select(EntityCandidate).where(
            EntityCandidate.kind == "person", EntityCandidate.normalized_form == "maria lyma"))).scalar_one()
        row.suggested_matches_json = json.dumps([{"name": "Maria Lima", "path": target, "score": 0.9}])
        await s.commit()

    tp = _triagem_path(vault)
    text = tp.read_text(encoding="utf-8")
    assert "## ⚡ Ações em massa" not in text  # not rendered any more
    text = text.rstrip() + "\n\n" + "\n".join(_bulk_block_lines())  # simulate a legacy note
    tp.write_text(_set_resolucao(text, "aceitar sugestões fortes", "SIM"), encoding="utf-8")

    n = await apply_pending_triagem(vault)
    assert n >= 1
    cands = {(c.kind, c.normalized_form): c for c in await _cands()}
    assert cands[("person", "maria lyma")].status == "linked"
    assert cands[("person", "maria lyma")].resolved_to == target
    # Rafael Moreira + the decision are still pending -> note stays in the inbox
    after = tp.read_text(encoding="utf-8")
    assert "status: pending" in after and "item(ns) processado(s)" in after
