"""Gmail API Connector with multi-account support and anti-noise filtering."""

import base64
import re
from datetime import datetime, timezone
from typing import Any, Optional, List
from pydantic import BaseModel

from rysos.config import settings
from rysos.identity import account_type_for
from rysos.auth.google_auth import GoogleAuthManager, google_auth, AccountSession


class EmailData(BaseModel):
    id: str
    thread_id: str
    subject: str
    sender: str
    recipient: str = ""
    account: str = ""  # e.g. "me@company.com" or "me@gmail.com"
    account_type: str = "corporativo"  # 'corporativo' or 'pessoal'
    date: datetime
    snippet: str
    body_text: str
    labels: list[str] = []
    is_unread: bool = False
    is_starred: bool = False


class GmailConnector:
    """Connector to interact with Gmail API across multiple Google accounts."""

    def __init__(self, auth_manager: Optional[GoogleAuthManager] = None):
        self.auth_manager = auth_manager or google_auth

    def fetch_recent_emails(
        self,
        query: Optional[str] = None,
        max_results: Optional[int] = None,
        max_results_per_account: int = 15,
    ) -> List[EmailData]:
        """Fetches recent emails matching query across all authenticated Google accounts."""
        limit = max_results if max_results is not None else max_results_per_account
        accounts = self.auth_manager.get_all_accounts()
        if not accounts:
            return []

        search_query = query or settings.GMAIL_QUERY_FILTER
        all_emails: List[EmailData] = []

        for acc in accounts:
            account_type = account_type_for(acc.email)
            try:
                service = acc.gmail
                queries = [search_query]
                if account_type == "pessoal":
                    queries.append('(boleto OR fatura OR vencimento OR "nfs-e" OR conta OR condomínio OR condominio) newer_than:7d')

                seen_msg_ids = set()
                for q in queries:
                    response = service.users().messages().list(
                        userId="me",
                        q=q,
                        maxResults=limit,
                    ).execute()
                    messages = response.get("messages", [])

                    for msg_meta in messages:
                        mid = msg_meta["id"]
                        if mid in seen_msg_ids:
                            continue
                        seen_msg_ids.add(mid)

                        msg = service.users().messages().get(
                            userId="me",
                            id=mid,
                            format="full",
                        ).execute()
                        email_data = self._parse_message(msg, account_email=acc.email, account_type=account_type)
                        if email_data and not self.is_noise_email(email_data):
                            all_emails.append(email_data)
            except Exception as e:
                # Log account error and continue with other accounts
                continue

        return all_emails

    def is_noise_email(self, email: EmailData) -> bool:
        """Determines if an email is automated noise, receipts, or non-actionable bulk."""
        if email.is_starred:
            return False

        subject_lower = email.subject.lower()
        snippet_lower = email.snippet.lower()
        text_full = f"{subject_lower} {snippet_lower}"

        # EXCEÇÃO DE OURO: Boletos e faturas em conta pessoal NUNCA são ruído
        is_personal = getattr(email, "account_type", "") == "pessoal"
        bill_keywords = ["boleto", "fatura", "vencimento", "nfs-e", "conta de luz", "conta de agua", "conta de água", "condomínio", "condominio", "iptu", "ipva", "gasnorte", "luzsul", "bancodigital"]
        if is_personal and any(kw in text_full for kw in bill_keywords):
            return False

        sender_lower = email.sender.lower()

        noise_senders = [
            "noreply",
            "no-reply",
            "notifications@",
            "notification@",
            "billing@",
            "alerts@",
            "alert@",
            "newsletter@",
            "news@",
            "marketing@",
            "automated@",
            "mailer-daemon",
            "bounce@",
            "donotreply",
            "updates@",
            "digest@",
            "promo@",
        ]
        if any(ns in sender_lower for ns in noise_senders):
            return True

        noise_subjects = [
            "código de segurança",
            "código de verificação",
            "sua fatura",
            "recibo de pagamento",
            "comprovante",
            "nota fiscal",
            "security alert",
            "newsletter",
            "resumo semanal",
            "digest",
            "confirmação de pedido",
            "password reset",
            "redefinição de senha",
            "extrato",
        ]
        if any(subj in subject_lower for subj in noise_subjects):
            return True

        return False

    def _parse_message(self, msg: dict[str, Any], account_email: str, account_type: str) -> Optional[EmailData]:
        """Extracts structured email data from Gmail API response."""
        msg_id = msg.get("id", "")
        thread_id = msg.get("threadId", "")
        snippet = msg.get("snippet", "")
        labels = msg.get("labelIds", [])

        payload = msg.get("payload", {})
        headers = {h["name"].lower(): h["value"] for h in payload.get("headers", [])}

        subject = headers.get("subject", "(Sem assunto)")
        sender = headers.get("from", "Desconhecido")
        recipient = headers.get("to", "")
        date_raw = headers.get("date", "")

        date_dt = datetime.now(timezone.utc)
        try:
            import email.utils
            parsed_tuple = email.utils.parsedate_to_datetime(date_raw)
            if parsed_tuple:
                date_dt = parsed_tuple
        except Exception:
            pass

        body_text = self._extract_body(payload) or snippet

        return EmailData(
            id=f"{account_email}_{msg_id}",
            thread_id=thread_id,
            subject=subject,
            sender=sender,
            recipient=recipient,
            account=account_email,
            account_type=account_type,
            date=date_dt,
            snippet=snippet,
            body_text=body_text[:4000],
            labels=labels,
            is_unread="UNREAD" in labels,
            is_starred="STARRED" in labels,
        )

    def _extract_body(self, payload: dict[str, Any]) -> str:
        """Recursively extracts plain text body (or cleaned HTML) from MIME payload."""
        # 1. Try finding text/plain first
        plain_text = self._find_mime_data(payload, "text/plain")
        if plain_text:
            return plain_text

        # 2. Fallback to text/html and strip tags
        html_raw = self._find_mime_data(payload, "text/html")
        if html_raw:
            html_no_style = re.sub(r"<style.*?</style>", "", html_raw, flags=re.DOTALL | re.IGNORECASE)
            html_no_script = re.sub(r"<script.*?</script>", "", html_no_style, flags=re.DOTALL | re.IGNORECASE)
            clean_text = re.sub(r"<[^>]+>", " ", html_no_script)
            clean_text = re.sub(r"\s+", " ", clean_text).strip()
            return clean_text

        return ""

    def _find_mime_data(self, payload: dict[str, Any], target_mime: str) -> str:
        """Recursively searches for target MIME part and decodes it."""
        mime_type = payload.get("mimeType", "")
        body = payload.get("body", {})
        parts = payload.get("parts", [])

        if mime_type.lower() == target_mime.lower() and "data" in body:
            try:
                return base64.urlsafe_b64decode(body["data"]).decode("utf-8", errors="replace")
            except Exception:
                return ""

        for part in parts:
            found = self._find_mime_data(part, target_mime)
            if found:
                return found

        return ""
