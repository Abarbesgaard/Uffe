# Uffe

Read-only Kubernetes monitoring with live, newest-first logs and evidence drafts
for an agent in a separate herdr pane. This is a local terminal application.

## Try the demo

```sh
uv run uffe --demo
```

Demo mode needs no cluster, credentials, kubectl or Stern. Agent discovery and
drafting are disabled in demo mode unless `--allow-demo-handoff` is explicit.

## Install and configure

Python 3.11+, macOS or Linux. Install with `uv tool install .` from this repository,
or `uv tool install git+https://github.com/Abarbesgaard/Uffe@main` from GitHub.
Live mode additionally needs `kubectl`, `stern`, and your kubeconfig's existing
authentication helper on PATH. Uffe never edits kubeconfig or handles passwords.

Copy `examples/config.toml` to `~/.config/uffe/config.toml`, then edit the context
and namespace names. No team-specific names or local checkout paths ship as defaults.

```sh
uffe doctor --config ~/.config/uffe/config.toml
uffe --config ~/.config/uffe/config.toml
uffe workspace --config ~/.config/uffe/config.toml
```

The workspace command creates a separate, explicitly addressed herdr workspace:
Uffe on the left, a normal terminal on the right. Start your chosen agent in
that terminal, or choose an existing agent from another workspace when drafting.
It never relocates, restarts or sends to an existing agent automatically.

## Interaction

- Tab: move between pods and live logs; the blue border and focus label show the active area.
- Up/Down: select a pod or log in the active area; Left/Right: choose environment. Selection follows resource
  identity through refreshes and remains highlighted when viewing logs.
- `l`: filter logs to the selected pod; `a`: show all logs in the current scope.
- `/`: search; `0`/`1`/`2`/`3`: all/first/second/third configured namespace.
- `p`: pods/deployments; Enter: resource details; `e`: events.
- PageUp/PageDown: log history; Space: pause/resume; Home: follow latest.
- Log level selector: All, Information, Warning, Error, or Warning + Error.
  `i`, `w`, `x` toggle Information, Warning, or Error respectively; `a` shows all.
  Warning/error history is retained independently from noisy info traffic.
  `f` expands logs.
- `c`: capture the selected resource with a bounded log excerpt and add a note.
- Tab into live logs, use Up/Down to choose an entry, `v` to mark/unmark several
  entries, then `s` to annotate, preview and choose an agent. The selected line is
  highlighted with ▶ and marked lines have ●. Entries freeze while selecting;
  collection continues in the background. Tab returns to pods and resumes the feed.
- Ctrl+L opens the larger frozen log inspector with the same mark/send controls.
  In that inspector, `c` saves a note without opening the send flow.
- In resource details and events, capture the selected evidence.
- `n`: pending notes, edit/remove them, preview the full request, then choose an agent.
- `s`: preview the request and pick an existing agent. Clipboard copy is also available.
- `?`: help. `q`: quit (pending notes require an explicit discard).

Logs are individual records, newest first, with original timestamps and container
identity. UI filters do not throw away the underlying buffered log records. Log
inspection pauses selection over immutable records; incoming records do not move
the selected excerpt. Each collector has a bounded in-memory buffer.

## Read-only boundary and handoff

Kubernetes connections accept only GET requests on the configured namespace paths
for pods, deployments, events, metrics and pod logs. No shell, edit, delete, scale,
restart, port-forward or credential-changing operations exist in this application.
Metrics and events may be unavailable without blocking pod state or keyboard input.

Captured evidence is frozen at capture time, contains no Pod environment variables
or Secret resources, and is reviewable before delivery. Logs can themselves contain
sensitive data: inspect or edit the excerpt before sharing. Pending notes live only
in memory and survive refresh/reconnect, not process termination.

The agent picker spans the current local herdr session and shows workspace, tab,
working directory and agent state. Drafting revalidates pane/session identity,
uses literal bracketed paste, never submits Enter, and then focuses the target.
Successful delivery clears only that batch. Failures retain it. An ambiguous send
is never automatically retried. The receiving agent keeps its own permissions;
the draft requests read-only investigation unless you explicitly instruct otherwise.

No cloud agent API keys or hosted service are needed. Without herdr, clipboard
export works. Clipboard support uses pbcopy, wl-copy, xclip or xsel when present.

## Development

```sh
uv sync --group dev
uv run pytest
uv run ruff check .
uv build
```

See `MVP.md` for the behavior contract. The herdr plugin is optional and launches
the same CLI. Current compatibility target: macOS/Linux and herdr 0.9.5; Windows
and remote herdr sessions are outside this MVP.

## Herdr plugin

```sh
herdr plugin install Abarbesgaard/Uffe
herdr plugin action invoke open --plugin abarbesgaard.uffe
```

The plugin installs an isolated environment under its own root. Configure
`~/.config/uffe/config.toml` before opening live mode. The action creates a new
workspace explicitly and leaves existing panes alone. The adjacent agent slot
is a normal terminal until you start your preferred agent there.
