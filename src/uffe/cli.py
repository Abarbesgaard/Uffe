"""Uffe command line and optional explicitly addressed herdr workspace."""
import argparse
import json
import os
from pathlib import Path
import shlex
import shutil
import subprocess
import sys

from . import __version__
from .config import default_path, demo_config, load

BANNER = r""" _   _  __  __
| | | |/ _|/ _| ___
| | | | |_| |_ / _ \
| |_| |  _|  _|  __/
 \___/|_| |_|  \___|
"""


def parser():
    result = argparse.ArgumentParser(description="Uffe · read-only pods, live logs and evidence drafts")
    result.add_argument("command", nargs="?", choices=("doctor", "workspace"))
    result.add_argument("--config", type=Path, default=default_path())
    result.add_argument("--demo", action="store_true", help="Offline synthetic data; no cluster access")
    result.add_argument("--allow-demo-handoff", action="store_true", help="Explicitly enable herdr discovery/drafting in demo")
    result.add_argument("--version", action="version", version=f"Uffe {__version__}")
    result.add_argument("--banner", action="store_true", help="Print the Uffe ASCII art and exit")
    return result


def create_workspace(config: Path, demo: bool = False):
    binary = os.environ.get("HERDR_BIN_PATH", "herdr")

    def call(*args):
        output = subprocess.check_output([binary, *args], text=True, timeout=10)
        return json.loads(output)["result"] if output.strip() else {}

    created = call("workspace", "create", "--cwd", str(Path.cwd()), "--label", "Uffe · MVP", "--no-focus")
    pane = created["root_pane"]["pane_id"]
    workspace = created["workspace"]["workspace_id"]
    split = call("pane", "split", pane, "--direction", "right", "--ratio", "0.72", "--no-focus")
    right = split["pane"]["pane_id"]
    call("pane", "rename", pane, "Uffe · read-only pod view")
    call("pane", "rename", right, "Agent · start your chosen agent here")
    executable = Path(sys.executable).parent / "uffe"
    command = [str(executable)] if executable.exists() else [sys.executable, "-m", "uffe"]
    command += ["--demo"] if demo else ["--config", str(config.resolve())]
    call("pane", "run", pane, shlex.join(command))
    # No agent is chosen, started or messaged automatically.
    call("workspace", "focus", workspace)
    print(f"Uffe workspace {workspace}; start your agent in the right pane or choose an existing agent when drafting.")


def main():
    args = parser().parse_args()
    if args.banner:
        print(BANNER)
        return
    try:
        config = demo_config() if args.demo else load(args.config.expanduser())
        if args.command == "doctor":
            print(BANNER)
            missing = []
            for name in ("kubectl", "stern"):
                path = shutil.which(name)
                print(f"{name}: {path or 'not found'}")
                if not path and not args.demo:
                    missing.append(name)
            print(f"herdr: {shutil.which('herdr') or 'not found (clipboard-only handoff)'}")
            print(f"Configuration: {len(config.environments)} environment(s), {len(config.namespaces)} namespace(s)")
            print("Kubernetes operations: scoped GET reads only. No credential or cluster changes performed.")
            if missing:
                raise ValueError("Missing dependencies: " + ", ".join(missing))
            return
        if args.command == "workspace":
            create_workspace(args.config, args.demo)
            return
        from .app import UffeApp
        UffeApp(config, demo=args.demo, allow_demo_handoff=args.allow_demo_handoff).run()
    except (ValueError, OSError, subprocess.SubprocessError) as error:
        print(f"Uffe: {error}", file=sys.stderr)
        raise SystemExit(2) from error
