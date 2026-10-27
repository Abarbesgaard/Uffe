"""Pure unit tests: no UI, Kubernetes, real herdr, clipboard or child processes."""
from dataclasses import FrozenInstanceError, replace
from datetime import datetime, timezone
import json
import subprocess
from types import SimpleNamespace

import pytest

from uffe.config import default_path, load
from uffe.herdr import HandoffError, Herdr, clipboard
from uffe.model import LogBuffer, Resource, capture, clean, matches, timestamp, with_metrics
from test_core import FakeHerdr, agent, line


VALID_ENV = '\n[[environments]]\nname="Prod"\ncontext="production"\nnamespaces=["payments"]\n'


@pytest.mark.parametrize('setting', [
    'schema_version=2', 'timezone="NoSuch/Zone"', 'refresh_seconds=4',
    'refresh_seconds=301', 'refresh_seconds="20"', 'log_buffer=99',
    'log_buffer=50001', 'log_history="yesterday"', 'log_history=30',
    'exclude_containers="proxy"', 'exclude_containers=[1]', 'exclude_containers=["["]',
])
def test_invalid_config_settings_fail_with_value_error(tmp_path, setting):
    path = tmp_path/'config.toml'
    path.write_text(setting + VALID_ENV)
    with pytest.raises(ValueError):
        load(path)


@pytest.mark.parametrize('environment', [
    '', '[[environments]]\nname=""\ncontext="prod"\nnamespaces=["payments"]',
    '[[environments]]\nname="Prod"\ncontext=""\nnamespaces=["payments"]',
    '[[environments]]\nname="Prod"\ncontext="prod"\nnamespaces=[]',
    VALID_ENV + VALID_ENV,
    VALID_ENV + VALID_ENV.replace('name="Prod"', 'name="Other"'),
    VALID_ENV + VALID_ENV.replace('context="production"', 'context="other"'),
])
def test_missing_and_duplicate_environments_are_rejected(tmp_path, environment):
    path = tmp_path/'config.toml'
    path.write_text(environment)
    with pytest.raises(ValueError):
        load(path)


def test_config_deduplicates_namespaces_and_resolves_repository(tmp_path, monkeypatch):
    monkeypatch.setenv('HOME', str(tmp_path))
    monkeypatch.setenv('XDG_CONFIG_HOME', str(tmp_path/'config'))
    assert default_path() == tmp_path/'config/uffe/config.toml'
    path = tmp_path/'profile.toml'
    path.write_text(VALID_ENV.replace('["payments"]', '["payments","payments","support"]') +
                    '\n[[repositories]]\nnamespace="payments"\nworkload="worker"\npath="~/worker"\n')
    config = load(path)
    assert config.namespaces == ('payments', 'support')
    assert config.repo_for('payments', 'worker') == str(tmp_path/'worker')
    assert config.repo_for('support', 'worker') == ''


@pytest.mark.parametrize(('value', 'expected'), [
    ('2026-10-27T10:00:00+02:00', '2026-10-27T08:00:00+00:00'),
    ('2026-10-27T08:00:00Z', '2026-10-27T08:00:00+00:00'),
    ('2026-10-27T08:00:00', '2026-10-27T08:00:00+00:00'),
])
def test_timestamp_normalizes_to_utc(value, expected):
    assert timestamp(value).isoformat() == expected


@pytest.mark.parametrize('value', [None, '', 'invalid', '2026-99-99'])
def test_invalid_timestamp_uses_aware_current_time(monkeypatch, value):
    now = datetime(2026, 10, 27, tzinfo=timezone.utc)
    monkeypatch.setattr('uffe.model.utcnow', lambda: now)
    assert timestamp(value) == now


@pytest.mark.parametrize(('level', 'expected'), [
    ('Information', 'info'), ('WARNING', 'warn'), ('wrn', 'warn'),
    ('err', 'error'), ('fatal', 'error'), ('critical', 'error'),
    ('debug', 'debug'), ('trace', 'trace'),
])
def test_structured_log_level_aliases(level, expected):
    record = LogBuffer().append('prod', line('message', level=level))
    assert record.level == expected and record.message == 'message'


@pytest.mark.parametrize('raw', ['not json', 'null', '[]', '42', '"text"', '{'])
def test_malformed_collector_records_are_ignored(raw):
    buffer = LogBuffer(5)
    assert buffer.append('prod', raw) is None
    assert not buffer.records and buffer.dropped == 0


@pytest.mark.parametrize(('message', 'level'), [
    ('panic in worker', 'error'), ('exception raised', 'error'),
    ('WARNING retry', 'warn'), ('ordinary request', 'info'),
])
def test_plain_log_severity_inference(message, level):
    record = LogBuffer().append('prod', json.dumps({'message': message}))
    assert record.level == level and record.message == message


def test_log_filters_compose_without_mutating_history():
    buffer = LogBuffer(10)
    expected = buffer.append('prod', line('Needle', level='warning'))
    buffer.append('uat', line('Needle', level='warning'))
    buffer.append('prod', line('Needle', level='error', namespace='support'))
    buffer.append('prod', line('other', level='info'))
    assert buffer.entries(context='prod', namespace='payments', pod='worker-abc',
                          query='NEEDLE', level='warn') == (expected,)
    assert buffer.entries(context='missing') == ()
    assert buffer.entries(pod='missing') == ()
    assert len(buffer.entries(level='problems')) == 3
    assert len(buffer.entries()) == 4
    with pytest.raises(FrozenInstanceError):
        expected.message = 'changed'


@pytest.mark.parametrize('control', ['\x1b[31m', '\x1b[2J', '\x1b]0;title\x07', '\x00', '\x08'])
def test_terminal_controls_are_removed_but_unicode_and_newlines_survive(control):
    assert clean('æøå\n' + control + 'message\t✓') == 'æøå\nmessage\t✓'


def test_evidence_caps_multibyte_utf8_and_keeps_record_identity():
    resource = Resource('prod', 'payments', 'uid', 'Pod', 'worker-abc', 'worker', 1, 1, 'Running', 0)
    record = LogBuffer().append('prod', line('æ' * 30000))
    evidence = capture(resource, (record,), source='Logs', repository='/example/repo')
    assert '\ufffd' not in evidence.excerpt
    assert evidence.omitted == 1 and 'truncated at 32 KiB' in evidence.excerpt
    assert 'prod/payments/worker-abc/app' in evidence.excerpt
    assert evidence.resource_uid == 'uid' and evidence.repository == '/example/repo'
    assert len(evidence.excerpt.encode()) <= 32768 + 50


def test_partial_metrics_remain_unknown_instead_of_underreporting_deployment():
    first = Resource('prod', 'payments', 'a', 'Pod', 'pod-a', 'worker', 1, 1, 'Running', 0)
    second = replace(first, uid='b', name='pod-b')
    deployment = replace(first, uid='d', kind='Deployment', name='worker', ready=2, desired=2)
    values = [{'metadata': {'name': 'pod-a'}, 'containers': [
        {'usage': {'cpu': '100m', 'memory': '64Mi'}},
        {'usage': {'cpu': '50m', 'memory': '32Mi'}}]}]
    updated = with_metrics((first, second, deployment), values)
    assert (updated[0].cpu, updated[0].memory) == (150, 96)
    assert updated[1].cpu is None and updated[2].cpu is None
    assert first.cpu is None  # Normalized resources stay immutable.


@pytest.mark.parametrize(('operator', 'labels', 'expected'), [
    ('In', {'app': 'worker'}, True), ('In', {}, False),
    ('NotIn', {'app': 'other'}, True), ('NotIn', {'app': 'worker'}, False),
    ('Exists', {'app': 'other'}, True), ('Exists', {}, False),
    ('DoesNotExist', {}, True), ('DoesNotExist', {'app': 'worker'}, False),
])
def test_kubernetes_selector_expressions(operator, labels, expected):
    selector = {'matchExpressions': [{'key': 'app', 'operator': operator, 'values': ['worker']}]}
    assert matches(labels, selector) == expected


@pytest.mark.parametrize('changed', [{'cwd': '/different'}, {'workspace': 'w3'}, {'kind': 'other'}])
def test_agent_identity_changes_refuse_before_pasting(changed):
    runner = FakeHerdr()
    with pytest.raises(HandoffError, match='changed or closed'):
        Herdr(runner).draft(replace(agent(), **changed), 'request')
    assert not any(c[1:3] == ['pane', 'send-text'] for c in runner.calls)


@pytest.mark.parametrize('failure', ['missing', 'malformed', 'timeout', 'refused'])
def test_discovery_failures_have_actionable_error(failure):
    def runner(command, **kwargs):
        if failure == 'missing':
            raise FileNotFoundError('missing')
        if failure == 'timeout':
            raise subprocess.TimeoutExpired(command, 4)
        return SimpleNamespace(returncode=1 if failure == 'refused' else 0, stdout='not json')
    with pytest.raises(HandoffError):
        Herdr(runner).discover()


def test_discovery_excludes_self_prioritizes_current_workspace_and_tolerates_label_failure(monkeypatch):
    monkeypatch.setenv('HERDR_PANE_ID', 'self')
    monkeypatch.setenv('HERDR_WORKSPACE_ID', 'local')
    base = {'agent': 'codex', 'workspace_id': 'remote', 'tab_id': 't1'}
    rows = [dict(base, pane_id='remote'), dict(base, pane_id='self'),
            dict(base, pane_id='local', workspace_id='local'), {'pane_id': 'terminal'},
            {'pane_id': 'incomplete', 'agent': 'codex'}]
    def runner(command, **kwargs):
        return SimpleNamespace(returncode=0 if command[1:] == ['agent', 'list'] else 1,
                               stdout=json.dumps({'result': {'agents': rows}}))
    discovered = Herdr(runner).discover()
    assert [a.pane for a in discovered] == ['local', 'remote']
    assert discovered[0].workspace_name == 'local'


@pytest.mark.parametrize('available', [False, True])
def test_clipboard_failure_is_reported_without_consuming_evidence(monkeypatch, available):
    monkeypatch.setattr('uffe.herdr.shutil.which', lambda _: '/fake/clipboard' if available else None)
    def fail(*args, **kwargs):
        raise subprocess.CalledProcessError(1, args[0])
    monkeypatch.setattr('uffe.herdr.subprocess.run', fail)
    with pytest.raises(HandoffError, match='Clipboard failed' if available else 'No clipboard tool'):
        clipboard('request')
