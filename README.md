# Chronix

A task management CLI tool that syncs tasks from Google Docs and schedules them intelligently based on deadlines, duration, and blocked time.

## Overview

**Chronix** consolidates tasks from Google Docs into a unified, time-aware schedule. It features an interactive REPL with command history and a clean, readable interface for managing your daily workflow.

## Features

- **Google Docs Integration**: Automatically syncs tasks from Google Docs with tab-aware parsing
- **Interactive REPL**: Command history, clear screen, and auto-sync on startup
- **Smart Scheduling**: Time-aware task placement considering deadlines, duration, and blocked periods
- **Complete Timeline View**: See your full day including tasks, breaks, sleep, and empty time slots
- **Task Management**: Track incomplete and completed tasks with detailed metadata

## Installation

Install Chronix from source:

```bash
pip install -e .
```

## Quick Start

### 1. Initialize Configuration

Create the default configuration file:

```bash
chronix config init
```

This creates a configuration file at:

```
~/.config/chronix/config.toml
```

### 2. Set Up Google Authentication

Chronix supports two authentication methods: **OAuth** (recommended for personal use) and **Service Account** (for automated/service environments).

#### Authentication Precedence

**Important**: Service account credentials take priority over OAuth. If both exist, the service account will be used. To use OAuth, do not provide service account credentials.

#### Option A: OAuth (Recommended)

Place your Google Cloud OAuth credentials in:

```
~/.config/chronix/google/credentials.json
```

On first use, Chronix will open a browser for authentication and store the token at:

```
~/.config/chronix/google/token.json
```

#### Option B: Service Account

Place your service account key file at:

```
~/.config/chronix/google/service_account.json
```

**Note**: To obtain credentials, visit the [Google Cloud Console](https://console.cloud.google.com/apis/credentials) and enable the Google Docs API.

### 3. Configure Document IDs

Edit `~/.config/chronix/config.toml` and add your Google Docs document IDs:

```toml
[google_docs]
document_ids = [
    "1JiPapSKWzs775Kl7h_MlkK-aLjRMEMN-uvjntNBLAB8"
]
```

**Important**: Only provide document IDs, not document names. Chronix automatically retrieves document titles from the Google Docs API.

## Usage

### Interactive Mode (REPL)

Start the interactive shell:

```bash
chronix
```

The REPL automatically runs `sync` on startup to fetch the latest tasks.

Available commands:
- `sync` - Fetch and parse all configured documents
- `today` - Display today's complete schedule
- `explain <task_id>` - Show detailed information about a specific task
- `clear` or `cls` - Clear the terminal screen
- `help` - Show available commands
- `exit` or `quit` - Exit the REPL

**REPL Features**:
- Command history: Use UP/DOWN arrows to navigate previous commands
- Auto-sync: Tasks are synced automatically when entering the REPL

### One-Shot Commands

Run commands directly without entering the REPL:

```bash
chronix sync          # Sync tasks from Google Docs
chronix today         # View today's schedule
chronix explain xyz   # Get details about task with ID xyz
```

### Configuration Commands

Manage your configuration:

```bash
chronix config init      # Create default config file
chronix config show      # Display current configuration
chronix config path      # Show config file location
chronix config validate  # Validate configuration
```

## Google Docs Task Format

### Tab Structure

Each Google Docs document must contain a special identifier line in each tab that has tasks. This line identifies the checkbox list used for tasks:

```
TASKS ::: duration; external_deadline; user_deadline
```

**Important**: This identifier line must be a checkbox item, but is NOT treated as a task itself.

### Task Format

A line is considered a task if:

1. It is a checkbox in the same list as the identifier line
2. It contains valid task metadata with the `:::` separator
3. It is NOT the identifier line itself

Task format:

```
<task title> ::: <duration>; <external_deadline>; <user_deadline>
```

Example:

```
Implement TUI ::: 3hours; 2026-01-19T12:00; 2026-01-18T18:00
```

### Metadata Fields

- **Duration**: Specify time in `<number>hours` or `<number>minutes` (e.g., `3hours`, `30minutes`)
- **External Deadline**: ISO-8601 datetime (e.g., `2026-01-19T12:00` or `2026-01-19T12:00+00:00`), or `-` for none
- **User Deadline**: ISO-8601 datetime or `-` for none

Both datetime formats are supported:
- Without timezone: `2026-01-09T12:00`
- With timezone: `2026-01-09T12:00+00:00`

### Completed Tasks

A task is marked as completed if either:
- Any text in the task has `strikethrough` formatting
- The checkbox bullet has `strikethrough` formatting

### Tab Behavior

- The `todo` tab is treated specially and excluded from task derivation
- Each tab can have its own checkbox list
- Tasks are parsed per-tab, respecting tab-specific list IDs

## Output

### Sync Summary

After syncing, Chronix displays:

```
Sync complete!
  Projects: 1
  Total tasks: 7
  Incomplete tasks: 5
  Completed tasks: 2
```

### Today's Schedule

The `today` command shows a complete timeline including:
- Scheduled tasks with full metadata (project, tab, duration, ID, deadline)
- Blocked time (breaks, sleep, static meetings)
- Empty time slots where no task is assigned

Example output:

```
Schedule for Today
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

1. 13:10 – 13:25  📋 Implement TUI
   [chronix] • Tab 2
   Duration: 15m | ID: 8mNX9CEB
   Deadline: 2026-01-19 00:00

2. 13:25 – 14:00  (empty)

3. 14:00 – 14:15  ☕ Break
```

## Architecture

Chronix maintains clear separation between components:

- **Core domain logic** (`chronix/core/`): Task models, scheduling engine, aggregation
- **Integrations** (`chronix/integrations/`): Platform-specific adapters (Google Docs)
- **CLI interface** (`chronix/cli/`): REPL, command routing, formatted output
- **Configuration** (`chronix/config/`): TOML-based settings with Pydantic validation
- **Utilities** (`chronix/utils/`): Shared helper functions

## License

TBD
