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
        windows = config.scheduling.effective_work_windows()
        if len(windows) == 1:
            print(f"   Work hours: {windows[0].start_time.strftime('%H:%M')} - {windows[0].end_time.strftime('%H:%M')}")
        else:
            window_strs = ", ".join(f"{w.start_time.strftime('%H:%M')}–{w.end_time.strftime('%H:%M')}" for w in windows)
            print(f"   Work windows: {window_strs}")
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
        
        # Projects and their sources
        print("📄 Projects:")
        print(f"   Auth method: {config.google_docs.auth_method}")
        print(f"   Credentials: {config.google_docs.credentials_path}")
        print(f"   Token cache: {config.google_docs.token_path}")
        if config.projects:
            print(f"   Projects: {len(config.projects)} configured")
            ranked = sorted(
                config.projects,
                key=lambda p: p.priority if p.priority is not None else float("inf")
            )
            for project in ranked[:3]:
                label = project.label()
                priority_str = f" [priority {project.priority}]" if project.priority is not None else ""
                source_types = ", ".join(s.type for s in project.sources)
                print(f"     • {label}{priority_str} ({source_types})")
            if len(ranked) > 3:
                print(f"     ... and {len(ranked) - 3} more")
        else:
            print(f"   Projects: None configured")
        print()
        
        # Startup commands
        print("🚀 Startup:")
        for tokens in config.startup.commands:
            print(f"   {' '.join(tokens)}")
        
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


def config_reload_command(args: list[str]) -> int:
    """
    Reload configuration from disk into the current session.

    Usage: config reload

    Chronix loads config.toml once (at REPL startup, or on-demand in
    one-shot mode) and keeps using that in-memory copy for the rest of the
    session. Edits made to config.toml on disk have no effect until this is
    run, or the session is restarted.
    """
    from chronix.cli.commands import _context

    config_path = ChronixConfig.get_default_path()

    if not config_path.exists():
        print(f"No configuration found at: {config_path}")
        return 1

    try:
        config = ChronixConfig.from_toml(config_path)
    except Exception as e:
        print(f"Failed to reload configuration: {e}")
        print("Your existing in-memory configuration is unchanged.")
        return 1

    _context.config = config

    # Project priority gets baked into each synced project's ProjectContext
    # at sync time, and from there stamped onto each Task the first time
    # it's aggregated (TaskAggregator._enrich_task_with_project only assigns
    # task.priority when it's still None, so it never overwrites after that
    # first stamp). It's not re-read from config afterwards, so without
    # this, editing a project's priority in config.toml and running
    # `config reload` has no effect on scheduling until a full `sync`
    # rebuilds the Task objects from scratch. Refresh it here, in place, and
    # reset task.priority so the next aggregate() call re-stamps it from the
    # freshly reloaded config.
    refreshed_projects = 0
    refreshed_tasks = 0
    for project in _context.projects:
        project_config = config.find_project(project.project_context.project_id)
        if project_config is None:
            continue
        project.project_context.priority = project_config.priority
        refreshed_projects += 1
        for task in project.tasks:
            task.priority = None
            refreshed_tasks += 1

    print(f"✓ Configuration reloaded from: {config_path}")
    if refreshed_projects:
        print(f"✓ Refreshed priority for {refreshed_projects} synced project(s) ({refreshed_tasks} task(s)).")
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
        windows = config.scheduling.effective_work_windows()
        if len(windows) == 1:
            print(f"  • Work hours: {windows[0].start_time} - {windows[0].end_time}")
        else:
            window_strs = ", ".join(f"{w.start_time}–{w.end_time}" for w in windows)
            print(f"  • Work windows: {window_strs}")
        print(f"  • Sleep windows: {len(config.scheduling.sleep_windows)}")
        print(f"  • Breaks: {len(config.scheduling.breaks)}")
        print(f"  • Meetings: {len(config.scheduling.meetings)}")
        print(f"  • Projects: {len(config.projects)}")
        print(f"  • Startup commands: {len(config.startup.commands)}")
        
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
      reload    Reload config.toml into the current session
    """
    if not args:
        print("Usage: config <subcommand>")
        print()
        print("Subcommands:")
        print("  init      Initialize a default configuration file")
        print("  show      Display current configuration")
        print("  path      Show configuration file path")
        print("  validate  Validate configuration file")
        print("  reload    Reload config.toml into the current session")
        return 1
    
    subcommand = args[0]
    subargs = args[1:]
    
    subcommands = {
        "init": config_init_command,
        "show": config_show_command,
        "path": config_path_command,
        "validate": config_validate_command,
        "reload": config_reload_command,
    }
    
    if subcommand not in subcommands:
        print(f"Unknown subcommand: {subcommand}")
        print("Run 'chronix config' to see available subcommands")
        return 1
    
    return subcommands[subcommand](subargs)
