"""Parser for extracting structured task data from Google Docs content."""

from typing import Any


class ParsedParagraph:
    """A parsed paragraph with text and metadata."""
    
    def __init__(self, text: str, bullet: dict[str, Any] | None = None, style: str = "NORMAL_TEXT"):
        self.text = text
        self.bullet = bullet
        self.style = style
    
    def to_dict(self) -> dict[str, Any]:
        """Convert to dictionary for serialization."""
        result = {
            "text": self.text,
            "style": self.style,
        }
        if self.bullet:
            result["bullet"] = self.bullet
        return result


class ParsedTab:
    """A parsed tab with its content."""
    
    def __init__(self, tab_id: str, title: str, index: int):
        self.tab_id = tab_id
        self.title = title
        self.index = index
        self.paragraphs: list[ParsedParagraph] = []
        self.checkbox_list_id: str | None = None
    
    def to_dict(self) -> dict[str, Any]:
        """Convert to dictionary for serialization."""
        return {
            "tab_id": self.tab_id,
            "title": self.title,
            "index": self.index,
            "paragraphs": [p.to_dict() for p in self.paragraphs],
            "checkbox_list_id": self.checkbox_list_id,
        }


class DocumentStructure:
    """Raw structural data extracted from a Google Docs document."""
    
    def __init__(self):
        self.title: str = ""
        self.document_id: str = ""
        self.tabs: list[ParsedTab] = []
    
    def to_dict(self) -> dict[str, Any]:
        """Convert to dictionary for serialization."""
        return {
            "title": self.title,
            "document_id": self.document_id,
            "tabs": [tab.to_dict() for tab in self.tabs],
        }


class GoogleDocsParser:
    """Parser for extracting raw structural content from Google Docs API responses."""

    TASK_IDENTIFIER = "TASKS ::: duration; external_deadline; user_deadline"

    def parse_document(self, doc: dict[str, Any]) -> DocumentStructure:
        """Extract structural content from a Google Docs document with tabs support."""
        structure = DocumentStructure()
        structure.title = doc.get("title", "")
        structure.document_id = doc.get("documentId", "")

        # Process tabs if present
        tabs = doc.get("tabs", [])
        if tabs:
            for tab_data in tabs:
                parsed_tab = self._process_tab(tab_data)
                if parsed_tab:
                    structure.tabs.append(parsed_tab)
        else:
            # Fallback: treat document body as a single unnamed tab (legacy support)
            content = doc.get("body", {}).get("content", [])
            if content:
                fallback_tab = ParsedTab(tab_id="legacy", title="", index=0)
                self._discover_tab_checkbox_list_id(content, fallback_tab)
                for element in content:
                    self._process_element(element, fallback_tab)
                structure.tabs.append(fallback_tab)
        
        return structure
    
    def _discover_tab_checkbox_list_id(self, content: list[dict[str, Any]], tab: ParsedTab) -> None:
        """Discover the checkbox list ID for a specific tab.
        
        Scans the tab's content for a paragraph with text matching exactly:
        "TASKS ::: duration; external_deadline; user_deadline"
        
        Sets tab.checkbox_list_id to the bullet.listId of that paragraph, or None if not found.
        """
        for element in content:
            if "paragraph" not in element:
                continue
            
            paragraph = element["paragraph"]
            
            # Check if it has a bullet
            if "bullet" not in paragraph:
                continue
            
            # Extract text from all text runs
            text_parts = []
            for elem in paragraph.get("elements", []):
                if "textRun" in elem:
                    text_run = elem["textRun"]
                    # Skip suggestions
                    if "suggestedInsertionIds" in text_run or "suggestedDeletionIds" in text_run:
                        continue
                    text_parts.append(text_run.get("content", ""))
            
            combined_text = "".join(text_parts).strip()
            
            # Check if this is the identifier line
            if combined_text == self.TASK_IDENTIFIER:
                list_id = paragraph["bullet"].get("listId")
                tab.checkbox_list_id = list_id
                return
        
        # If not found, checkbox_list_id remains None
        tab.checkbox_list_id = None
    
    def _process_tab(self, tab_data: dict[str, Any]) -> ParsedTab | None:
        """Process a single tab from the document."""
        tab_props = tab_data.get("tabProperties", {})
        tab_id = tab_props.get("tabId", "")
        title = tab_props.get("title", "")
        index = tab_props.get("index", 0)
        
        parsed_tab = ParsedTab(tab_id=tab_id, title=title, index=index)
        
        # Extract content from documentTab.body.content
        doc_tab = tab_data.get("documentTab", {})
        body = doc_tab.get("body", {})
        content = body.get("content", [])
        
        # Discover the checkbox list ID for this tab
        self._discover_tab_checkbox_list_id(content, parsed_tab)
        
        # Process all elements in this tab
        for element in content:
            self._process_element(element, parsed_tab)
        
        return parsed_tab
    
    def _process_element(self, element: dict[str, Any], tab: ParsedTab):
        """Process a single structural element from tab content."""
        if "paragraph" in element:
            self._process_paragraph(element["paragraph"], tab)
        elif "table" in element:
            self._process_table(element["table"], tab)
        # Note: sectionBreak is ignored per requirements
    
    def _process_paragraph(self, paragraph: dict[str, Any], tab: ParsedTab):
        """Extract text and metadata from a paragraph."""
        elements = paragraph.get("elements", [])
        
        # Concatenate all text runs and check for strikethrough
        text_parts = []
        has_strikethrough = False
        for elem in elements:
            if "textRun" in elem:
                text_run = elem["textRun"]
                # Skip suggestions
                if "suggestedInsertionIds" in text_run or "suggestedDeletionIds" in text_run:
                    continue
                text_content = text_run.get("content", "")
                text_parts.append(text_content)
                
                # Check for strikethrough in textStyle
                text_style = text_run.get("textStyle", {})
                if text_style.get("strikethrough", False):
                    has_strikethrough = True
        
        combined_text = "".join(text_parts).strip()
        
        # Skip empty paragraphs
        if not combined_text:
            return
        
        # Extract bullet metadata if present
        bullet_data = None
        if "bullet" in paragraph:
            bullet = paragraph["bullet"]
            bullet_data = {
                "list_id": bullet.get("listId"),
                "nesting_level": bullet.get("nestingLevel", 0),
            }
            
            # Check for strikethrough in bullet textStyle
            bullet_text_style = bullet.get("textStyle", {})
            if bullet_text_style.get("strikethrough", False):
                has_strikethrough = True
            
            # Store the strikethrough status
            bullet_data["has_strikethrough"] = has_strikethrough
        
        # Extract paragraph style
        named_style = paragraph.get("paragraphStyle", {}).get("namedStyleType", "NORMAL_TEXT")
        
        # Create parsed paragraph
        parsed_para = ParsedParagraph(
            text=combined_text,
            bullet=bullet_data,
            style=named_style
        )
        
        tab.paragraphs.append(parsed_para)
    
    def _process_table(self, table: dict[str, Any], tab: ParsedTab):
        """Process table structure by extracting cell content."""
        rows = table.get("tableRows", [])
        
        for row in rows:
            cells = row.get("tableCells", [])
            for cell in cells:
                cell_content = cell.get("content", [])
                for element in cell_content:
                    self._process_element(element, tab)


