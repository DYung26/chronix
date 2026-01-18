"""Configuration management commands for the CLI."""

from pathlib import Path
from typing import Optional

from chronix.config import ChronixConfig


def config_init_command(args: list[str]) -> int:
    """
    Initialize a default configuration file.
    
    Usage: config init [--force]
    """
    force = "--force" in args or "-f" in args
    
    config_path = ChronixConfig.get_default_path()
    
    if config_path.exists() and not force:
        print(f"Configuration file already exists: {config_path}")
        print("Use --force to overwrite")
        return 1
    
    try:
        # Create default configuration with sensible defaults
        config = ChronixConfig()
        
        # Add a default lunch break
        from datetime import time
        from chronix.config.settings import TimeBlockConfig
        
        config.scheduling.breaks.append(
            TimeBlockConfig(
                start_time=time(12, 0),
                end_time=time(13, 0),
                kind="break",
                label="Lunch",
                days=["monday", "tuesday", "wednesday", "thursday", "friday"]
            )
        )
        
        # Save configuration
        config.to_toml(config_path)
        
        print(f"✓ Configuration initialized at: {config_path}")
        print()
        print("Default settings:")
        print(f"  Work hours: {config.scheduling.work_start_time} - {config.scheduling.work_end_time}")
        print(f"  Timezone: {config.scheduling.timezone}")
        print(f"  Default task duration: {config.scheduling.default_task_duration_minutes} minutes")
        print()
        print("Edit the file to customize your schedule, breaks, and meetings.")
        
        return 0
    
    except Exception as e:
        print(f"Failed to initialize configuration: {e}")
        return 1


def config_show_command(args: list[str]) -> int:
    """
    Show current configuration.
    
    Usage: config show
    """
    try:
        config_path = ChronixConfig.get_default_path()
        
        if not config_path.exists():
            print(f"No configuration found at: {config_path}")
            print("Run 'chronix config init' to create a default configuration.")
            return 1
        
        config = ChronixConfig.from_toml(config_path)
        
        print(f"Configuration: {config_path}")
        print()
        
        # Scheduling settings
        print("📅 Scheduling:")
        print(f"   Work hours: {config.scheduling.work_start_time.strftime('%H:%M')} - {config.scheduling.work_end_time.strftime('%H:%M')}")
        print(f"   Timezone: {config.scheduling.timezone}")
        print(f"   Default task duration: {config.scheduling.default_task_duration_minutes} minutes")
        print()
        
        # Sleep windows
        if config.scheduling.sleep_windows:
            print("😴 Sleep windows:")
            for block in config.scheduling.sleep_windows:
                days = ", ".join(block.days[:3]) + ("..." if len(block.days) > 3 else "")
                print(f"   {block.start_time.strftime('%H:%M')} - {block.end_time.strftime('%H:%M')} ({days})")
            print()
        
        # Breaks
        if config.scheduling.breaks:
            print("☕ Breaks:")
            for block in config.scheduling.breaks:
                days = ", ".join(block.days[:3]) + ("..." if len(block.days) > 3 else "")
                label = f" - {block.label}" if block.label else ""
                print(f"   {block.start_time.strftime('%H:%M')} - {block.end_time.strftime('%H:%M')} ({days}){label}")
            print()
        
        # Meetings
        if config.scheduling.meetings:
            print("📞 Recurring meetings:")
            for block in config.scheduling.meetings:
                days = ", ".join(block.days[:3]) + ("..." if len(block.days) > 3 else "")
                label = f" - {block.label}" if block.label else ""
                print(f"   {block.start_time.strftime('%H:%M')} - {block.end_time.strftime('%H:%M')} ({days}){label}")
            print()
        
        # Google Docs settings
        print("📄 Google Docs:")
        print(f"   Auth method: {config.google_docs.auth_method}")
        print(f"   Credentials: {config.google_docs.credentials_path}")
        print(f"   Token cache: {config.google_docs.token_path}")
        if config.google_docs.document_ids:
            print(f"   Documents: {len(config.google_docs.document_ids)} configured")
            for doc_id in config.google_docs.document_ids[:3]:
                print(f"     • {doc_id}")
            if len(config.google_docs.document_ids) > 3:
                print(f"     ... and {len(config.google_docs.document_ids) - 3} more")
        else:
            print(f"   Documents: None configured")
        
        return 0
    
    except Exception as e:
        print(f"Failed to load configuration: {e}")
        return 1


def config_path_command(args: list[str]) -> int:
    """
    Show the configuration file path.
    
    Usage: config path
    """
    config_path = ChronixConfig.get_default_path()
    print(config_path)
    return 0


def config_validate_command(args: list[str]) -> int:
    """
    Validate the current configuration file.
    
    Usage: config validate
    """
    try:
        config_path = ChronixConfig.get_default_path()
        
        if not config_path.exists():
            print(f"No configuration found at: {config_path}")
            return 1
        
        print(f"Validating: {config_path}")
        
        config = ChronixConfig.from_toml(config_path)
        
        print("✓ Configuration is valid")
        print()
        print("Summary:")
        print(f"  • Work hours: {config.scheduling.work_start_time} - {config.scheduling.work_end_time}")
        print(f"  • Sleep windows: {len(config.scheduling.sleep_windows)}")
        print(f"  • Breaks: {len(config.scheduling.breaks)}")
        print(f"  • Meetings: {len(config.scheduling.meetings)}")
        print(f"  • Documents: {len(config.google_docs.document_ids)}")
        
        return 0
    
    except Exception as e:
        print(f"✗ Configuration is invalid: {e}")
        return 1


def config_command(args: list[str]) -> int:
    """
    Configuration management command dispatcher.
    
    Usage: config <subcommand>
    
    Subcommands:
      init      Initialize a default configuration file
      show      Display current configuration
      path      Show configuration file path
      validate  Validate configuration file
    """
    if not args:
        print("Usage: config <subcommand>")
        print()
        print("Subcommands:")
        print("  init      Initialize a default configuration file")
        print("  show      Display current configuration")
        print("  path      Show configuration file path")
        print("  validate  Validate configuration file")
        return 1
    
    subcommand = args[0]
    subargs = args[1:]
    
    subcommands = {
        "init": config_init_command,
        "show": config_show_command,
        "path": config_path_command,
        "validate": config_validate_command,
    }
    
    if subcommand not in subcommands:
        print(f"Unknown subcommand: {subcommand}")
        print("Run 'chronix config' to see available subcommands")
        return 1
    
    return subcommands[subcommand](subargs)
