"""CLI application entry point and command wiring."""

import sys
import os
from datetime import datetime, timezone
from typing import Optional

from prompt_toolkit import PromptSession
from prompt_toolkit.history import InMemoryHistory
from prompt_toolkit.key_binding import KeyBindings
from prompt_toolkit.document import Document

from chronix.cli.commands import (
    sync_command,
    today_command,
    calendar_command,
    documents_command,
    schedule_command,
    explain_command,
    help_command
)
from chronix.cli.config_commands import config_command
from chronix.cli.formatting import console


class ChronixShell:
    """Interactive REPL shell for chronix."""
    
    def __init__(self):
        self.running = False
        self.commands = {
            'sync': sync_command,
            'today': today_command,
            'calendar': calendar_command,
            'documents': documents_command,
            'schedule': schedule_command,
            'explain': explain_command,
            'config': config_command,
            'help': help_command,
            'exit': self._exit_command,
            'quit': self._exit_command,
            'clear': self._clear_command,
            'cls': self._clear_command,
        }
        
        # Set up command history with prompt_toolkit
        self.history = InMemoryHistory()
        
        # State for history navigation with temporary buffer
        self.history_nav_buffer = None  # Stores original input when navigating history
        self.history_nav_index = None   # Tracks current position in history during navigation
        
        # Set up key bindings for history navigation
        kb = self._create_key_bindings()
        
        self.prompt_session = PromptSession(
            history=self.history,
            enable_history_search=True,
            key_bindings=kb
        )
    
    def _create_key_bindings(self) -> KeyBindings:
        """Create custom key bindings for history navigation with buffer support."""
        kb = KeyBindings()
        
        @kb.add('up')
        def _(event):
            """Navigate to previous command in history, preserving unsaved input."""
            buffer = event.app.current_buffer
            history_items = buffer.history.get_strings()
            
            # Initialize navigation state if not already navigating
            if self.history_nav_index is None:
                self.history_nav_buffer = buffer.text
                # Start at the end of history (most recent)
                self.history_nav_index = len(history_items) - 1
            else:
                self.history_nav_index -= 1
            
            # Ensure index stays within bounds
            if self.history_nav_index < 0:
                self.history_nav_index = 0
            
            # Get the history item and display it
            if self.history_nav_index < len(history_items):
                buffer.document = Document(history_items[self.history_nav_index], 
                                          len(history_items[self.history_nav_index]))
        
        @kb.add('down')
        def _(event):
            """Navigate to next command in history, restoring saved input at the end."""
            buffer = event.app.current_buffer
            history_items = buffer.history.get_strings()
            
            # Only handle down if we're currently navigating history
            if self.history_nav_index is not None:
                self.history_nav_index += 1
                
                # If we've gone past the last history item, restore the original input
                if self.history_nav_index >= len(history_items):
                    buffer.document = Document(
                        self.history_nav_buffer,
                        len(self.history_nav_buffer)
                    )
                    self.history_nav_index = None
                    self.history_nav_buffer = None
                else:
                    # Display the next history item
                    buffer.document = Document(history_items[self.history_nav_index],
                                              len(history_items[self.history_nav_index]))
        
        return kb
    
    def _exit_command(self, args: list[str]) -> int:
        """Exit the shell."""
        self.running = False
        console.print("[dim]Goodbye![/dim]")
        return 0
    
    def _clear_command(self, args: list[str]) -> int:
        """Clear the terminal screen."""
        # Clear screen using ANSI escape codes (works on Unix/Mac/Windows 10+)
        os.system('cls' if os.name == 'nt' else 'clear')
        # Re-print the welcome header
        console.print("[bold cyan]chronix[/bold cyan] [dim]v0.1.0[/dim] — Interactive Shell")
        console.print("[dim]Type 'help' for available commands or 'exit' to quit.[/dim]\n")
        return 0
    
    def _read_continued_input(self, initial_input: str) -> str:
        """Read input with line continuation support (backslash at end of line)."""
        combined = initial_input
        while combined.rstrip().endswith('\\'):
            combined = combined.rstrip()[:-1].rstrip()
            try:
                next_line = self.prompt_session.prompt("... ").strip()
                combined = combined + ' ' + next_line if next_line else combined
            except EOFError:
                raise
        return combined
    
    def _execute_command_chain(self, user_input: str) -> bool:
        """Execute command chain with && and ; support. Returns True if successful.
        
        ; (semicolon) has lower precedence: segments are unconditional.
        && (ampersand-ampersand) has higher precedence: conditional within segment.
        """
        semicolon_segments = user_input.split(';')
        overall_success = True
        
        for segment in semicolon_segments:
            segment = segment.strip()
            if not segment:
                continue
            
            and_commands = segment.split('&&')
            segment_success = True
            
            for command_str in and_commands:
                command_str = command_str.strip()
                if not command_str:
                    continue
                
                parts = command_str.split()
                command_name = parts[0]
                args = parts[1:]
                
                if command_name not in self.commands:
                    console.print(f"[yellow]Unknown command:[/yellow] {command_name}")
                    console.print("[dim]Type 'help' for available commands.[/dim]")
                    segment_success = False
                    break
                
                command = self.commands[command_name]
                try:
                    result = command(args)
                    if result != 0:
                        segment_success = False
                        break
                except KeyboardInterrupt:
                    console.print("\n^C")
                    segment_success = False
                    break
                except Exception as e:
                    console.print(f"[red]Error executing command:[/red] {e}")
                    segment_success = False
                    break
            
            if not segment_success:
                overall_success = False
        
        return overall_success

    def run(self):
        """Run the interactive shell."""
        # Clear terminal on startup
        os.system('cls' if os.name == 'nt' else 'clear')
        
        self.running = True
        console.print("[bold cyan]chronix[/bold cyan] [dim]v0.1.0[/dim] — Interactive Shell")
        console.print("[dim]Type 'help' for available commands or 'exit' to quit.[/dim]\n")
        
        # Auto-run sync on REPL startup
        console.print("[dim]Running initial sync...[/dim]\n")
        try:
            sync_command([])
        except Exception as e:
            console.print(f"[yellow]⚠️[/yellow]  Initial sync failed: {e}")
            console.print("[dim]You can retry with the 'sync' command.[/dim]\n")
        
        while self.running:
            try:
                user_input = self.prompt_session.prompt("chronix> ").strip()
                
                if not user_input:
                    continue
                
                user_input = self._read_continued_input(user_input)
                self._execute_command_chain(user_input)
                
                # Reset history navigation state after command execution
                self.history_nav_buffer = None
                self.history_nav_index = None
            
            except KeyboardInterrupt:
                console.print("\n[dim]Use 'exit' or 'quit' to leave the shell.[/dim]")
                # Reset navigation state on interrupt
                self.history_nav_buffer = None
                self.history_nav_index = None
                continue
            except EOFError:
                console.print("\n[dim]Goodbye![/dim]")
                break
    
    def execute_one_shot(self, command_name: str, args: list[str]) -> int:
        """Execute a single command and exit."""
        if command_name not in self.commands:
            console.print(f"[yellow]Unknown command:[/yellow] {command_name}")
            console.print("[dim]Type 'chronix help' for available commands.[/dim]")
            return 1
        
        command = self.commands[command_name]
        try:
            return command(args)
        except Exception as e:
            console.print(f"[red]Error:[/red] {e}")
            return 1


def main():
    """Main CLI entry point."""
    # Clear terminal on startup
    os.system('cls' if os.name == 'nt' else 'clear')
    
    shell = ChronixShell()
    
    if len(sys.argv) > 1:
        # One-shot command mode
        command_name = sys.argv[1]
        args = sys.argv[2:]
        return shell.execute_one_shot(command_name, args)
    else:
        # Interactive mode
        try:
            shell.run()
            return 0
        except KeyboardInterrupt:
            console.print("\n[dim]Goodbye![/dim]")
            return 0


if __name__ == "__main__":
    sys.exit(main())
