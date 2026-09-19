"""Who the assistant works for: name, role and e-mail accounts, all read from settings.

Nothing about the owner is hardcoded. Prompts carry ``@@TOKEN@@`` placeholders that
``personalize`` fills at import time; account type ("pessoal" / "corporativo") comes from the
e-mail domain, so a Gmail address is personal and any other domain is corporate.
"""

from __future__ import annotations

from rysos.config import settings

_TOKENS = ("@@USER_NAME@@", "@@USER_FIRST@@", "@@USER_ROLE@@", "@@PERSONAL_EMAIL@@", "@@CORP_EMAIL@@")


def domain_of(email: str) -> str:
    return email.rsplit("@", 1)[-1].strip().lower()


def account_type_for(email: str) -> str:
    """'pessoal' for consumer mail domains (see PERSONAL_EMAIL_DOMAINS), else 'corporativo'."""
    return "pessoal" if domain_of(email) in {d.lower() for d in settings.PERSONAL_EMAIL_DOMAINS} else "corporativo"


def user_name() -> str:
    return settings.USER_NAME.strip() or "Usuário"


def user_first_name() -> str:
    return user_name().split()[0]


def account_email(kind: str) -> str:
    """First configured USER_EMAILS entry of the given type ('pessoal'/'corporativo'), or ''."""
    for e in settings.USER_EMAILS:
        if account_type_for(e) == kind:
            return e
    return ""


def personalize(text: str) -> str:
    """Replace the ``@@...@@`` placeholders in a prompt with the configured identity."""
    values = {
        "@@USER_NAME@@": user_name(),
        "@@USER_FIRST@@": user_first_name(),
        "@@USER_ROLE@@": settings.USER_ROLE.strip() or "executivo",
        "@@PERSONAL_EMAIL@@": account_email("pessoal") or "a conta pessoal",
        "@@CORP_EMAIL@@": account_email("corporativo") or "a conta corporativa",
    }
    for token in _TOKENS:
        text = text.replace(token, values[token])
    return text
