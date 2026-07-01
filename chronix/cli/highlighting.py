"""Live syntax highlighting for REPL input."""

import re

from prompt_toolkit.document import Document
from prompt_toolkit.lexers import Lexer
from prompt_toolkit.styles import Style

_TOKEN_RE = re.compile(r"\s+|&&|;|--[A-Za-z][\w-]*|\S+")
_CHAIN_SEPARATORS = {"&&", ";"}

chronix_style = Style.from_dict({
    "command": "fg:cyan bold",
    "unknown-command": "fg:red",
    "flag": "fg:yellow",
    "separator": "fg:#888888",
})


class ChronixCommandLexer(Lexer):
    """Highlights command names and --flags as the user types in the REPL."""

    def __init__(self, known_commands: frozenset[str]):
        self._known_commands = known_commands

    def lex_document(self, document: Document):
        lines = document.lines

        def get_line(lineno: int):
            return _tokenize_line(lines[lineno], self._known_commands)

        return get_line


def _tokenize_line(line: str, known_commands: frozenset[str]) -> list[tuple[str, str]]:
    tokens: list[tuple[str, str]] = []
    expect_command = True

    for match in _TOKEN_RE.finditer(line):
        text = match.group()

        if text.isspace():
            tokens.append(("", text))
        elif text in _CHAIN_SEPARATORS:
            tokens.append(("class:separator", text))
            expect_command = True
        elif text.startswith("--"):
            tokens.append(("class:flag", text))
        elif expect_command:
            style = "class:command" if text in known_commands else "class:unknown-command"
            tokens.append((style, text))
            expect_command = False
        else:
            tokens.append(("", text))

    return tokens
