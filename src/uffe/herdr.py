"""Agent discovery and explicit draft-only handoff; no prompt submission."""
from dataclasses import dataclass
import json
import os
import shutil
import subprocess

from .model import clean


class HandoffError(RuntimeError):
    pass


class UncertainDelivery(HandoffError):
    pass


@dataclass(frozen=True)
class Agent:
    pane: str
    name: str
    kind: str
    state: str
    workspace: str
    workspace_name: str
    tab: str
    cwd: str
    session: str


@dataclass(frozen=True)
class Delivery:
    name: str
    focused: bool


def bracketed(text: str) -> str:
    return "\x1b[200~" + clean(text) + "\x1b[201~"


class Herdr:
    def __init__(self, runner=None):
        self.runner = runner or subprocess.run
        self.binary = os.environ.get("HERDR_BIN_PATH", "herdr")

    def call(self, args: list[str], timeout: float = 4) -> str:
        result = self.runner([self.binary, *args], capture_output=True, text=True, timeout=timeout)
        if result.returncode:
            raise HandoffError("herdr refused the request; the draft is still here")
        return result.stdout

    def raw_agents(self) -> list:
        try:
            return json.loads(self.call(["agent", "list"]))["result"]["agents"]
        except (OSError, ValueError, KeyError, subprocess.TimeoutExpired) as error:
            raise HandoffError("Agent discovery unavailable; copy the request instead") from error

    def discover(self) -> tuple[Agent, ...]:
        agents = self.raw_agents()
        workspaces, tabs = {}, {}
        for command, key, target in ((["workspace", "list"], "workspaces", workspaces), (["tab", "list"], "tabs", tabs)):
            try:
                items = json.loads(self.call(command))["result"][key]
                target.update({i["workspace_id" if key == "workspaces" else "tab_id"]: i.get("label", "") for i in items})
            except (OSError, ValueError, KeyError, HandoffError, subprocess.TimeoutExpired):
                pass
        result = []
        for item in agents:
            if not item.get("agent") or item.get("pane_id") == os.environ.get("HERDR_PANE_ID"):
                continue
            try:
                result.append(Agent(item["pane_id"], item.get("name") or item.get("display_agent") or item["agent"],
                    item["agent"], item.get("agent_status", "unknown"), item["workspace_id"],
                    workspaces.get(item["workspace_id"], item["workspace_id"]), tabs.get(item["tab_id"], item["tab_id"]),
                    item.get("cwd", ""), json.dumps(item.get("agent_session"), sort_keys=True)))
            except KeyError:
                continue
        current = os.environ.get("HERDR_WORKSPACE_ID")
        return tuple(sorted(result, key=lambda a: a.workspace != current))

    def draft(self, target: Agent, text: str) -> Delivery:
        current = next((a for a in self.raw_agents() if a.get("pane_id") == target.pane), None)
        if (current is None or current.get("agent") != target.kind or current.get("workspace_id") != target.workspace or
                json.dumps(current.get("agent_session"), sort_keys=True) != target.session or current.get("cwd", "") != target.cwd):
            raise HandoffError("The selected agent changed or closed; choose it again. Notes kept.")
        try:
            self.call(["pane", "send-text", target.pane, bracketed(text)], timeout=5)
        except subprocess.TimeoutExpired as error:
            raise UncertainDelivery("Delivery is uncertain. Check the agent input before retrying; notes kept.") from error
        except OSError as error:
            raise HandoffError("Could not reach herdr; notes kept") from error
        focused = True
        try:
            self.call(["agent", "focus", target.pane])
        except (OSError, HandoffError, subprocess.TimeoutExpired):
            focused = False
        return Delivery(target.name, focused)


def clipboard(text: str):
    for binary, args in (("pbcopy", []), ("wl-copy", []), ("xclip", ["-selection", "clipboard"]), ("xsel", ["--clipboard", "--input"])):
        executable = shutil.which(binary)
        if executable:
            try:
                subprocess.run([executable, *args], input=text, text=True, check=True, timeout=5)
                return
            except (OSError, subprocess.SubprocessError) as error:
                raise HandoffError("Clipboard failed; notes kept") from error
    raise HandoffError("No clipboard tool found; install pbcopy, wl-copy, xclip or xsel")
