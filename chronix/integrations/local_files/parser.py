"""Parser for extracting structured task data from local plain-text files."""

import re

from chronix.core.todo import TaskParser

_TITLE_PATTERN = re.compile(r"^#\s+(.*)$")
_SECTION_PATTERN = re.compile(r"^##\s+(.*)$")
_CHECKBOX_PATTERN = re.compile(r"^-\s*\[\s*([xX]?)\s*\]\s*(.*)$")

# Local files have exactly one checkbox list per section, so a single
# constant stands in for the per-list identity Google Docs assigns
# dynamically -- it only needs to be internally consistent, not globally
# unique, since chronix.core.todo.TaskParser only compares it against
# itself within one document_structure.
_LOCAL_CHECKBOX_LIST_ID = "local"


class ParsedParagraph:
    """A parsed paragraph, matching the shape chronix.core.todo consumes."""

    def __init__(self, text: str, bullet: dict | None = None, indent_level: int = 0):
        self.text = text
        self.bullet = bullet
        self.indent_level = indent_level

    def to_dict(self) -> dict:
        result = {
            "text": self.text,
            "style": "NORMAL_TEXT",
            "indent_level": self.indent_level,
        }
        if self.bullet:
            result["bullet"] = self.bullet
        return result


class ParsedSection:
    """A parsed section, equivalent to a Google Docs tab."""

    def __init__(self, title: str):
        self.title = title
        self.paragraphs: list[ParsedParagraph] = []
        self.checkbox_list_id: str | None = None

    def to_dict(self) -> dict:
        return {
            "title": self.title,
            "paragraphs": [p.to_dict() for p in self.paragraphs],
            "checkbox_list_id": self.checkbox_list_id,
        }


class DocumentStructure:
    """Raw structural data extracted from a local plain-text task file."""

    def __init__(self):
        self.title: str = ""
        self.document_id: str = ""
        self.tabs: list[ParsedSection] = []

    def to_dict(self) -> dict:
        return {
            "title": self.title,
            "document_id": self.document_id,
            "tabs": [tab.to_dict() for tab in self.tabs],
        }


class LocalFileParser:
    """Parses raw plain-text content into the source-agnostic document_structure shape."""

    def parse_document(self, content: str, document_id: str) -> DocumentStructure:
        structure = DocumentStructure()
        structure.document_id = document_id

        section = ParsedSection(title="")
        has_explicit_section = False

        for line in content.splitlines():
            title_match = _TITLE_PATTERN.match(line)
            if title_match:
                structure.title = title_match.group(1).strip()
                continue

            section_match = _SECTION_PATTERN.match(line)
            if section_match:
                if section.paragraphs or has_explicit_section:
                    structure.tabs.append(section)
                section = ParsedSection(title=section_match.group(1).strip())
                has_explicit_section = True
                continue

            self._parse_line(line, section)

        if section.paragraphs or not structure.tabs:
            structure.tabs.append(section)

        return structure

    def _parse_line(self, line: str, section: ParsedSection) -> None:
        stripped = line.strip()

        if stripped == TaskParser.TASK_IDENTIFIER:
            bullet = {
                "list_id": _LOCAL_CHECKBOX_LIST_ID,
                "nesting_level": 0,
                "has_strikethrough": False,
            }
            section.paragraphs.append(ParsedParagraph(text=stripped, bullet=bullet, indent_level=0))
            section.checkbox_list_id = _LOCAL_CHECKBOX_LIST_ID
            return

        checkbox_match = _CHECKBOX_PATTERN.match(stripped)
        if checkbox_match:
            checked, text = checkbox_match.groups()
            bullet = {
                "list_id": _LOCAL_CHECKBOX_LIST_ID,
                "nesting_level": 0,
                "has_strikethrough": checked != "",
            }
            section.paragraphs.append(ParsedParagraph(text=text.strip(), bullet=bullet, indent_level=0))
            return

        if not stripped:
            return

        indent_level = 1 if line[:1].isspace() else 0
        section.paragraphs.append(ParsedParagraph(text=stripped, indent_level=indent_level))
