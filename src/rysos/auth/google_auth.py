"""Google Workspace OAuth 2.0 multi-account authentication manager."""

import re
import sys
from pathlib import Path
from typing import Optional, Any, Tuple, List, Dict
from urllib.parse import urlparse, parse_qs
from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import Flow, InstalledAppFlow
from googleapiclient.discovery import build, Resource

from rysos.config import settings


class AccountSession:
    """Represents an authenticated Google account session."""

    def __init__(self, email: str, token_path: Path, creds: Credentials):
        self.email = email
        self.token_path = token_path
        self.creds = creds
        self._gmail_service: Optional[Resource] = None
        self._calendar_service: Optional[Resource] = None

    @property
    def gmail(self) -> Resource:
        if self._gmail_service is None:
            self._gmail_service = build("gmail", "v1", credentials=self.creds)
        return self._gmail_service

    @property
    def calendar(self) -> Resource:
        if self._calendar_service is None:
            self._calendar_service = build("calendar", "v3", credentials=self.creds)
        return self._calendar_service


class GoogleAuthManager:
    """Manages multi-account Google OAuth 2.0 credentials and services."""

    def __init__(
        self,
        credentials_path: Optional[Path] = None,
        tokens_dir: Optional[Path] = None,
        scopes: Optional[list[str]] = None,
    ):
        self.credentials_path = credentials_path or settings.GOOGLE_CREDENTIALS_PATH
        self.tokens_dir = tokens_dir or (settings.BASE_DIR / "tokens")
        self.legacy_token_path = settings.GOOGLE_TOKEN_PATH
        self.scopes = scopes or settings.GOOGLE_SCOPES
        self._cached_flow: Optional[Flow] = None

        # Ensure tokens directory exists
        self.tokens_dir.mkdir(parents=True, exist_ok=True)
        self._migrate_legacy_token()

    def _migrate_legacy_token(self) -> None:
        """Migrates legacy single token.json into the tokens/ directory."""
        if self.legacy_token_path.exists():
            try:
                creds = Credentials.from_authorized_user_file(str(self.legacy_token_path), self.scopes)
                if creds and creds.valid:
                    # Get profile email
                    service = build("gmail", "v1", credentials=creds)
                    profile = service.users().getProfile(userId="me").execute()
                    email = profile.get("emailAddress")
                    if email:
                        safe_email = re.sub(r'[^a-zA-Z0-9]', '_', email)
                        target = self.tokens_dir / f"token_{safe_email}.json"
                        target.write_text(creds.to_json(), encoding="utf-8")
            except Exception:
                pass

    def get_all_accounts(self) -> List[AccountSession]:
        """Loads and refreshes credentials for all authenticated accounts."""
        accounts: List[AccountSession] = []
        token_files = list(self.tokens_dir.glob("token_*.json"))

        # Fallback to single token if tokens_dir has nothing yet
        if not token_files and self.legacy_token_path.exists():
            token_files = [self.legacy_token_path]

        for tf in token_files:
            try:
                creds = Credentials.from_authorized_user_file(str(tf), self.scopes)
                if creds and creds.expired and creds.refresh_token:
                    try:
                        creds.refresh(Request())
                        tf.write_text(creds.to_json(), encoding="utf-8")
                    except Exception:
                        continue

                if creds and creds.valid:
                    # Identify email address
                    try:
                        service = build("gmail", "v1", credentials=creds)
                        profile = service.users().getProfile(userId="me").execute()
                        email = profile.get("emailAddress", tf.stem.replace("token_", ""))
                    except Exception:
                        email = tf.stem.replace("token_", "")

                    accounts.append(AccountSession(email=email, token_path=tf, creds=creds))
            except Exception:
                continue

        return accounts

    def is_authenticated(self) -> bool:
        """Checks if at least one valid Google account is authenticated."""
        return len(self.get_all_accounts()) > 0

    def add_new_account_interactive(self, port: int = 0, open_browser: bool = True) -> AccountSession:
        """Launches OAuth flow to authenticate an additional Google account."""
        if not self.credentials_path.exists():
            raise FileNotFoundError(
                f"Arquivo de credenciais não encontrado em: {self.credentials_path}."
            )

        flow = InstalledAppFlow.from_client_secrets_file(
            str(self.credentials_path),
            scopes=self.scopes,
        )
        creds = flow.run_local_server(
            port=port,
            open_browser=open_browser,
            prompt="select_account consent",
            access_type="offline",
        )

        service = build("gmail", "v1", credentials=creds)
        profile = service.users().getProfile(userId="me").execute()
        email = profile.get("emailAddress", "unknown")

        safe_email = re.sub(r'[^a-zA-Z0-9]', '_', email)
        target_path = self.tokens_dir / f"token_{safe_email}.json"
        target_path.write_text(creds.to_json(), encoding="utf-8")

        # Also update legacy token for backward compatibility
        self.legacy_token_path.write_text(creds.to_json(), encoding="utf-8")

        return AccountSession(email=email, token_path=target_path, creds=creds)

    def complete_auth_with_code(self, input_data: str, redirect_uri: str = "http://localhost") -> AccountSession:
        """Exchanges an authorization code or redirect URL for tokens."""
        if not self.credentials_path.exists():
            raise FileNotFoundError(f"Arquivo de credenciais não encontrado em: {self.credentials_path}.")

        input_data = input_data.strip()
        code = input_data
        if "code=" in input_data:
            parsed = urlparse(input_data)
            query_params = parse_qs(parsed.query)
            if "code" in query_params:
                code = query_params["code"][0]
            else:
                m = re.search(r"code=([^&]+)", input_data)
                if m:
                    code = m.group(1)

        flow = Flow.from_client_secrets_file(
            str(self.credentials_path),
            scopes=self.scopes,
            redirect_uri=redirect_uri,
        )
        flow.fetch_token(code=code)
        creds = flow.credentials

        service = build("gmail", "v1", credentials=creds)
        profile = service.users().getProfile(userId="me").execute()
        email = profile.get("emailAddress", "unknown")

        safe_email = re.sub(r'[^a-zA-Z0-9]', '_', email)
        target_path = self.tokens_dir / f"token_{safe_email}.json"
        target_path.write_text(creds.to_json(), encoding="utf-8")
        self.legacy_token_path.write_text(creds.to_json(), encoding="utf-8")

        return AccountSession(email=email, token_path=target_path, creds=creds)


google_auth = GoogleAuthManager()
