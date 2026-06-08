"""User configuration and settings management."""

from datetime import time, timedelta
from pathlib import Path
from typing import Optional, Literal
from pydantic import BaseModel, Field, field_validator, model_validator
import tomllib
import tomli_w


class TimeBlockConfig(BaseModel):
    """Configuration for a recurring time block (sleep, breaks, meetings)."""
    
    start_time: time
    end_time: time
    kind: Literal["sleep", "break", "meeting", "blocked"]
    label: Optional[str] = None
    days: list[str] = Field(default_factory=lambda: ["monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday"])

    @field_validator("days")
    @classmethod
    def validate_days(cls, v: list[str]) -> list[str]:
        valid_days = {"monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday"}
        normalized = [day.lower() for day in v]
        invalid = set(normalized) - valid_days
        if invalid:
            raise ValueError(f"Invalid days: {invalid}. Must be one of {valid_days}")
        return normalized
    
    @model_validator(mode="after")
    def validate_times(self):
        if self.start_time >= self.end_time:
            raise ValueError("start_time must be before end_time")
        return self


class SchedulingConfig(BaseModel):
    """Configuration for task scheduling behavior."""
    
    work_start_time: time = Field(default=time(9, 0), description="Daily work start time")
    work_end_time: time = Field(default=time(18, 0), description="Daily work end time")
    timezone: str = Field(default="UTC", description="Timezone for scheduling")
    default_task_duration_minutes: int = Field(default=60, ge=1, description="Default task duration if not specified")

    sleep_windows: list[TimeBlockConfig] = Field(default_factory=list, description="Sleep time blocks")
    breaks: list[TimeBlockConfig] = Field(default_factory=list, description="Break time blocks")
    meetings: list[TimeBlockConfig] = Field(default_factory=list, description="Recurring meeting blocks")

    @model_validator(mode="after")
    def validate_work_hours(self):
        if self.work_start_time >= self.work_end_time:
            raise ValueError("work_start_time must be before work_end_time")
        return self

    def get_default_task_duration(self) -> timedelta:
        """Get default task duration as timedelta."""
        return timedelta(minutes=self.default_task_duration_minutes)


class DocumentConfig(BaseModel):
    """Configuration for a single Google Docs document."""

    document_id: str
    alias: Optional[str] = None


class GoogleDocsConfig(BaseModel):
    """Configuration for Google Docs integration."""

    auth_method: Literal["oauth", "service_account"] = Field(default="oauth")
    credentials_path: Optional[Path] = Field(default=None, description="Path to OAuth credentials or service account key")
    token_path: Optional[Path] = Field(default=None, description="Path to OAuth token cache")

    documents: list[DocumentConfig] = Field(default_factory=list, description="List of Google Docs documents to sync")

    @field_validator("credentials_path", "token_path")
    @classmethod
    def expand_path(cls, v: Optional[Path]) -> Optional[Path]:
        if v is None:
            return None
        return Path(v).expanduser().resolve()

    @model_validator(mode="after")
    def set_default_paths(self):
        """Set default paths if not specified."""
        if self.credentials_path is None:
            self.credentials_path = Path.home() / ".chronix" / "credentials.json"
        if self.token_path is None:
            self.token_path = Path.home() / ".chronix" / "token.json"
        return self

    @model_validator(mode="after")
    def validate_alias_uniqueness(self):
        aliases = [doc.alias for doc in self.documents if doc.alias is not None]
        seen = set()
        for alias in aliases:
            if alias in seen:
                raise ValueError(f"Duplicate alias '{alias}' found in document configuration")
            seen.add(alias)
        return self

    @property
    def document_ids(self) -> list[str]:
        """All configured document IDs."""
        return [doc.document_id for doc in self.documents]

    def resolve(self, token: str) -> Optional[str]:
        """
        Resolve a token (alias or document_id) to its canonical document_id.

        Returns the document_id if found, None if not configured.
        """
        for doc in self.documents:
            if doc.alias == token or doc.document_id == token:
                return doc.document_id
        return None

    def get_alias(self, document_id: str) -> Optional[str]:
        """Return the alias for a document_id, or None if no alias is set."""
        for doc in self.documents:
            if doc.document_id == document_id:
                return doc.alias
        return None

    def format_document_label(self, document_id: str) -> str:
        """Format a document for display: 'alias (document_id)' or just 'document_id'."""
        alias = self.get_alias(document_id)
        if alias:
            return f"{alias} ({document_id})"
        return document_id


class ChronixConfig(BaseModel):
    """Root configuration for chronix."""

    scheduling: SchedulingConfig = Field(default_factory=SchedulingConfig)
    google_docs: GoogleDocsConfig = Field(default_factory=GoogleDocsConfig)

    @classmethod
    def from_toml(cls, path: Path) -> "ChronixConfig":
        """Load configuration from TOML file."""
        if not path.exists():
            raise FileNotFoundError(f"Configuration file not found: {path}")

        with open(path, "rb") as f:
            data = tomllib.load(f)

        return cls.model_validate(data)

    def to_toml(self, path: Path) -> None:
        """Save configuration to TOML file."""
        path.parent.mkdir(parents=True, exist_ok=True)

        data = self.model_dump(mode="json")

        if "google_docs" in data:
            if data["google_docs"].get("credentials_path"):
                data["google_docs"]["credentials_path"] = str(data["google_docs"]["credentials_path"])
            if data["google_docs"].get("token_path"):
                data["google_docs"]["token_path"] = str(data["google_docs"]["token_path"])

        with open(path, "wb") as f:
            tomli_w.dump(data, f)
    
    @classmethod
    def get_default_path(cls) -> Path:
        """Get the default configuration file path."""
        return Path.home() / ".config" / "chronix" / "config.toml"
    
    @classmethod
    def load_or_default(cls) -> "ChronixConfig":
        """Load configuration or return default if not found."""
        path = cls.get_default_path()
        if path.exists():
            return cls.from_toml(path)
        return cls()
    
    @classmethod
    def create_default(cls, path: Optional[Path] = None) -> "ChronixConfig":
        """Create a default configuration file."""
        if path is None:
            path = cls.get_default_path()
        
        config = cls()
        config.to_toml(path)
        return config
