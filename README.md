<div align="center">

# claude-teams

MCP server that implements Claude Code's [agent teams](https://code.claude.com/docs/en/agent-teams) protocol for any MCP client.

</div>



https://github.com/user-attachments/assets/531ada0a-6c36-45cd-8144-a092bb9f9a19



Claude Code has a built-in agent teams feature (shared task lists, inter-agent messaging, tmux-based spawning), but the protocol is internal and tightly coupled to its own tooling. This MCP server reimplements that protocol as a standalone [MCP](https://modelcontextprotocol.io/) server, making it available to any MCP client: [Claude Code](https://docs.anthropic.com/en/docs/claude-code), [OpenCode](https://opencode.ai), or anything else that speaks MCP. Based on a [deep dive into Claude Code's internals](https://gist.github.com/cs50victor/0a7081e6824c135b4bdc28b566e1c719). PRs welcome.

> **This fork** adds **Codex CLI** as a first-class `backend_type` alongside `claude` and `opencode`, plus a web monitor dashboard.

## What's changed in this fork

- `backend_type="codex"` now available in `spawn_teammate`
- Discovers `codex` binary on PATH automatically at startup
- Set `CLAUDE_TEAMS_BACKENDS=opencode,codex` to enable both backends
- Uses `codex exec --full-auto` so teammates start immediately in their tmux pane
- Web monitor: HTTP server with SSE push + dashboard UI

## Install

> **Pin to a release tag** (e.g. `@v0.1.1`), not `main`. There are breaking changes between releases.

Claude Code (`.mcp.json`):

```json
{
  "mcpServers": {
    "claude-teams": {
      "command": "uvx",
      "args": ["--from", "git+https://github.com/MoeJw/claude-code-teams-mcp", "claude-teams"]
    }
  }
}
```

OpenCode (`~/.config/opencode/opencode.json`):

```json
{
  "mcp": {
    "claude-teams": {
      "type": "local",
      "command": [
        "/Users/mohammadjawabreh/.local/bin/uvx",
        "--from",
        "git+https://github.com/MoeJw/claude-code-teams-mcp",
        "claude-teams"
      ],
      "environment": {
        "CLAUDE_TEAMS_BACKENDS": "opencode,codex",
        "OPENCODE_SERVER_URL": "http://localhost:4096"
      },
      "enabled": true
    }
  }
}
```

## Spawning a Codex teammate

```python
spawn_teammate(
    team_name="my-team",
    name="codex-worker",
    prompt="Implement the feature described in task-1",
    backend_type="codex",
)
```

## Requirements

- Python 3.12+
- [tmux](https://github.com/tmux/tmux)
- At least one coding agent on PATH: [Claude Code](https://docs.anthropic.com/en/docs/claude-code) (`claude`), [OpenCode](https://opencode.ai) (`opencode`), or [Codex CLI](https://github.com/openai/codex) (`codex`)
- OpenCode teammates require `OPENCODE_SERVER_URL` and the `claude-teams` MCP connected in that instance

## Configuration

| Variable | Description | Default |
|----------|-------------|---------|
| `CLAUDE_TEAMS_BACKENDS` | Comma-separated enabled backends (`claude`, `opencode`, `codex`) | Auto-detect from connecting client |
| `OPENCODE_SERVER_URL` | OpenCode HTTP API URL (required for opencode teammates) | *(unset)* |
| `USE_TMUX_WINDOWS` | Spawn teammates in tmux windows instead of panes | *(unset)* |

Without `CLAUDE_TEAMS_BACKENDS`, the server auto-detects the connecting client and enables only its backend. Set it explicitly to enable multiple backends:

```json
{
  "mcpServers": {
    "claude-teams": {
      "command": "uvx",
      "args": ["--from", "git+https://github.com/MoeJw/claude-code-teams-mcp", "claude-teams"],
      "env": {
        "CLAUDE_TEAMS_BACKENDS": "claude,opencode,codex",
        "OPENCODE_SERVER_URL": "http://localhost:4096"
      }
    }
  }
}
```

## Tools

| Tool | Description |
|------|-------------|
| `team_create` | Create a new agent team (one per session) |
| `team_delete` | Delete team and all data (fails if teammates active) |
| `spawn_teammate` | Spawn a teammate in tmux |
| `send_message` | Send DMs, broadcasts (lead only), shutdown/plan responses |
| `read_inbox` | Read messages from an agent's inbox |
| `read_config` | Read team config and member list |
| `task_create` | Create a task (auto-incrementing ID) |
| `task_update` | Update task status, owner, dependencies, or metadata |
| `task_list` | List all tasks |
| `task_get` | Get full task details |
| `force_kill_teammate` | Kill a teammate's tmux pane/window and clean up |
| `process_shutdown_approved` | Remove teammate after graceful shutdown |

## Architecture

- **Spawning**: Teammates launch in tmux panes (default) or windows (`USE_TMUX_WINDOWS`). Each gets a unique agent ID and color.
- **Messaging**: JSON inboxes at `~/.claude/teams/<team>/inboxes/`. Lead messages anyone; teammates message only lead.
- **Tasks**: JSON files at `~/.claude/tasks/<team>/`. Status tracking, ownership, and dependency management.
- **Concurrency**: Atomic writes via `tempfile` + `os.replace`. Cross-platform file locks via `filelock`.

```
~/.claude/
├── teams/<team>/
│   ├── config.json
│   └── inboxes/
│       ├── team-lead.json
│       ├── worker-1.json
│       └── .lock
└── tasks/<team>/
    ├── 1.json
    ├── 2.json
    └── .lock
```

## License

[MIT](./LICENSE)
