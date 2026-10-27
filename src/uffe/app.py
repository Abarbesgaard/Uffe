"""Uffe terminal UI. Background reads never own keyboard or selection state."""
import asyncio
from dataclasses import replace
import json
from zoneinfo import ZoneInfo

from rich.text import Text
from textual import on, work
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical
from textual.widgets import DataTable, Footer, Input, Select, Static

from .backend import DemoBackend, LiveBackend
from .cli import BANNER
from .config import Config
from .herdr import Agent, HandoffError, Herdr, UncertainDelivery, clipboard
from .model import Evidence, KubeEvent, Note, Resource, capture, format_notes, utcnow
from .screens import (AgentPicker, ConfirmQuit, DetailsScreen, EventsScreen, EvidenceEditor,
                      HelpScreen, LogSelection, LogsScreen, NotesScreen, ReviewRequest)


class LogPane(Static, can_focus=True):
    """Keep keyboard focus on the active logs, including after a modal closes."""


class UffeApp(App):
    TITLE = "Uffe · read-only Kubernetes view"
    CSS = """
    Screen { background: #11131c; color: #cdd6f4; }
    #masthead { height: 7; background: #181d2c; padding: 0 1; }
    #brand { width: 27; color: #89b4fa; text-style: bold; }
    #intro { width: 1fr; padding: 1 1; color: #a6adc8; }
    #filters { height: 3; padding: 0 1; }
    #search { width: 1fr; height: 3; }
    #scope { width: 2fr; content-align: left middle; padding-left: 2; color: #89b4fa; }
    #environments { height: 1fr; }
    .environment { width: 1fr; padding: 0 1; }
    .summary { height: 4; border: round #454d68; padding: 0 1; }
    #level { width: 25; height: 3; }
    .environment.active .summary { border: round #89b4fa; }
    .resources { height: 2fr; min-height: 5; max-height: 18; border: round #454d68; }
    .environment.active .resources { border: round #89b4fa; }
    .environment.logs-active .resources { border: round #454d68; }
    .environment.logs-active .logs { border: round #89b4fa; }
    DataTable { background: #11131c; }
    DataTable > .datatable--header { background: #20273b; color: #89b4fa; text-style: bold; }
    DataTable > .datatable--cursor { background: #334d76; color: #ffffff; text-style: bold; }
    .logs { height: 3fr; min-height: 7; border: round #454d68; padding: 0 1; overflow: hidden; }
    .log-status { height: 1; color: #7f8ba9; }
    #status { height: 2; padding: 0 2; color: #b4befe; background: #181d2c; }
    Footer { background: #20273b; }
    ModalScreen { align: center middle; background: #000000 65%; }
    .dialog { width: 90%; max-width: 130; height: 88%; padding: 1 2; background: #181d2c; border: round #89b4fa; }
    .dialog-title { height: 2; color: #89b4fa; text-style: bold; }
    .dialog Label { height: auto; margin-bottom: 1; }
    .muted { color: #939db8; height: auto; }
    .warning { color: #f9e2af; height: auto; }
    .buttons { height: 3; margin: 1 0; }
    .buttons Button { margin-right: 1; min-width: 10; }
    TextArea { height: 1fr; background: #11131c; border: round #454d68; }
    #instruction { height: 1fr; min-height: 5; }
    #excerpt { height: 2fr; }
    #payload { height: 1fr; }
    #agent-table, #note-table, #event-table, #log-table { height: 1fr; }
    #event-message { height: 6; margin-top: 1; }
    #raw-record { height: 2fr; }
    #confirm { height: 11; max-width: 76; }
    .expanded .resources { display: none; }
    """
    BINDINGS = [
        Binding("up,k", "resource(-1)", "Select pod", priority=True, show=False),
        Binding("down,j", "resource(1)", "Select pod", priority=True, show=False),
        Binding("left", "environment(-1)", "Environment", priority=True, show=False),
        Binding("right", "environment(1)", "Environment", priority=True, show=False),
        Binding("tab,shift+tab", "focus_region", "Pods / logs", priority=True),
        Binding("enter", "details", "Details", priority=True),
        Binding("l", "logs", "Logs", priority=True),
        Binding("a", "all_logs", "All logs", show=False),
        Binding("p", "kind", "Pods/deploys", show=False),
        Binding("slash", "search", "Search"),
        Binding("escape", "clear", "Clear", show=False),
        Binding("0", "namespace(0)", "Scope", show=False),
        Binding("1", "namespace(1)", "Scope", show=False),
        Binding("2", "namespace(2)", "Scope", show=False),
        Binding("3", "namespace(3)", "Scope", show=False),
        Binding("pageup", "history(-5)", "History", priority=True, show=False),
        Binding("pagedown", "history(5)", "History", priority=True, show=False),
        Binding("home", "latest", "Latest", priority=True, show=False),
        Binding("space", "pause", "Pause", show=False),
        Binding("w", "level('warn')", "Warnings", show=False),
        Binding("i", "level('info')", "Information", show=False),
        Binding("x", "level('error')", "Errors", show=False),
        Binding("f", "expand", "Expand logs", show=False),
        Binding("v", "mark_log", "Mark log", show=False),
        Binding("ctrl+l", "inspect_logs", "Log inspector", show=False),
        Binding("e", "events", "Events"),
        Binding("c", "capture", "Capture"),
        Binding("n", "notes", "Notes"),
        Binding("s", "send", "Draft to agent"),
        Binding("r", "refresh", "Refresh", show=False),
        Binding("question_mark", "help", "Help"),
        Binding("q", "quit", "Quit"),
    ]

    def __init__(self, config: Config, demo=False, allow_demo_handoff=False, backend=None, host=None):
        super().__init__()
        self.config = config
        self.demo = demo
        self.allow_handoff = not demo or allow_demo_handoff
        self.backend = backend or (DemoBackend(config) if demo else LiveBackend(config))
        self.host = host or Herdr()
        self.active_env = 0
        self.kind = "Pod"
        self.namespace = ""
        self.query_text = ""
        self.follow_pod = False
        self.focus_region = "pods"
        self.log_cursor = {e.context: 0 for e in config.environments}
        self.marked_logs = {}
        self.paused_before_selection = False
        self.warnings = False
        self.log_level = "all"
        self.paused = False
        self.frozen: dict[str, tuple] = {}
        self.offsets = {e.context: 0 for e in config.environments}
        self.selected: dict[str, str] = {}
        self.selected_objects: dict[str, Resource] = {}
        self.resource_rows: dict[str, tuple[Resource, ...]] = {}
        self.fingerprints: dict[str, tuple] = {}
        self.notes: list[Note] = []
        self.last_agent = ""
        self.uncertain = False
        self.sending = False
        self.status_text = "Read only · capture evidence with c; you review and submit the agent draft."
        self.tz = None if config.timezone == "local" else ZoneInfo(config.timezone)
        self.sent_at = None
        self.paint_timer = None
        self.closing = False

    def compose(self) -> ComposeResult:
        with Horizontal(id="masthead"):
            yield Static(Text(BANNER.strip("\n")), id="brand")
            yield Static("READ-ONLY KUBERNETES VIEW\nObserve → capture → annotate → choose agent → draft\n"
                         "Live logs newest first · Tab moves between pods and logs\n" +
                         ("OFFLINE DEMO · synthetic records" if self.demo else "Your kubeconfig · no cluster mutations"), id="intro")
        with Horizontal(id="filters"):
            yield Input(placeholder="/ search pods and logs", id="search")
            yield Static(id="scope")
            yield Select([("All levels", "all"), ("Information", "info"), ("Warning", "warn"),
                          ("Error", "error"), ("Warning + Error", "problems")], value="all", allow_blank=False, id="level")
        with Horizontal(id="environments"):
            for index, environment in enumerate(self.config.environments):
                with Vertical(classes="environment", id=f"env-{index}"):
                    yield Static(id=f"summary-{index}", classes="summary")
                    yield DataTable(id=f"resources-{index}", classes="resources", cursor_type="row", zebra_stripes=True)
                    yield LogPane(id=f"logs-{index}", classes="logs")
                    yield Static(id=f"log-status-{index}", classes="log-status")
        yield Static(id="status")
        yield Footer()

    async def on_mount(self):
        for i, _ in enumerate(self.config.environments):
            table = self.query_one(f"#resources-{i}", DataTable)
            table.add_columns("Pod / workload", "Namespace", "Ready", "State", "CPU", "Memory", "↻")
            table.border_title = "PODS"
        await self.backend.start()
        self.paint_timer = self.set_interval(.1, self.paint_data)
        self.paint_data()
        self.query_one("#resources-0").focus()

    async def on_unmount(self):
        self.closing = True
        if self.paint_timer:
            self.paint_timer.stop()
        await self.backend.close()

    def check_action(self, action, parameters):
        if len(self.screen_stack) > 1:
            return False
        if isinstance(self.focused, Input) and action not in ("clear", "search", "focus_region"):
            return False
        return True

    @property
    def environment(self):
        return self.config.environments[self.active_env]

    @property
    def resource(self) -> Resource | None:
        return self.selected_objects.get(self.environment.context)

    def resources_for(self, context):
        resources = (r for (c, n), s in self.backend.scopes.items() if c == context and
                     (not self.namespace or n == self.namespace) for r in s.resources)
        return tuple(sorted((r for r in resources if r.kind == self.kind and r.status != "Completed" and
                             self.query_text.lower() in (r.name+" "+r.namespace).lower()),
                            key=lambda r: (r.healthy, r.namespace, r.name)))

    def logs_for(self, context):
        selected = self.selected_objects.get(context)
        records = self.frozen.get(context, ()) if self.paused else self.backend.logs.entries(context=context, level=self.log_level)
        allowed_levels = ("warn", "error") if self.log_level == "problems" else (self.log_level,)
        names = None
        if self.follow_pod and selected:
            names = {selected.name} if selected.kind == "Pod" else {r.name for (c, _), s in self.backend.scopes.items()
                        if c == context for r in s.resources if r.kind == "Pod" and r.workload == selected.name and r.namespace == selected.namespace}
        return tuple(r for r in records if (not self.namespace or r.namespace == self.namespace) and
                     (names is None or r.pod in names and r.namespace == selected.namespace) and
                     (self.log_level == "all" or r.level in allowed_levels) and
                     self.query_text.lower() in (r.message+" "+r.pod+" "+r.namespace).lower())

    def paint_data(self):
        if self.closing or not self.is_mounted or len(self.screen_stack) != 1:
            return
        required = ["#scope", "#status"] + [f"#{name}-{i}" for i in range(len(self.config.environments))
                    for name in ("env", "summary", "resources", "logs", "log-status")]
        if any(not self.screen.query(selector) for selector in required):
            return
        # Painting is read-only with respect to user selection. A vanished UID is
        # retained explicitly until the person chooses another row.
        for i, env in enumerate(self.config.environments):
            panel = self.query_one(f"#env-{i}")
            panel.set_class(i == self.active_env, "active")
            panel.set_class(i == self.active_env and self.focus_region == "logs", "logs-active")
            scopes = [s for (c, n), s in self.backend.scopes.items() if c == env.context and (not self.namespace or n == self.namespace)]
            missing = any(s.observed_at is None for s in scopes)
            failed = any(s.error for s in scopes)
            old = any(s.observed_at and (utcnow()-s.observed_at).total_seconds() > self.config.refresh_seconds*3 for s in scopes)
            status = "STALE / RECONNECTING" if failed or old else "CONNECTING" if missing else "LIVE"
            all_pods = [r for s in scopes for r in s.resources if r.kind == "Pod" and r.status != "Completed"]
            ready = sum(r.healthy for r in all_pods)
            heading = Text(f"{env.name} · {env.context}\n", style="bold #89b4fa")
            heading.append(f"{status}  ·  {ready}/{len(all_pods)} pods ready  ·  ↻ {sum(r.restarts for r in all_pods)}", style="#f9e2af" if status != "LIVE" or ready != len(all_pods) else "#a6e3a1")
            self.query_one(f"#summary-{i}", Static).update(heading)
            rows = self.resources_for(env.context)
            self.resource_rows[env.context] = rows
            if env.context not in self.selected and rows:
                self.selected[env.context] = rows[0].key
                self.selected_objects[env.context] = rows[0]
            selected = self.selected.get(env.context)
            current = next((r for r in rows if r.key == selected), None)
            if current:
                self.selected_objects[env.context] = current
            table = self.query_one(f"#resources-{i}", DataTable)
            table.border_title = f"{'PODS' if self.kind == 'Pod' else 'DEPLOYMENTS'} · {len(rows)}"
            table.border_subtitle = "Selected resource disappeared · ↑/↓ chooses another" if selected and current is None else "↑/↓ select · Enter details · c capture"
            fingerprint = (rows, selected)
            if self.fingerprints.get(env.context) != fingerprint:
                self.fingerprints[env.context] = fingerprint
                with table.prevent(DataTable.RowHighlighted):
                    table.clear()
                    for resource in rows:
                        color = "#a6e3a1" if resource.healthy else "#f9e2af"
                        table.add_row(Text(resource.name), Text(resource.namespace), Text(f"{resource.ready}/{resource.desired}"),
                                      Text(resource.status, style=color), "—" if resource.cpu is None else f"{resource.cpu:.0f}m",
                                      "—" if resource.memory is None else f"{resource.memory:.0f}Mi", str(resource.restarts), key=resource.key)
                    table.cursor_type = "row" if current else "none"
                    if current:
                        table.move_cursor(row=rows.index(current), animate=False, scroll=False)
            entries = self.logs_for(env.context)
            self.log_cursor[env.context] = min(self.log_cursor[env.context], max(0, len(entries)-1))
            self.offsets[env.context] = min(self.offsets[env.context], max(0, len(entries)-1))
            offset = self.offsets[env.context]
            widget = self.query_one(f"#logs-{i}", Static)
            widget.border_title = ("SELECT LOGS · ↑/↓ choose · v mark · s send" if self.focus_region == "logs" and i == self.active_env else
                                   ("PAUSED" if self.paused else "LIVE LOGS") + " · NEWEST FIRST")
            level_label = {"all": "all levels", "info": "information", "warn": "warning", "error": "error", "problems": "warning + error"}[self.log_level]
            widget.border_subtitle = level_label + (" · selected resource" if self.follow_pod else " · scope")
            text = Text()
            capacity = max(2, widget.size.height-3)
            used = 0
            for position, record in enumerate(entries[offset:offset+max(1, capacity//3)], offset):
                clock = record.at.astimezone(self.tz).strftime("%H:%M:%S")
                selected_log = self.focus_region == "logs" and i == self.active_env and position == self.log_cursor[env.context]
                marker = "●" if record.id in self.marked_logs else " "
                prefix = "▶" if selected_log else " "
                style = "bold white on #334d76" if selected_log else "bold #f38ba8" if record.level == "error" else "bold #f9e2af" if record.level == "warn" else "bold #89b4fa"
                text.append(f"{prefix}{marker} {clock} {record.level.upper():5} {record.pod}/{record.container}\n", style=style)
                message = record.message.replace("\n", " ↵ ")
                width = max(10, widget.size.width-4)
                if len(message) > width:
                    message = message[:width-1]+"…"
                text.append("   "+message+"\n", style="white on #334d76" if selected_log else "")
                text.append("\n")
                used += 1
            if not entries:
                text.append("No matching buffered records. New entries appear automatically.\n\n"
                            "Choose a level above · a shows all logs", style="#9399b2")
            widget.update(text)
            stream = self.backend.stream_state.get(env.context, "connecting")
            self.query_one(f"#log-status-{i}", Static).update(Text(f"{stream} · {offset+1 if entries else 0}–{offset+used}/{len(entries)} · PgUp/PgDn history · Home latest"))
        scope_text = self.namespace or "All namespaces"
        self.query_one("#scope", Static).update(Text(f"0 all · 1–3 namespace · {scope_text}"))
        navigation = "LOGS · ↑/↓ select · v mark/unmark · s send · Tab returns to pods" if self.focus_region == "logs" else "PODS · ↑/↓ select · Tab moves down to live logs"
        self.query_one("#status", Static).update(Text(f"{navigation} · {len(self.marked_logs)} marked\n{len(self.notes)} pending note(s) · {self.status_text}"))

    @on(DataTable.RowHighlighted)
    def highlighted(self, event: DataTable.RowHighlighted):
        if not event.data_table.id or not event.data_table.id.startswith("resources-") or len(self.screen_stack) != 1:
            return
        index = int(event.data_table.id.rsplit("-", 1)[1])
        self.active_env = index
        context = self.config.environments[index].context
        resource = next((r for r in self.resource_rows.get(context, ()) if r.key == str(event.row_key.value)), None)
        if resource:
            self.selected[context], self.selected_objects[context] = resource.key, resource

    def action_resource(self, delta: int):
        if self.focus_region == "logs":
            self.move_log(delta)
            return
        context = self.environment.context
        rows = self.resource_rows.get(context, ())
        if rows:
            index = next((i for i, r in enumerate(rows) if r.key == self.selected.get(context)), -1)
            resource = rows[max(0, min(len(rows)-1, index+delta))]
            self.selected[context], self.selected_objects[context] = resource.key, resource
            self.offsets[context] = 0
            self.paint_data()
            table = self.query_one(f"#resources-{self.active_env}", DataTable)
            table.focus()
            table.move_cursor(row=rows.index(resource), animate=False)

    def action_focus_region(self):
        if self.focus_region == "pods":
            self.focus_region = "logs"
            self.paused_before_selection = self.paused
            if not self.paused:
                self.action_pause()
            self.status_text = "Log selection is frozen while new entries continue collecting. v marks; s prepares a draft."
            self.query_one(f"#logs-{self.active_env}").focus()
            self.log_cursor[self.environment.context] = 0
            self.offsets[self.environment.context] = 0
        else:
            self.focus_region = "pods"
            if not self.paused_before_selection:
                self.paused = False
            self.query_one(f"#resources-{self.active_env}").focus()
        self.paint_data()

    def move_log(self, delta: int):
        context = self.environment.context
        entries = self.logs_for(context)
        self.log_cursor[context] = max(0, min(len(entries)-1, self.log_cursor[context]+delta)) if entries else 0
        widget = self.query_one(f"#logs-{self.active_env}", Static)
        visible_count = max(1, (widget.size.height-3)//3)
        if self.log_cursor[context] < self.offsets[context]:
            self.offsets[context] = self.log_cursor[context]
        elif self.log_cursor[context] >= self.offsets[context]+visible_count:
            self.offsets[context] = self.log_cursor[context]-visible_count+1
        self.paint_data()

    def selected_log(self):
        entries = self.logs_for(self.environment.context)
        return entries[min(self.log_cursor[self.environment.context], len(entries)-1)] if entries else None

    def action_mark_log(self):
        if self.focus_region != "logs":
            self.action_focus_region()
        record = self.selected_log()
        if record:
            if record.id in self.marked_logs:
                del self.marked_logs[record.id]
            else:
                self.marked_logs[record.id] = record
            self.paint_data()

    def action_environment(self, delta: int):
        self.active_env = (self.active_env+delta) % len(self.config.environments)
        self.focus_active_region()
        self.paint_data()

    def focus_active_region(self):
        region = "logs" if self.focus_region == "logs" else "resources"
        self.query_one(f"#{region}-{self.active_env}").focus()

    def action_kind(self):
        self.kind = "Deployment" if self.kind == "Pod" else "Pod"
        self.selected.clear()
        self.selected_objects.clear()
        self.fingerprints.clear()
        self.paint_data()

    def action_logs(self):
        self.follow_pod = True
        self.offsets[self.environment.context] = 0
        self.status_text = "Following selected resource · Tab selects live logs · v marks entries · s prepares a draft"
        self.paint_data()

    def action_all_logs(self):
        self.follow_pod = False
        self.set_level("all")
        self.paint_data()

    def action_namespace(self, index: int):
        namespaces = self.config.namespaces
        if index > len(namespaces):
            return
        self.namespace = namespaces[index-1] if index else ""
        self.follow_pod = False
        self.offsets = {e.context: 0 for e in self.config.environments}
        self.log_cursor = {e.context: 0 for e in self.config.environments}
        self.selected.clear()
        self.selected_objects.clear()
        self.paint_data()

    @on(Input.Changed, "#search")
    def search_changed(self, event: Input.Changed):
        self.query_text = event.value
        self.selected.clear()
        self.selected_objects.clear()
        self.paint_data()

    @on(Input.Submitted, "#search")
    def search_submitted(self):
        self.focus_active_region()

    def action_search(self):
        self.query_one("#search").focus()

    def action_clear(self):
        self.query_one("#search", Input).value = ""
        self.follow_pod = False
        self.focus_active_region()

    def action_history(self, amount: int):
        if self.focus_region == "logs":
            self.move_log(amount)
            return
        self.offsets[self.environment.context] = max(0, self.offsets[self.environment.context]+amount)
        self.paint_data()

    def action_latest(self):
        self.focus_region = "pods"
        self.paused = False
        self.offsets = {e.context: 0 for e in self.config.environments}
        self.focus_active_region()
        self.paint_data()

    def action_pause(self):
        self.paused = not self.paused
        if self.paused:
            self.frozen = {e.context: tuple(sorted({r.id: r for level in ("all", "info", "warn", "error", "debug", "trace")
                for r in self.backend.logs.entries(context=e.context, level=level)}.values(), key=lambda r:(r.at, r.id), reverse=True)) for e in self.config.environments}
        self.paint_data()

    def set_level(self, level: str):
        self.log_level = level
        self.warnings = level != "all"
        self.offsets = {e.context: 0 for e in self.config.environments}
        self.log_cursor = {e.context: 0 for e in self.config.environments}
        selector = self.query_one("#level", Select)
        if selector.value != level:
            selector.value = level
        self.paint_data()

    def action_level(self, level: str):
        self.set_level("all" if self.log_level == level else level)

    @on(Select.Changed, "#level")
    def selected_level(self, event: Select.Changed):
        if isinstance(event.value, str):
            self.set_level(event.value)

    def action_levels(self):
        self.action_level("warn")
        self.paint_data()

    def action_expand(self):
        self.query_one("#environments").toggle_class("expanded")

    def action_refresh(self):
        self.backend.refresh.set()
        self.status_text = "Background read refresh requested"

    def evidence(self, resource=None, records=None, source=None, event=None) -> Evidence | None:
        resource = resource or self.resource
        if resource is None:
            self.notify("Select a resource first", severity="warning")
            return None
        if records is None:
            names = {resource.name} if resource.kind == "Pod" else {r.name for s in self.backend.scopes.values()
                for r in s.resources if r.context == resource.context and r.namespace == resource.namespace and r.kind == "Pod" and r.workload == resource.name}
            records = tuple(r for r in self.backend.logs.entries(context=resource.context, namespace=resource.namespace) if r.pod in names)
        result = capture(resource, records, source or resource.kind, self.config.repo_for(resource.namespace, resource.workload), event)
        snapshot = json.loads(result.snapshot_json)
        scope = self.backend.scopes.get((resource.context, resource.namespace))
        snapshot["observed_at"] = scope.observed_at.isoformat() if scope and scope.observed_at else None
        snapshot["connection_state"] = scope.error or "available" if scope else "not observed"
        return replace(result, snapshot_json=json.dumps(snapshot, indent=2, ensure_ascii=False))

    def action_capture(self):
        evidence = self.evidence()
        if evidence:
            self.push_screen(EvidenceEditor(evidence), self.saved_note)

    def saved_note(self, note):
        if note is None:
            return
        self.notes = [note if n.id == note.id else n for n in self.notes] if any(n.id == note.id for n in self.notes) else [*self.notes, note]
        self.uncertain = False
        self.status_text = "Frozen note saved · n reviews notes · s prepares agent handoff"

    def action_details(self):
        if self.focus_region == "logs":
            record = self.selected_log()
            if record:
                self.push_screen(DetailsScreen(record.pod+" / "+record.container, record.raw),
                                 lambda result: self.captured_logs(self.resource, LogSelection((record,))) if result else None)
            return
        resource = self.resource
        if resource:
            evidence = self.evidence(resource)
            self.push_screen(DetailsScreen(resource.name, evidence.snapshot_json),
                             lambda result: self.push_screen(EvidenceEditor(evidence), self.saved_note) if result else None)

    def action_events(self):
        events = tuple(sorted((e for (c, n), s in self.backend.scopes.items() if c == self.environment.context and
                               (not self.namespace or n == self.namespace) for e in s.events), key=lambda e: e.at, reverse=True))
        self.push_screen(EventsScreen(events), self.captured_event)

    def captured_event(self, event: KubeEvent | None):
        if event is None:
            return
        resource = next((r for s in self.backend.scopes.values() for r in s.resources if r.context == event.context and r.namespace == event.namespace and r.uid == event.resource_uid), None)
        if resource is None:
            resource = Resource(event.context, event.namespace, event.resource_uid or event.uid, "Event", event.resource, event.resource, 0, 0, event.reason, 0)
        evidence = self.evidence(resource, source="Event", event=event)
        if evidence:
            self.push_screen(EvidenceEditor(evidence), self.saved_note)

    def action_inspect_logs(self):
        resource = self.resource
        # Freeze records and resource now, not when the dialog eventually closes.
        records = self.logs_for(self.environment.context)
        self.push_screen(LogsScreen(records), lambda chosen: self.captured_logs(resource, chosen))

    def captured_logs(self, resource, selection):
        if not selection:
            return
        records = selection.records
        first = records[0]
        actual = next((r for s in self.backend.scopes.values() for r in s.resources if r.context == first.context and r.namespace == first.namespace and r.kind == "Pod" and r.name == first.pod), None)
        if actual is None:
            actual = resource if resource and resource.name == first.pod else Resource(first.context, first.namespace,
                "not-observed", "Pod", first.pod, first.pod, 0, 0, "Not observed", 0)
        evidence = self.evidence(actual, records=records, source="Logs")
        if evidence:
            def saved(note):
                self.saved_note(note)
                if note and selection.draft:
                    self.marked_logs.clear()
                    self.review_notes()
            self.push_screen(EvidenceEditor(evidence), saved)

    def action_notes(self):
        self.push_screen(NotesScreen(tuple(self.notes)), self.notes_action)

    def notes_action(self, result):
        if not result:
            return
        action, note_id = result
        note = next((n for n in self.notes if n.id == note_id), None)
        if action == "remove":
            self.notes = [n for n in self.notes if n.id != note_id]
        elif action == "edit" and note:
            self.push_screen(EvidenceEditor(note.evidence, note), self.saved_note)
        elif action == "send":
            self.review_notes()

    def action_send(self):
        if self.focus_region == "logs" and (self.marked_logs or not self.notes):
            records = tuple(self.marked_logs.values()) or ((self.selected_log(),) if self.selected_log() else ())
            if records:
                self.captured_logs(self.resource, LogSelection(records, draft=True))
                return
        self.review_notes()

    def review_notes(self):
        if self.sending:
            return
        if not self.notes:
            self.notify("Capture evidence and add a note first", severity="warning")
            return
        batch = tuple(self.notes)
        payload = format_notes(batch)
        if len(payload.encode()) > 131072:
            self.notify("Batch exceeds 128 KiB; edit or remove excerpts before sending", severity="warning")
            return
        self.push_screen(ReviewRequest(payload, self.uncertain), lambda choice: self.reviewed(choice, batch, payload))

    def reviewed(self, choice, batch, payload):
        if choice == "copy":
            self.copy_request(payload)
        elif choice == "choose":
            if not self.allow_handoff:
                self.notify("Demo handoff disabled. Use --allow-demo-handoff to opt in, or copy the request.", severity="warning")
                return
            self.discover_agents(batch, payload)

    @work(exclusive=True, group="discovery")
    async def discover_agents(self, batch, payload):
        self.status_text = "Finding agents in this local herdr session…"
        try:
            agents = await asyncio.to_thread(self.host.discover)
            if not agents:
                raise HandoffError("No agent panes found. Start one beside Uffe, or copy the request.")
            self.push_screen(AgentPicker(agents, self.last_agent), lambda target: self.deliver(target, batch, payload) if target else None)
        except (HandoffError, OSError) as error:
            self.status_text = str(error)
            self.notify(str(error), severity="warning")

    @work(exclusive=True, group="delivery")
    async def deliver(self, target: Agent, batch: tuple[Note, ...], payload: str):
        if self.sending:
            return
        self.sending = True
        self.status_text = "Drafting to "+target.name+"…"
        try:
            result = await asyncio.to_thread(self.host.draft, target, payload)
            ids = {n.id for n in batch}
            self.notes = [n for n in self.notes if n.id not in ids]
            self.uncertain = False
            self.last_agent = target.pane
            self.sent_at = utcnow()
            self.status_text = f"Drafted {len(batch)} note(s) to {result.name}; not submitted." + (" Focus that pane to review." if not result.focused else " Review it in the agent pane.")
            self.notify(self.status_text)
        except UncertainDelivery as error:
            self.uncertain = True
            self.status_text = str(error)
            self.notify(str(error), severity="warning", timeout=12)
        except (HandoffError, OSError) as error:
            self.status_text = str(error)
            self.notify(str(error), severity="error")
        finally:
            self.sending = False

    @work(exclusive=True, group="clipboard")
    async def copy_request(self, payload):
        try:
            await asyncio.to_thread(clipboard, payload)
            self.status_text = "Request copied; notes retained."
        except HandoffError as error:
            self.notify(str(error), severity="warning")

    def action_help(self):
        self.push_screen(HelpScreen())

    def action_quit(self):
        if self.notes:
            self.push_screen(ConfirmQuit(), lambda discard: self.exit() if discard else None)
        else:
            self.closing = True
            self.exit()
