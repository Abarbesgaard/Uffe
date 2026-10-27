from dataclasses import replace
from datetime import timedelta
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from uffe.backend import DemoBackend, ReadClient, ReadError
from uffe.config import Environment, demo_config, load
from uffe.herdr import Agent, HandoffError, Herdr, UncertainDelivery, bracketed
from uffe.model import (LogBuffer, Note, Resource, capture, clean, cpu, format_notes,
                        matches, memory, normalize, utcnow, with_metrics)


@pytest.fixture
def resource():
    return Resource("prod", "payments", "original-uid", "Pod", "worker-abc", "worker", 1, 1, "Running", 2,
                    details_json=json.dumps({"name": "worker-abc", "containers": [{"name": "app", "image": "worker:v1"}]}))


def line(message="hello", stamp="2026-10-26T23:59:59Z", level="warning", namespace="payments"):
    return json.dumps({"namespace": namespace, "podName": "worker-abc", "containerName": "app",
                       "timestamp": stamp, "message": json.dumps({"level": level, "message": message, "attempt": 4})})


def test_config_portable_and_validated(tmp_path):
    path = tmp_path/"config.toml"
    path.write_text('schema_version=1\n[[environments]]\nname="Prod"\ncontext="my-context"\nnamespaces=["payments"]\n')
    config = load(path)
    assert config.environments[0].context == "my-context"
    path.write_text(path.read_text().replace('"payments"', '"../escape"'))
    with pytest.raises(ValueError, match="namespace"):
        load(path)


def test_log_dates_order_duplicates_and_bounded_memory():
    buffer = LogBuffer(3)
    buffer.append("prod", line("old"))
    buffer.append("prod", line("new", "2026-10-27T00:00:01Z"))
    buffer.append("prod", line("new", "2026-10-27T00:00:01Z"))
    assert [r.message for r in buffer.entries()] == ["new", "new", "old"]
    buffer.append("prod", line("later", "2026-10-27T00:00:02Z", "info"))
    assert len(buffer.records) == 3 and buffer.dropped == 1
    assert len(buffer.entries(warnings=True)) == 3  # independently retained warning history
    assert len(buffer.entries(warnings=False)) == 3
    assert buffer.entries()[0].at.date().isoformat() == "2026-10-27"


def test_stern_plain_and_timestamp_prefix():
    buffer = LogBuffer()
    record = buffer.append("prod", json.dumps({"podName": "worker", "namespace": "payments", "message": "2026-10-27T01:02:03.123456789Z plain info"}))
    assert record.message == "plain info" and record.level == "info"
    assert record.at.hour == 1
    assert buffer.append("prod", "non json diagnostic") is None


def test_info_flood_does_not_erase_warning_history():
    buffer = LogBuffer(3)
    buffer.append('prod', line('keep warning', level='warn'))
    buffer.append('prod', line('keep error', level='error'))
    for index in range(10):
        buffer.append('prod', line(str(index), level='info'))
    assert {r.message for r in buffer.entries(warnings=True)} == {'keep warning', 'keep error'}


def test_capture_immutable_and_capped(resource):
    buffer = LogBuffer()
    for i in range(60):
        buffer.append("prod", line(str(i)))
    evidence = capture(resource, buffer.entries())
    before = evidence.excerpt
    buffer.append("prod", line("not in evidence"))
    changed = replace(resource, uid="replacement", details_json='{"image":"v2"}')
    assert evidence.resource_uid != changed.uid
    assert evidence.excerpt == before and "not in evidence" not in before
    assert evidence.omitted == 10
    large = capture(resource, (replace(buffer.entries()[0], raw="x"*40000),))
    assert large.omitted and "truncated" in large.excerpt


def test_payload_separates_instruction_and_untrusted_data(resource):
    evidence = capture(resource, ())
    payload = format_notes([Note("Explain why this retries", evidence)])
    assert "USER REQUEST:" in payload and "quoted data" in payload
    assert "original-uid" in payload
    assert "Do not change Kubernetes resources" in payload


def test_controls_cannot_break_bracketed_paste():
    text = "a\x1b[201~\x1b[31mRED\x00\r\x1b]0;title\x07\nb"
    result = bracketed(text)
    assert result.startswith("\x1b[200~") and result.endswith("\x1b[201~")
    assert result.count("\x1b[201~") == 1
    assert clean(text) == "aRED\nb"


def test_resource_normalization_does_not_export_env():
    backend = DemoBackend(demo_config())
    resource = next(iter(backend.scopes.values())).resources[0]
    details = json.loads(resource.details_json)
    assert details["containers"][0]["image"]
    assert "env" not in details and "annotations" not in details


def test_selectors_and_metrics():
    assert matches({"app": "x", "tier": "api"}, {"matchLabels": {"app": "x"}, "matchExpressions": [{"key": "tier", "operator": "In", "values": ["api"]}]})
    assert not matches({"app": "x"}, {"matchExpressions": [{"key": "tier", "operator": "Exists"}]})
    assert not matches({"app": "x"}, {})
    assert cpu("100000000n") == 100 and memory("1Gi") == 1024
    backend = DemoBackend(demo_config())
    rows = next(iter(backend.scopes.values())).resources
    pod = next(r for r in rows if r.kind == "Pod")
    result = with_metrics(rows, [{"metadata": {"name": pod.name}, "containers": [{"usage": {"cpu": "20m", "memory": "40Mi"}}]}])
    assert all(r.cpu == 20 and r.memory == 40 for r in result)
    assert all(r.cpu is None for r in with_metrics(rows, []))


def test_crashloop_and_completed_status():
    pod = {"metadata": {"name": "p", "uid": "uid"}, "spec": {"containers": [{"name": "c"}]}, "status": {"phase": "Running", "containerStatuses": [{"name": "c", "ready": False, "state": {"waiting": {"reason": "CrashLoopBackOff"}}}]}}
    rows = normalize("ctx", "ns", [pod], [])
    assert rows[0].status == "CrashLoopBackOff" and not rows[0].healthy
    pod["status"] = {"phase": "Succeeded"}
    assert normalize("ctx", "ns", [pod], [])[0].status == "Completed"


def test_proxy_path_allowlist_rejects_sensitive_and_mutating_subresources():
    client = ReadClient(Environment("p", "prod", ("payments",)))
    try:
        assert client.allowed("/api/v1/namespaces/payments/pods")
        for path in ("/api/v1/namespaces/other/pods", "/api/v1/namespaces/payments/secrets", "/api/v1/namespaces/payments/pods/x/exec", "/api/v1/namespaces/payments/pods/../secrets"):
            with pytest.raises(ReadError):
                client.get(path)
        assert client.process is None
    finally:
        client.close()
    assert not client.directory.exists()


def test_client_reuses_process_and_only_calls_get(monkeypatch):
    client = ReadClient(Environment("p", "prod", ("payments",)))
    calls = []
    monkeypatch.setattr(client, "start", lambda: calls.append("start"))
    class Connection:
        def __init__(self, *args): pass
        def request(self, method, path): calls.append((method, path))
        def getresponse(self): return SimpleNamespace(status=200, read=lambda: b'{"items":[]}')
        def close(self): pass
    monkeypatch.setattr("uffe.backend.UnixHTTP", Connection)
    try:
        client.get("/api/v1/namespaces/payments/pods")
        client.get("/api/v1/namespaces/payments/pods")
        assert all(c[0] == "GET" for c in calls if isinstance(c, tuple))
    finally:
        client.close()


def agent():
    return Agent("w2:p1", "chosen", "codex", "idle", "w2", "work", "backend", "/repo", "null")


class FakeHerdr:
    def __init__(self, failure=""):
        self.calls = []
        self.failure = failure

    def __call__(self, command, **kwargs):
        self.calls.append(command)
        args = command[1:]
        if args == ["agent", "list"]:
            value = {"agent": "codex", "agent_status": "idle", "pane_id": "w2:p1", "workspace_id": "w2", "tab_id": "t1", "cwd": "/repo"}
            if self.failure == "closed":
                return SimpleNamespace(returncode=0, stdout=json.dumps({"result": {"agents": []}}))
            if self.failure == "changed":
                value["agent_session"] = {"value": "new"}
            return SimpleNamespace(returncode=0, stdout=json.dumps({"result": {"agents": [value]}}))
        if args[:2] == ["pane", "send-text"] and self.failure == "uncertain":
            import subprocess
            raise subprocess.TimeoutExpired(command, 5)
        if args[:2] == ["pane", "send-text"] and self.failure == "refused":
            return SimpleNamespace(returncode=1, stdout="")
        if args[:2] == ["agent", "focus"] and self.failure == "focus":
            return SimpleNamespace(returncode=1, stdout="")
        if args == ["workspace", "list"]:
            return SimpleNamespace(returncode=0, stdout='{"result":{"workspaces":[{"workspace_id":"w2","label":"work"}]}}')
        if args == ["tab", "list"]:
            return SimpleNamespace(returncode=0, stdout='{"result":{"tabs":[{"tab_id":"t1","label":"backend"}]}}')
        return SimpleNamespace(returncode=0, stdout="")


def test_agent_discovery_and_draft_never_submits():
    fake = FakeHerdr()
    host = Herdr(fake)
    target = host.discover()[0]
    result = host.draft(target, "request\nline two")
    assert result.focused
    sends = [c for c in fake.calls if c[1:3] == ["pane", "send-text"]]
    assert len(sends) == 1 and sends[0][-1] == bracketed("request\nline two")
    assert not any("prompt" in c or "send-keys" in c or "run" in c for c in fake.calls)


@pytest.mark.parametrize("reason", ["closed", "changed", "refused"])
def test_bad_destination_refuses(reason):
    fake = FakeHerdr(reason)
    with pytest.raises(HandoffError):
        Herdr(fake).draft(agent(), "request")


def test_focus_failure_is_not_delivery_failure():
    fake = FakeHerdr("focus")
    result = Herdr(fake).draft(agent(), "request")
    assert not result.focused
    assert sum(c[1:3] == ["pane", "send-text"] for c in fake.calls) == 1


def test_ambiguous_delivery_is_never_retried():
    fake = FakeHerdr("uncertain")
    with pytest.raises(UncertainDelivery):
        Herdr(fake).draft(agent(), "request")
    assert sum(c[1:3] == ["pane", "send-text"] for c in fake.calls) == 1


def test_no_user_machine_paths_in_package():
    root = Path(__file__).parents[1]/"src/uffe"
    for file in root.glob("*.py"):
        assert "/Users/" not in file.read_text()
        assert "/opt/homebrew/" not in file.read_text()


def test_old_and_new_data_never_share_uid(resource):
    newer = replace(resource, uid="new-uid")
    assert newer.key != resource.key
    assert capture(resource, ()).resource_uid == "original-uid"
    assert utcnow() - timedelta(seconds=1) < capture(resource, ()).captured_at
