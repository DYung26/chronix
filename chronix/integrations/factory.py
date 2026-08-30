"""Factory for constructing TaskSourceIntegration clients and TaskWriters by source type.

The single seam that knows about concrete integration packages
(google_docs, local_files). Everything else -- sync_helpers, cli/commands,
mcp/tools_write -- programs against SourceRef and the abstract
TaskSourceIntegration/TaskWriter interfaces, and never imports a concrete
integration module directly.
"""

from datetime import timezone
from typing import Any

from chronix.config.settings import SourceRef
from chronix.core.writer import TaskWriter


def get_parser(source_type: str) -> Any:
    """Return the document_structure parser for a source type.

    The returned object exposes `parse_document(...) -> DocumentStructure`,
    but the exact call signature differs by source type (Google Docs takes
    a raw API response dict; local files take (content, document_id)) --
    callers already branch on source_type to call fetch_document correctly,
    so they branch here too rather than this factory hiding a signature
    mismatch behind a uniform-looking call.
    """
    if source_type == "google_docs":
        from chronix.integrations.google_docs.parser import GoogleDocsParser
        return GoogleDocsParser()
    if source_type == "local_files":
        from chronix.integrations.local_files.parser import LocalFileParser
        return LocalFileParser()
    raise ValueError(f"Unknown source type: {source_type}")


def get_client(source: SourceRef) -> Any:
    """Return a TaskSourceIntegration client for a configured source.

    For google_docs, source.source_id is unused here (the client
    authenticates once and takes document_id per fetch_document call). For
    local_files, source.source_id is the file path, bound into the client
    at construction time since LocalFileClient is scoped to one file.
    """
    if source.type == "google_docs":
        from chronix.integrations.google_docs.client import GoogleDocsClient
        return GoogleDocsClient()
    if source.type == "local_files":
        from chronix.integrations.local_files.client import LocalFileClient
        return LocalFileClient(source.source_id)
    raise ValueError(f"Unknown source type: {source.type}")


def get_writer(source_type: str, tz: timezone = timezone.utc) -> TaskWriter:
    """Return a TaskWriter for a source type.

    Google Docs' writer additionally needs an AuthStrategy, which callers
    that already hold an authenticated GoogleDocsClient should pass through
    via get_writer_for_client instead of calling this directly for that type.
    """
    if source_type == "google_docs":
        from chronix.integrations.google_docs.writer import GoogleDocsTaskWriter
        return GoogleDocsTaskWriter(tz=tz)
    if source_type == "local_files":
        from chronix.integrations.local_files.writer import LocalFileTaskWriter
        return LocalFileTaskWriter(tz=tz)
    raise ValueError(f"Unknown source type: {source_type}")


def get_writer_for_client(source_type: str, client: Any, tz: timezone = timezone.utc) -> TaskWriter:
    """Return a TaskWriter reusing an already-constructed client's auth/identity.

    Preferred over get_writer when a client for this source was already
    built (e.g. by get_client), so Google Docs' writer reuses the client's
    AuthStrategy rather than triggering a second, redundant auth flow.
    """
    if source_type == "google_docs":
        from chronix.integrations.google_docs.writer import GoogleDocsTaskWriter
        return GoogleDocsTaskWriter(auth_strategy=client.auth_strategy, tz=tz)
    if source_type == "local_files":
        from chronix.integrations.local_files.writer import LocalFileTaskWriter
        return LocalFileTaskWriter(tz=tz)
    raise ValueError(f"Unknown source type: {source_type}")


def fetch_raw_document(client: Any, source: SourceRef) -> Any:
    """Fetch raw source data (Docs API response dict, or local file content) uniformly.

    Returns whatever that source type's parser.parse_document expects as
    input -- for google_docs, the raw API dict; for local_files, the file's
    text content (document_id is passed separately by the caller, matching
    LocalFileParser.parse_document's (content, document_id) signature).
    """
    if source.type == "google_docs":
        return client.fetch_document(source.source_id)
    if source.type == "local_files":
        return client.fetch_document()["content"]
    raise ValueError(f"Unknown source type: {source.type}")


def backfill_source_data(raw: Any, source: SourceRef) -> Any:
    """Convert fetch_raw_document's output into what this source's TaskWriter.backfill_missing_ids expects.

    GoogleDocsTaskWriter.backfill_missing_ids expects the raw API dict as-is
    (its source_data param), matching fetch_raw_document's google_docs
    output directly. LocalFileTaskWriter.backfill_missing_ids expects a
    list of lines (it enumerates and rewrites individual lines), not the
    raw file content string fetch_raw_document returns for local_files --
    splitting here keeps that line-oriented contract out of sync_helpers.py.
    """
    if source.type == "google_docs":
        return raw
    if source.type == "local_files":
        return raw.splitlines()
    raise ValueError(f"Unknown source type: {source.type}")
