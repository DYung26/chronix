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


class WorkWindowConfig(BaseModel):
    """A single contiguous work window within a day."""

    start_time: time
    end_time: time

    @model_validator(mode="after")
    def validate_times(self):
        if self.start_time >= self.end_time:
            raise ValueError("start_time must be before end_time")
        return self


class SchedulingConfig(BaseModel):
    """Configuration for task scheduling behavior."""

    work_start_time: time = Field(default=time(9, 0), description="Daily work start time (single-window fallback)")
    work_end_time: time = Field(default=time(18, 0), description="Daily work end time (single-window fallback)")
    work_windows: list[WorkWindowConfig] = Field(
        default_factory=list,
        description="Multiple work windows per day (overrides work_start_time/work_end_time when non-empty)",
    )
    timezone: str = Field(default="UTC", description="Timezone for scheduling")
    default_task_duration_minutes: int = Field(default=60, ge=1, description="Default task duration if not specified")

    sleep_windows: list[TimeBlockConfig] = Field(default_factory=list, description="Sleep time blocks")
    breaks: list[TimeBlockConfig] = Field(default_factory=list, description="Break time blocks")
    meetings: list[TimeBlockConfig] = Field(default_factory=list, description="Recurring meeting blocks")

    @model_validator(mode="after")
    def validate_work_hours(self):
        if not self.work_windows:
            if self.work_start_time >= self.work_end_time:
                raise ValueError("work_start_time must be before work_end_time")
        return self

    def effective_work_windows(self) -> list["WorkWindowConfig"]:
        """Return the active work windows, falling back to single-window config."""
        if self.work_windows:
            return sorted(self.work_windows, key=lambda w: w.start_time)
        return [WorkWindowConfig(start_time=self.work_start_time, end_time=self.work_end_time)]

    def get_default_task_duration(self) -> timedelta:
        """Get default task duration as timedelta."""
        return timedelta(minutes=self.default_task_duration_minutes)


class GoogleDocsSourceConfig(BaseModel):
    """A Google Docs document as one source within a project.

    Holds only what's needed to fetch/write this specific document --
    priority is project-level now (see ProjectConfig), since a project's
    scheduling identity shouldn't differ depending on which of its sources
    you're looking at.
    """

    type: Literal["google_docs"] = "google_docs"
    document_id: str


class LocalFileSourceConfig(BaseModel):
    """A local plain-text task file as one source within a project."""

    type: Literal["local_files"] = "local_files"
    file_path: str

    @field_validator("file_path")
    @classmethod
    def expand_file_path(cls, v: str) -> str:
        return str(Path(v).expanduser().resolve())


# A project's source list holds at most one of each type (see
# ProjectConfig.validate_at_most_one_per_type) -- Pydantic's discriminated
# union picks the right model from each entry's `type` field.
ProjectSourceConfig = GoogleDocsSourceConfig | LocalFileSourceConfig


class ProjectConfig(BaseModel):
    """Configuration for one project: an identity plus the source(s) it draws tasks from.

    A project may have zero, one Google Docs source, and/or one local-files
    source (at most one of each type -- see validate_at_most_one_per_type).
    Two sources under the same project are treated as the same backlog:
    tasks sharing an id across them are deduplicated or flagged as
    conflicting (see chronix.core.aggregation). Sources under *different*
    projects never merge this way even if a task id somehow collided --
    that would indicate a real anomaly (e.g. an id collision), not a
    legitimate cross-project mirror, and is reported as an error rather than
    silently merged.
    """

    name: str = Field(description="The project's identity. Used for lookups, priority, and as the project label shown in commands.")
    priority: Optional[int] = Field(
        default=None,
        description=(
            "Scheduling priority rank for this project's tasks, relative to other "
            "projects. Lower number = higher priority (1 is highest). Unset means "
            "unranked, which is treated as lowest priority. This is a soft nudge on "
            "top of deadline-driven scheduling, not a hard override: a lower-priority "
            "project's task with a critical deadline is still protected."
        ),
    )
    sources: list[ProjectSourceConfig] = Field(
        default_factory=list,
        description="The source(s) this project draws tasks from: at most one google_docs and one local_files entry.",
    )

    @model_validator(mode="after")
    def validate_at_most_one_per_type(self):
        types_seen = [s.type for s in self.sources]
        duplicates = {t for t in types_seen if types_seen.count(t) > 1}
        if duplicates:
            raise ValueError(
                f"Project '{self.name}' has more than one source of type(s) {sorted(duplicates)}. "
                f"A project may have at most one google_docs source and one local_files source."
            )
        return self

    def source_id_for(self, source_type: str) -> Optional[str]:
        """Return this project's document_id/file_path for the given source type, if it has one."""
        for source in self.sources:
            if source.type == source_type:
                return source.document_id if source.type == "google_docs" else source.file_path
        return None

    def label(self) -> str:
        """Format for display: the project's name."""
        return self.name


class GoogleDocsConfig(BaseModel):
    """Connection-level configuration for Google Docs integration.

    Per-document configuration (which documents to sync) now lives on each
    project's sources list (see ProjectConfig) -- this section only holds
    settings that apply to the connection as a whole, regardless of how many
    documents or projects use it.
    """

    auth_method: Literal["oauth", "service_account"] = Field(default="oauth")
    credentials_path: Optional[Path] = Field(default=None, description="Path to OAuth credentials or service account key")
    token_path: Optional[Path] = Field(default=None, description="Path to OAuth token cache")

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


class StartupConfig(BaseModel):
    """Configuration for commands run automatically when the REPL starts."""

    commands: list[list[str]] = Field(
        default_factory=lambda: [["sync"]],
        description=(
            "Shell commands to run in order at REPL startup, each as a list of "
            "tokens (command name followed by its arguments), e.g. [\"sync\"] or "
            "[\"sync\", \"my-project\"]. A failing command is reported but does not "
            "stop the ones after it."
        ),
    )

    @field_validator("commands")
    @classmethod
    def validate_commands_nonempty(cls, v: list[list[str]]) -> list[list[str]]:
        for tokens in v:
            if not tokens:
                raise ValueError("Each startup command must have at least one token (the command name)")
        return v


class SourceRef(BaseModel):
    """A resolved reference to one source of one configured project.

    The unified iteration surface for code that needs to fetch/write a
    single source (sync, write commands) without knowing about
    GoogleDocsSourceConfig/LocalFileSourceConfig individually. ``source_id``
    is the document_id or file_path -- whichever identifier that source
    type's TaskWriter/TaskSourceIntegration expects. ``project_name`` and
    ``priority`` are carried from the owning ProjectConfig so downstream
    code (sync, aggregation) can stamp them onto the resulting
    ProjectContext without a second config lookup.
    """

    type: Literal["google_docs", "local_files"]
    source_id: str
    project_name: str
    priority: Optional[int] = None

    def label(self) -> str:
        """Format for display: the owning project's name."""
        return self.project_name


class ChronixConfig(BaseModel):
    """Root configuration for chronix."""

    scheduling: SchedulingConfig = Field(default_factory=SchedulingConfig)
    google_docs: GoogleDocsConfig = Field(default_factory=GoogleDocsConfig)
    projects: list[ProjectConfig] = Field(default_factory=list, description="Projects to sync, each with one or more sources")
    startup: StartupConfig = Field(default_factory=StartupConfig)

    @model_validator(mode="after")
    def validate_project_name_uniqueness(self):
        names = [p.name for p in self.projects]
        seen_names = set()
        for name in names:
            if name in seen_names:
                raise ValueError(f"Duplicate project name '{name}' found in configuration")
            seen_names.add(name)
        return self

    def all_sources(self) -> list[SourceRef]:
        """Every configured source across every project, as SourceRefs.

        Projects are iterated in configured order; within a project,
        google_docs is listed before local_files if both are present. This
        ordering has no scheduling significance -- sync/aggregation treat
        all sources as peers regardless of position here.
        """
        refs = []
        for project in self.projects:
            for source in project.sources:
                source_id = source.document_id if source.type == "google_docs" else source.file_path
                refs.append(SourceRef(
                    type=source.type,
                    source_id=source_id,
                    project_name=project.name,
                    priority=project.priority,
                ))
        return refs

    def find_project(self, token: str) -> Optional[ProjectConfig]:
        """Resolve a token (project name) to its ProjectConfig."""
        for project in self.projects:
            if project.name == token:
                return project
        return None

    def resolve_source(self, token: str) -> Optional[SourceRef]:
        """Resolve a token (project name) to a SourceRef for that project.

        A project can have two sources (one google_docs, one local_files);
        when both are present, google_docs is preferred as the single
        SourceRef returned here, since callers using this method want one
        canonical source to act on (e.g. a document-specific command like
        `tabs`) rather than every source. Callers that need every source for
        a project (e.g. sync) should use all_sources() filtered by
        project_name instead.
        """
        project = self.find_project(token)
        if project is None:
            return None
        for preferred_type in ("google_docs", "local_files"):
            for source in project.sources:
                if source.type == preferred_type:
                    source_id = source.document_id if source.type == "google_docs" else source.file_path
                    return SourceRef(
                        type=source.type,
                        source_id=source_id,
                        project_name=project.name,
                        priority=project.priority,
                    )
        return None

    def sources_for_project(self, project_name: str) -> list[SourceRef]:
        """Every SourceRef belonging to a single project, by its canonical name."""
        return [s for s in self.all_sources() if s.project_name == project_name]

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
