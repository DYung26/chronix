from types import SimpleNamespace
from unittest.mock import Mock, patch

from chronix.cli import commands
from chronix.config import ChronixConfig, GoogleDocsSourceConfig, ProjectConfig


def test_tabs_command_uses_project_name_for_document_label():
    config = ChronixConfig(
        projects=[
            ProjectConfig(
                name="maestro",
                sources=[GoogleDocsSourceConfig(document_id="document-123")],
            )
        ]
    )
    client = Mock()
    client.authenticate.return_value = True
    client.fetch_document.return_value = object()
    document_structure = SimpleNamespace(
        tabs=[
            SimpleNamespace(
                title="TASKS",
                tab_id="tab-123",
                checkbox_list_id="list-123",
            )
        ]
    )

    with (
        patch.object(commands._context, "config", config),
        patch.object(commands._context, "_ensure_google_client", return_value=client),
        patch.object(commands, "GoogleDocsParser") as parser,
        patch.object(commands, "console") as console,
    ):
        parser.return_value.parse_document.return_value = document_structure

        result = commands.tabs_command(["maestro"])

    assert result == 0
    console.print.assert_any_call("[bold]Tabs in maestro:[/bold]")
