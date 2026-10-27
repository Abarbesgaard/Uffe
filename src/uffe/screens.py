"""Explicit evidence capture, review and destination choice."""
from dataclasses import dataclass, replace
import json

from rich.text import Text
from textual import on
from textual.app import ComposeResult
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.screen import ModalScreen
from textual.widgets import Button, DataTable, Label, Static, TextArea

from .herdr import Agent
from .model import Evidence, KubeEvent, LogEntry, Note


class EvidenceEditor(ModalScreen[Note | None]):
    BINDINGS = [("escape", "cancel", "Cancel"), ("ctrl+s", "save", "Save note")]

    def __init__(self, evidence: Evidence, note: Note | None = None):
        super().__init__()
        self.evidence = evidence
        self.note = note

    def compose(self) -> ComposeResult:
        with Vertical(id="editor", classes="dialog"):
            yield Label("CAPTURED EVIDENCE → YOUR INSTRUCTION", classes="dialog-title")
            yield Static(Text(f"{self.evidence.context} / {self.evidence.namespace} / {self.evidence.resource_name}\n"
                              f"Captured {self.evidence.captured_at.isoformat()} · UID {self.evidence.resource_uid}"))
            yield Label("What should the agent investigate or propose?")
            yield TextArea(self.note.instruction if self.note else "", id="instruction", soft_wrap=True)
            yield Label(f"Editable log excerpt · omitted/truncated blocks: {self.evidence.omitted}")
            yield TextArea(self.evidence.excerpt, id="excerpt", soft_wrap=True)
            with Horizontal(classes="buttons"):
                yield Button("Save note", id="save", variant="primary")
                yield Button("Cancel", id="cancel")
            yield Static("Frozen snapshot; live updates cannot change this evidence. Ctrl+S saves. Esc cancels.", classes="muted")

    def on_mount(self):
        self.query_one("#instruction").focus()

    @on(Button.Pressed, "#save")
    def action_save(self):
        instruction = self.query_one("#instruction", TextArea).text.strip()
        excerpt = self.query_one("#excerpt", TextArea).text
        if not instruction:
            self.notify("Add an instruction before saving", severity="warning")
            return
        if len(instruction.encode()) > 8192 or len(excerpt.encode()) > 40000:
            self.notify("Instruction/excerpt too large (8 KiB / 40 KiB limits)", severity="warning")
            return
        evidence = replace(self.evidence, excerpt=excerpt)
        self.dismiss(replace(self.note, instruction=instruction, evidence=evidence) if self.note else Note(instruction, evidence))

    @on(Button.Pressed, "#cancel")
    def action_cancel(self):
        self.dismiss(None)


class ReviewRequest(ModalScreen[str | None]):
    BINDINGS = [("escape", "cancel", "Back")]

    def __init__(self, payload: str, uncertain: bool = False):
        super().__init__()
        self.payload = payload
        self.uncertain = uncertain

    def compose(self) -> ComposeResult:
        with Vertical(classes="dialog", id="request"):
            yield Label("REVIEW EXACT REQUEST", classes="dialog-title")
            if self.uncertain:
                yield Static("Previous delivery was uncertain. Check that agent input first. Choosing again may duplicate the draft.", classes="warning")
            yield Static("Logs are quoted evidence. Check their contents before sharing. The agent receives a draft; you submit it.", classes="muted")
            yield TextArea(self.payload, read_only=True, id="payload", soft_wrap=True)
            with Horizontal(classes="buttons"):
                yield Button("Choose agent…", id="choose", variant="primary")
                yield Button("Copy", id="copy")
                yield Button("Back", id="back")

    @on(Button.Pressed)
    def clicked(self, event: Button.Pressed):
        self.dismiss({"choose": "choose", "copy": "copy"}.get(event.button.id))

    def action_cancel(self):
        self.dismiss(None)


class AgentPicker(ModalScreen[Agent | None]):
    BINDINGS = [("escape", "cancel", "Cancel")]

    def __init__(self, agents: tuple[Agent, ...], last: str = ""):
        super().__init__()
        self.agents, self.last = agents, last

    def compose(self) -> ComposeResult:
        with Vertical(classes="dialog", id="agents"):
            yield Label("DRAFT TO AN AGENT · NO SUBMISSION", classes="dialog-title")
            yield Static("Choose an existing agent. Workspace, tab and repository identify the destination.", classes="muted")
            yield DataTable(id="agent-table", cursor_type="row", zebra_stripes=True)
            with Horizontal(classes="buttons"):
                yield Button("Draft into selected agent", id="draft", variant="primary", disabled=not self.agents)
                yield Button("Cancel", id="cancel")

    def on_mount(self):
        table = self.query_one(DataTable)
        table.add_columns("Agent", "State", "Workspace", "Tab", "Working directory")
        for agent in self.agents:
            table.add_row(*(Text(v) for v in (agent.name + (" · last used" if agent.pane == self.last else ""), agent.state,
                agent.workspace_name, agent.tab, agent.cwd)), key=agent.pane)
        if self.agents:
            table.move_cursor(row=next((i for i, a in enumerate(self.agents) if a.pane == self.last), 0))
        table.focus()

    @on(DataTable.RowSelected)
    @on(Button.Pressed, "#draft")
    def choose(self):
        table = self.query_one(DataTable)
        self.dismiss(self.agents[table.cursor_row] if self.agents else None)

    @on(Button.Pressed, "#cancel")
    def action_cancel(self):
        self.dismiss(None)


class NotesScreen(ModalScreen[tuple[str, str] | None]):
    BINDINGS = [("escape", "cancel", "Back")]

    def __init__(self, notes: tuple[Note, ...]):
        super().__init__()
        self.notes = notes

    def compose(self) -> ComposeResult:
        with Vertical(classes="dialog", id="notes"):
            yield Label(f"PENDING NOTES · {len(self.notes)}", classes="dialog-title")
            yield DataTable(id="note-table", cursor_type="row", zebra_stripes=True)
            with Horizontal(classes="buttons"):
                yield Button("Edit", id="edit", disabled=not self.notes)
                yield Button("Remove", id="remove", disabled=not self.notes)
                yield Button("Review & send…", id="send", variant="primary", disabled=not self.notes)
                yield Button("Back", id="back")

    def on_mount(self):
        table = self.query_one(DataTable)
        table.add_columns("Source", "Environment / resource", "Your instruction")
        for note in self.notes:
            e = note.evidence
            table.add_row(Text(e.source), Text(e.context+" / "+e.resource_name), Text(note.instruction.splitlines()[0]), key=note.id)
        table.focus()

    @on(Button.Pressed)
    def clicked(self, event: Button.Pressed):
        action = event.button.id or "back"
        index = self.query_one(DataTable).cursor_row
        self.dismiss((action, self.notes[index].id if self.notes else "") if action != "back" else None)

    @on(DataTable.RowSelected)
    def selected(self):
        if self.notes:
            self.dismiss(("edit", self.notes[self.query_one(DataTable).cursor_row].id))

    def action_cancel(self):
        self.dismiss(None)


class DetailsScreen(ModalScreen[str | None]):
    BINDINGS = [("escape", "cancel", "Back"), ("c", "capture", "Capture")]

    def __init__(self, title: str, value: str):
        super().__init__()
        self.title_text, self.value = title, value

    def compose(self) -> ComposeResult:
        with Vertical(classes="dialog", id="details"):
            yield Label(Text(self.title_text), classes="dialog-title")
            yield TextArea(self.value, read_only=True, soft_wrap=True)
            with Horizontal(classes="buttons"):
                yield Button("Capture & annotate", id="capture", variant="primary")
                yield Button("Back", id="back")

    @on(Button.Pressed, "#capture")
    def action_capture(self):
        self.dismiss("capture")

    @on(Button.Pressed, "#back")
    def action_cancel(self):
        self.dismiss(None)


class EventsScreen(ModalScreen[KubeEvent | None]):
    BINDINGS = [("escape", "cancel", "Back")]

    def __init__(self, events: tuple[KubeEvent, ...]):
        super().__init__()
        self.events = events

    def compose(self) -> ComposeResult:
        with Vertical(classes="dialog", id="events"):
            yield Label("KUBERNETES EVENTS · FROZEN SELECTION", classes="dialog-title")
            yield DataTable(id="event-table", cursor_type="row", zebra_stripes=True)
            yield Static(id="event-message")
            with Horizontal(classes="buttons"):
                yield Button("Capture event", id="capture", variant="primary", disabled=not self.events)
                yield Button("Back", id="back")

    def on_mount(self):
        table = self.query_one(DataTable)
        table.add_columns("Time (UTC)", "Type", "Reason", "Object", "Count")
        for i, event in enumerate(self.events):
            table.add_row(Text(event.at.isoformat()), Text(event.level), Text(event.reason), Text(event.resource), event.count, key=str(i))
        table.focus()

    @on(DataTable.RowHighlighted)
    def highlight(self):
        if self.events:
            self.query_one("#event-message", Static).update(Text(self.events[self.query_one(DataTable).cursor_row].message))

    @on(DataTable.RowSelected)
    @on(Button.Pressed, "#capture")
    def capture(self):
        self.dismiss(self.events[self.query_one(DataTable).cursor_row] if self.events else None)

    @on(Button.Pressed, "#back")
    def action_cancel(self):
        self.dismiss(None)


@dataclass(frozen=True)
class LogSelection:
    records: tuple[LogEntry, ...]
    draft: bool = False


class LogsScreen(ModalScreen[LogSelection | None]):
    BINDINGS = [("escape", "cancel", "Back"), ("c", "capture", "Capture selected"),
                ("v", "mark", "Mark / unmark"), ("m", "mark", "Mark / unmark"), ("s", "send", "Draft selected")]

    def __init__(self, records: tuple[LogEntry, ...]):
        super().__init__()
        self.records = records[:500]
        self.marked: set[int] = set()

    def compose(self) -> ComposeResult:
        with Vertical(classes="dialog", id="logs-inspector"):
            yield Label("LOG INSPECTOR · ↑/↓ SELECT · v MARK / UNMARK · s SEND SELECTED", classes="dialog-title")
            yield Static("0 marked · the cursor entry is used when none are marked", id="marked-status", classes="muted")
            yield DataTable(id="log-table", cursor_type="row", zebra_stripes=True)
            yield TextArea("", read_only=True, id="raw-record", soft_wrap=True)
            with Horizontal(classes="buttons"):
                yield Button("Send selected…", id="send", variant="primary", disabled=not self.records)
                yield Button("Save note", id="capture", disabled=not self.records)
                yield Button("Back", id="back")

    def on_mount(self):
        table = self.query_one(DataTable)
        table.add_columns("Mark", "Time (UTC)", "Level", "Pod / container", "Message")
        for record in self.records:
            table.add_row("", Text(record.at.isoformat()), Text(record.level), Text(record.pod+"/"+record.container), Text(record.message[:160]), key=str(record.id))
        table.focus()

    @on(DataTable.RowHighlighted)
    def highlight(self):
        if self.records:
            record = self.records[self.query_one(DataTable).cursor_row]
            try:
                raw = json.dumps(json.loads(record.raw), indent=2, ensure_ascii=False)
            except ValueError:
                raw = record.raw
            self.query_one("#raw-record", TextArea).load_text(raw)

    def action_mark(self):
        table = self.query_one(DataTable)
        if self.records:
            record = self.records[table.cursor_row]
            if record.id in self.marked:
                self.marked.remove(record.id)
            else:
                self.marked.add(record.id)
            table.update_cell(str(record.id), table.columns[next(iter(table.columns))].key, "●" if record.id in self.marked else "")
            self.query_one("#marked-status", Static).update(f"{len(self.marked)} marked · records are frozen while you select")

    def selection(self):
        return tuple(r for r in self.records if r.id in self.marked) if self.marked else (self.records[self.query_one(DataTable).cursor_row],)

    @on(Button.Pressed, "#capture")
    def action_capture(self):
        if self.records:
            self.dismiss(LogSelection(self.selection()))

    @on(Button.Pressed, "#send")
    def action_send(self):
        if self.records:
            self.dismiss(LogSelection(self.selection(), draft=True))

    @on(Button.Pressed, "#back")
    def action_cancel(self):
        self.dismiss(None)


class HelpScreen(ModalScreen[None]):
    BINDINGS = [("escape", "close", "Back"), ("question_mark", "close", "Back")]

    def compose(self) -> ComposeResult:
        with Vertical(classes="dialog"):
            yield Label("UFFE · READ-ONLY CONTROLS", classes="dialog-title")
            with VerticalScroll():
                yield Static("↑/↓  Select resource (selection remains visible)\n←/→  Choose environment\n"
                    "Enter  Resource details\nl  Filter logs to selected pod/workload\na  All logs in scope\n"
                    "p  Pods / deployments\n0–3  Namespace filter\n/  Search\nEsc  Clear filters / close dialog\n"
                    "PageUp/PageDown  Log history\nSpace  Pause/resume\nHome  Follow latest\n"
                    "i / w / x  Information / Warning / Error (repeat to return to All)\nf  Expand logs\n"
                    "v  Frozen log inspector; v marks/unmarks entries, s drafts selected entries\n"
                    "e  Kubernetes events\nc  Capture resource and annotate\nn  Pending notes\n"
                    "s  Preview request, choose agent, draft without submission\nr  Refresh health (read-only)\n"
                    "?  Help\nq  Quit\n\n"
                    "Evidence stays frozen through refresh and pod replacement. Notes are in memory only.\n"
                    "The receiving agent runs in its own pane with its own permissions. You review and submit.\n"
                    "Uffe has no cluster mutation, shell, restart, scale or port-forward actions.")
            yield Button("Back", id="back")

    @on(Button.Pressed)
    def action_close(self):
        self.dismiss(None)


class ConfirmQuit(ModalScreen[bool]):
    BINDINGS = [("escape", "cancel", "Keep working")]

    def compose(self) -> ComposeResult:
        with Vertical(classes="dialog", id="confirm"):
            yield Label("Unsent notes exist in memory. Quit and discard them?")
            with Horizontal(classes="buttons"):
                yield Button("Keep working", id="keep", variant="primary")
                yield Button("Quit & discard", id="discard", variant="error")

    @on(Button.Pressed)
    def clicked(self, event: Button.Pressed):
        self.dismiss(event.button.id == "discard")

    def action_cancel(self):
        self.dismiss(False)
