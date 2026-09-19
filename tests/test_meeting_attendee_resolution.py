"""Tests for VaultManager._resolve_attendee_display_names — maps a bare-email attendee
to the matching 05_People note's real name before a meeting note's wikilink is rendered.
See the gil.prado@gmail.com incident (frontmatter email match) and the 2026-09-18
owner@acme-corp.example bug (owner's own multi-email identity) this guards against.
"""

from pathlib import Path

from rysos.config import settings
from rysos.vault.manager import VaultManager


def _vault(tmp_path: Path) -> VaultManager:
    vault = VaultManager(vault_path=tmp_path / "myvault")
    vault.initialize_vault_structure()
    return vault


def test_resolves_bare_email_to_matching_person_note(tmp_path: Path):
    vault = _vault(tmp_path)
    vault.create_person_note(name="Gil Prado", email="gil.prado@gmail.com")

    resolved = vault._resolve_attendee_display_names(["gil.prado@gmail.com", "Ana Torres"])
    assert resolved == ["Gil Prado", "Ana Torres"]


def test_leaves_unmatched_email_untouched(tmp_path: Path):
    vault = _vault(tmp_path)
    resolved = vault._resolve_attendee_display_names(["stranger@example.com"])
    assert resolved == ["stranger@example.com"]


def test_owner_own_email_resolves_via_user_emails_even_without_frontmatter_match(tmp_path: Path, monkeypatch):
    """2026-09-18 bug: Rafael Moreira.md's `email:` frontmatter is only one of his several
    real addresses — a calendar attendee listing him under a DIFFERENT one of his own
    emails (settings.USER_EMAILS) must still resolve to his note, not phantom-link."""
    monkeypatch.setattr(settings, "USER_EMAILS", ["owner@acme-corp.example", "owner.pessoal@gmail.com"])
    vault = _vault(tmp_path)
    vault.create_person_note(name="Rafael Moreira", email="owner.pessoal@gmail.com")

    resolved = vault._resolve_attendee_display_names(["owner@acme-corp.example", "Bruno Almeida Neto"])
    assert resolved == ["Rafael Moreira", "Bruno Almeida Neto"]

    # The frontmatter-matching email still resolves too, same code path either way.
    resolved2 = vault._resolve_attendee_display_names(["owner.pessoal@gmail.com"])
    assert resolved2 == ["Rafael Moreira"]


def test_create_meeting_note_no_longer_phantom_links_the_owner(tmp_path: Path, monkeypatch):
    from datetime import datetime

    monkeypatch.setattr(settings, "USER_EMAILS", ["owner@acme-corp.example", "owner.pessoal@gmail.com"])
    vault = _vault(tmp_path)
    vault.create_person_note(name="Rafael Moreira", email="owner.pessoal@gmail.com")
    vault.create_person_note(name="Bruno Almeida Neto")

    path = vault.create_meeting_note(
        summary="Bruno <> Moreira",
        start_time=datetime(2026, 9, 18, 15, 0),
        end_time=datetime(2026, 9, 18, 16, 0),
        attendees=["Bruno Almeida Neto", "owner@acme-corp.example"],
    )

    content = vault.read_file(path)
    assert "[[05_People/Rafael Moreira|Rafael Moreira]]" in content
    assert "owner@acme-corp.example" not in content
