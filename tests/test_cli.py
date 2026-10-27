import json
from pathlib import Path

from uffe.cli import BANNER, create_workspace, parser


def test_banner_is_uffe_and_demo_is_explicit():
    assert "| |_| |  _|  _|  __/" in BANNER
    assert parser().parse_args(["--demo"]).demo
    assert parser().parse_args(["--banner"]).banner


def test_workspace_launcher_handles_empty_run_output(monkeypatch, capsys):
    calls = []
    def command(args, **kwargs):
        calls.append(args)
        tail = args[1:]
        if tail[:2] == ["workspace", "create"]:
            return json.dumps({"result": {"root_pane": {"pane_id": "w:p1"}, "workspace": {"workspace_id": "w"}}})
        if tail[:2] == ["pane", "split"]:
            return json.dumps({"result": {"pane": {"pane_id": "w:p2"}}})
        if tail[:2] == ["pane", "run"]:
            return ""
        return '{"result":{}}'
    monkeypatch.setattr("uffe.cli.subprocess.check_output", command)
    create_workspace(Path("/tmp/profile with spaces.toml"))
    assert "Uffe workspace w" in capsys.readouterr().out
    assert not any("prompt" in c or "send-text" in c or "start" in c for c in calls)
    run = next(c for c in calls if c[1:3] == ["pane", "run"])
    assert repr(str(Path('/tmp/profile with spaces.toml').resolve())) in run[-1]
