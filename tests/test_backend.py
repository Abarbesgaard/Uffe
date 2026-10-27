"""Exercise backend orchestration with local fake collectors and fake API reads."""
import asyncio
import sys

import pytest

from uffe.backend import LiveBackend, ReadError
from uffe.config import demo_config
from test_core import line


async def until(predicate):
    async def wait():
        while not predicate():
            await asyncio.sleep(.01)
    await asyncio.wait_for(wait(), timeout=4)


@pytest.mark.asyncio
async def test_collector_streams_duplicates_reconnects_and_cleans_owned_processes(monkeypatch):
    backend = LiveBackend(demo_config())
    environment = backend.config.environments[0]
    commands, children = [], []
    real_spawn = asyncio.create_subprocess_exec

    async def spawn(*command, **kwargs):
        commands.append(command)
        # A real local child proves pipe ingestion, process ownership and shutdown.
        # It never runs Stern or contacts Kubernetes.
        source = 'import sys,time; print("not-json",flush=True); '
        source += f'print({line("duplicate")!r},flush=True); print({line("duplicate")!r},flush=True); '
        source += 'time.sleep(60)' if len(commands) > 1 else 'sys.exit(1)'
        process = await real_spawn(sys.executable, '-u', '-c', source, **kwargs)
        children.append(process)
        return process

    monkeypatch.setattr('uffe.backend.shutil.which', lambda _: 'fake-stern')
    monkeypatch.setattr('uffe.backend.asyncio.create_subprocess_exec', spawn)
    backend.tasks.append(asyncio.create_task(backend.collect_logs(environment)))
    try:
        await until(lambda: len(backend.logs.records) == 4)
        assert backend.stream_seen[environment.context] == 6
        assert backend.stream_parsed[environment.context] == 4
        assert {r.message for r in backend.logs.records} == {'duplicate'}
        assert len({r.id for r in backend.logs.records}) == 4
        first, second = commands
        assert first[first.index('--tail')+1] == '200'
        assert second[second.index('--tail')+1] == '0'
        assert first[first.index('--context')+1] == environment.context
        assert [first[i+1] for i, word in enumerate(first) if word == '-n'] == list(environment.namespaces)
    finally:
        await backend.close()
    assert not backend.processes
    assert all(child.returncode is not None for child in children)
    assert all(task.done() for task in backend.tasks)
    assert all(not client.directory.exists() for client in backend.clients.values())


@pytest.mark.asyncio
async def test_failed_environment_and_optional_reads_do_not_block_healthy_pods(monkeypatch):
    backend = LiveBackend(demo_config())
    calls = []
    fail_healthy = False

    def get(client, path):
        calls.append((client.environment.context, path))
        if client.environment.context == 'demo-prod' or fail_healthy:
            raise ReadError('synthetic unavailable')
        if 'metrics.k8s.io' in path or path.endswith('/events'):
            raise ReadError('optional resource forbidden')
        if path.endswith('/deployments'):
            return []
        namespace = path.split('/')[4]
        return [{'metadata': {'name': 'worker', 'namespace': namespace, 'uid': namespace},
                 'spec': {'containers': [{'name': 'app'}]},
                 'status': {'phase': 'Running'}}]

    async def no_collector(self, environment):
        await asyncio.Event().wait()

    monkeypatch.setattr('uffe.backend.ReadClient.get', get)
    monkeypatch.setattr(LiveBackend, 'collect_logs', no_collector)
    await backend.start()
    try:
        await until(lambda: all(s.observed_at for (c, _), s in backend.scopes.items() if c == 'demo-uat')
                    and all(s.events_state == 'unavailable' for (c, _), s in backend.scopes.items() if c == 'demo-uat'))
        healthy = backend.scopes[('demo-uat', 'payments')]
        assert healthy.resources and not healthy.error
        assert healthy.metrics_state == healthy.events_state == 'unavailable'
        assert all(s.error for (c, _), s in backend.scopes.items() if c == 'demo-prod')
        fail_healthy = True
        backend.refresh.set()
        await until(lambda: bool(backend.scopes[('demo-uat', 'payments')].error))
        stale = backend.scopes[('demo-uat', 'payments')]
        assert stale.resources == healthy.resources and stale.observed_at == healthy.observed_at
        assert all('/namespaces/' in path for _, path in calls)
    finally:
        await backend.close()


@pytest.mark.asyncio
async def test_missing_stern_has_explicit_state_without_spawning(monkeypatch):
    backend = LiveBackend(demo_config())
    monkeypatch.setattr('uffe.backend.shutil.which', lambda _: None)
    try:
        await backend.collect_logs(backend.config.environments[0])
        assert backend.stream_state['demo-prod'] == 'stern missing from PATH'
        assert not backend.processes and not backend.logs.records
    finally:
        await backend.close()
