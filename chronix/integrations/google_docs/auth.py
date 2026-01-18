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


SCOPES = ["https://www.googleapis.com/auth/documents.readonly"]


class AuthStrategy(ABC):
    """Abstract base class for Google Docs authentication strategies."""
    
    @abstractmethod
    def get_service(self):
        """Returns an authenticated Google Docs API service."""
        pass


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

    def get_service(self):
        """Returns authenticated service using OAuth flow."""
        creds = None

        if self.token_path.exists():
            creds = Credentials.from_authorized_user_file(str(self.token_path), self.scopes)

        if not creds or not creds.valid:
            if creds and creds.expired and creds.refresh_token:
                creds.refresh(Request())
            else:
                if not self.credentials_path.exists():
                    raise FileNotFoundError(
                        f"OAuth credentials not found at {self.credentials_path}. "
                        "Download OAuth client credentials from Google Cloud Console."
                    )
 
                flow = InstalledAppFlow.from_client_secrets_file(
                    str(self.credentials_path), self.scopes
                )

                _auth_url, _ = flow.authorization_url(prompt="consent")

                print(f"\nOpening browser for Google authentication...")
                # print(f"If browser doesn't open, visit this URL:\n{auth_url}\n")

                # try:
                #     webbrowser.open(auth_url)
                # except Exception:
                #     pass

                creds = flow.run_local_server(port=0)

            self.token_path.parent.mkdir(parents=True, exist_ok=True)
            with open(self.token_path, "w") as token_file:
                token_file.write(creds.to_json())

        return build("docs", "v1", credentials=creds)


class ServiceAccountAuth(AuthStrategy):
    """Service account authentication for Google Docs API."""
    
    def __init__(self, credentials_path: Path, scopes: list[str] = SCOPES):
        self.credentials_path = credentials_path
        self.scopes = scopes
    
    def get_service(self):
        """Returns authenticated service using service account."""
        if not self.credentials_path.exists():
            raise FileNotFoundError(
                f"Service account credentials not found at {self.credentials_path}"
            )
        
        creds = service_account.Credentials.from_service_account_file(
            str(self.credentials_path), scopes=self.scopes
        )

        return build("docs", "v1", credentials=creds)


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
