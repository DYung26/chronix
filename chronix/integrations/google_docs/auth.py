"""Authentication strategies for Google Docs API."""

from abc import ABC, abstractmethod
from pathlib import Path
from typing import Optional
import json
import webbrowser

from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google.oauth2 import service_account
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build


SCOPES = [
    "https://www.googleapis.com/auth/documents",
    "https://www.googleapis.com/auth/calendar.readonly",
    "https://www.googleapis.com/auth/calendar.events",
]


class AuthStrategy(ABC):
    """Abstract base class for Google Docs authentication strategies."""
    
    @abstractmethod
    def get_credentials(self, interactive: bool = True):
        """Return usable credentials, optionally allowing interactive auth."""
        pass

    def get_service(self, interactive: bool = True):
        """Return an authenticated Google Docs API service."""
        raise NotImplementedError


class OAuthAuth(AuthStrategy):
    """OAuth 2.0 authentication for installed/CLI applications."""

    def __init__(
        self,
        credentials_path: Path,
        token_path: Path,
        scopes: list[str] = SCOPES
    ):
        self.credentials_path = credentials_path
        self.token_path = token_path
        self.scopes = scopes
        self._credentials = None

    def get_credentials(self, interactive: bool = True):
        """Return usable OAuth credentials, optionally allowing interactive auth.

        Existing credentials are always loaded first. Expired credentials with a
        refresh token are refreshed before interactive authentication is considered.
        MCP callers pass ``interactive=False`` so a missing/unrefreshable credential
        never attempts to launch a browser.
        """
        creds = self._credentials

        if creds is None and self.token_path.exists():
            creds = Credentials.from_authorized_user_file(str(self.token_path), self.scopes)
            self._credentials = creds

        if not creds or not creds.valid:
            if creds and creds.expired and creds.refresh_token:
                try:
                    creds.refresh(Request())
                    self._credentials = creds
                    # Save the refreshed token
                    self.token_path.parent.mkdir(parents=True, exist_ok=True)
                    with open(self.token_path, "w") as token_file:
                        token_file.write(creds.to_json())
                except Exception as refresh_error:
                    print(f"\n[!] Token refresh failed: {refresh_error}")
                    print("[*] Re-authentication is required.\n")
                    creds = None
            
            if not creds or not creds.valid:
                if not interactive:
                    raise RuntimeError(
                        "Google authentication is unavailable without interactive authorization. "
                        "The stored credentials are missing, expired without a usable refresh token, "
                        "or could not be refreshed. Run Chronix from the CLI to re-authenticate."
                    )

                # Need full OAuth re-authentication
                if not self.credentials_path.exists():
                    raise FileNotFoundError(
                        f"OAuth credentials not found at {self.credentials_path}. "
                        "Download OAuth client credentials from Google Cloud Console."
                    )
 
                flow = InstalledAppFlow.from_client_secrets_file(
                    str(self.credentials_path), self.scopes
                )

                auth_url, _ = flow.authorization_url(prompt="consent")

                print(f"\n[*] Please visit this URL to authorize Chronix:\n")
                print(f"    {auth_url}\n")
                print(f"[*] Opening browser for Google authentication...\n")

                try:
                    webbrowser.open(auth_url)
                except Exception:
                    pass

                creds = flow.run_local_server(port=0)
                self._credentials = creds

                self.token_path.parent.mkdir(parents=True, exist_ok=True)
                with open(self.token_path, "w") as token_file:
                    token_file.write(creds.to_json())

        return creds

    def get_service(self, interactive: bool = True):
        """Return an authenticated Google Docs API service."""
        return build("docs", "v1", credentials=self.get_credentials(interactive=interactive))


class ServiceAccountAuth(AuthStrategy):
    """Service account authentication for Google Docs API."""

    def __init__(self, credentials_path: Path, scopes: list[str] = SCOPES):
        self.credentials_path = credentials_path
        self.scopes = scopes
        self._credentials = None

    def get_credentials(self, interactive: bool = True):
        """Return service-account credentials."""
        if not self.credentials_path.exists():
            raise FileNotFoundError(
                f"Service account credentials not found at {self.credentials_path}"
            )

        if self._credentials is None:
            self._credentials = service_account.Credentials.from_service_account_file(
                str(self.credentials_path), scopes=self.scopes
            )
        return self._credentials

    def get_service(self, interactive: bool = True):
        """Return an authenticated Google Docs API service."""
        creds = self.get_credentials(interactive=interactive)
        http = AuthorizedHttp(creds, http=httplib2.Http(timeout=30))
        return build("docs", "v1", http=http)


def get_default_auth_strategy() -> AuthStrategy:
    """Returns the default authentication strategy based on available credentials."""
    config_dir = Path.home() / ".config" / "chronix" / "google"

    oauth_creds = config_dir / "credentials.json"
    oauth_token = config_dir / "token.json"
    service_account_creds = config_dir / "service_account.json"

    if service_account_creds.exists():
        return ServiceAccountAuth(service_account_creds)

    if oauth_creds.exists() or oauth_token.exists():
        return OAuthAuth(oauth_creds, oauth_token)

    raise FileNotFoundError(
        f"No Google credentials found. Place one of:\n"
        f"  - OAuth credentials: {oauth_creds}\n"
        f"  - Service account: {service_account_creds}\n"
        f"Get credentials from: https://console.cloud.google.com/apis/credentials"
    )
