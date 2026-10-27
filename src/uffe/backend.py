"""Scoped GET transport and app-owned, bounded Stern streams."""
import asyncio
from dataclasses import replace
import http.client
import json
import os
from pathlib import Path
import re
import shutil
import signal
import socket
import subprocess
import tempfile
import time

from .config import Config, Environment
from .model import KubeEvent, LogBuffer, Scope, clean, normalize, timestamp, utcnow, with_metrics


class ReadError(RuntimeError):
    pass


class UnixHTTP(http.client.HTTPConnection):
    def __init__(self, path: Path, timeout: float = 8):
        super().__init__("localhost", timeout=timeout)
        self.path = path

    def connect(self):
        self.sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.sock.settimeout(self.timeout)
        self.sock.connect(str(self.path))


class ReadClient:
    """One persistent authenticated Kubernetes client per environment.

    The Unix socket is private, routes are exact allowlists and every HTTP call
    made by Uffe is GET. Transport creation never rewrites kubeconfig.
    """
    def __init__(self, environment: Environment):
        self.environment = environment
        self.directory = Path(tempfile.mkdtemp(prefix="uffe-", dir="/tmp"))
        self.socket = self.directory / "api.sock"
        self.process: subprocess.Popen | None = None
        self.closed = False

    def allowed(self, path: str) -> bool:
        for namespace in self.environment.namespaces:
            if path in (f"/api/v1/namespaces/{namespace}/pods", f"/api/v1/namespaces/{namespace}/events",
                        f"/apis/apps/v1/namespaces/{namespace}/deployments",
                        f"/apis/metrics.k8s.io/v1beta1/namespaces/{namespace}/pods"):
                return True
        return False

    def start(self):
        if self.closed:
            raise ReadError("Connection closed")
        if self.process is not None:
            if self.process.poll() is not None:
                raise ReadError("Connection stopped; press r to retry")
            return
        executable = shutil.which("kubectl")
        if not executable:
            raise ReadError("kubectl is missing from PATH")
        namespaces = "(" + "|".join(re.escape(n) for n in self.environment.namespaces) + ")"
        paths = (rf"^(/api/v1/namespaces/{namespaces}/(pods|events)|"
                 rf"/apis/apps/v1/namespaces/{namespaces}/deployments|"
                 rf"/apis/metrics.k8s.io/v1beta1/namespaces/{namespaces}/pods)$")
        self.process = subprocess.Popen(
            [executable, "--context", self.environment.context, "proxy", f"--unix-socket={self.socket}",
             f"--accept-paths={paths}", "--reject-methods=POST|PUT|PATCH|DELETE|CONNECT|OPTIONS|HEAD|TRACE"],
            stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            start_new_session=True, umask=0o077)
        for _ in range(100):
            if self.socket.exists():
                return
            if self.process.poll() is not None:
                raise ReadError("Cannot start kubectl connection; check kubeconfig/authentication")
            time.sleep(.02)
        raise ReadError("Connection startup timed out")

    def get(self, path: str) -> list:
        if not self.allowed(path):
            raise ReadError("Read outside configured namespaces/resources refused")
        self.start()
        connection = UnixHTTP(self.socket)
        try:
            connection.request("GET", path)
            response = connection.getresponse()
            if response.status != 200:
                response.read(1024)
                raise ReadError(f"HTTP {response.status}; check namespace access/authentication")
            # Do not persist raw Pod responses, which may contain environment data.
            return json.loads(response.read()).get("items", [])
        except (TimeoutError, OSError) as error:
            raise ReadError("API/authentication unavailable; existing data is stale") from error
        finally:
            connection.close()

    def reset_dead(self):
        if self.process is not None and self.process.poll() is not None:
            self.process = None
            self.socket.unlink(missing_ok=True)

    def close(self):
        self.closed = True
        if self.process is not None and self.process.poll() is None:
            try:
                os.killpg(self.process.pid, signal.SIGTERM)
                self.process.wait(timeout=2)
            except (OSError, subprocess.TimeoutExpired):
                try:
                    os.killpg(self.process.pid, signal.SIGKILL)
                except OSError:
                    pass
        shutil.rmtree(self.directory, ignore_errors=True)


class LiveBackend:
    def __init__(self, config: Config):
        self.config = config
        self.logs = LogBuffer(config.log_buffer)
        self.clients = {e.context: ReadClient(e) for e in config.environments}
        self.scopes = {(e.context, n): Scope(e.context, n) for e in config.environments for n in e.namespaces}
        self.stream_state = {e.context: "connecting" for e in config.environments}
        self.stream_seen = {e.context: 0 for e in config.environments}
        self.stream_parsed = {e.context: 0 for e in config.environments}
        self.tasks: list[asyncio.Task] = []
        self.processes: set[asyncio.subprocess.Process] = set()
        self.changed = asyncio.Event()
        self.stopping = False
        self.refresh = asyncio.Event()

    async def start(self):
        # Start each connection once, outside the UI. An environment's core reads
        # are sequential to avoid competing exec-auth helpers; environments are independent.
        for environment in self.config.environments:
            self.tasks.append(asyncio.create_task(self.poll(environment)))
            self.tasks.append(asyncio.create_task(self.collect_logs(environment)))

    async def poll(self, environment: Environment):
        client = self.clients[environment.context]
        while not self.stopping:
            client.reset_dead()
            for namespace in environment.namespaces:
                if self.stopping:
                    break
                key = (environment.context, namespace)
                try:
                    pods = await asyncio.to_thread(client.get, f"/api/v1/namespaces/{namespace}/pods")
                    deps = await asyncio.to_thread(client.get, f"/apis/apps/v1/namespaces/{namespace}/deployments")
                    resources = normalize(environment.context, namespace, pods, deps)
                    previous = self.scopes[key]
                    self.scopes[key] = replace(previous, resources=resources, observed_at=utcnow(), error="")
                    self.changed.set()
                    # Ancillary reads never stand between core state and first paint.
                    await self.extras(client, key)
                except Exception as error:
                    self.scopes[key] = replace(self.scopes[key], error=clean(str(error))[:160])
                    self.changed.set()
            try:
                await asyncio.wait_for(self.refresh.wait(), timeout=self.config.refresh_seconds)
                self.refresh.clear()
            except TimeoutError:
                pass

    async def extras(self, client: ReadClient, key: tuple[str, str]):
        context, namespace = key
        try:
            values = await asyncio.to_thread(client.get, f"/apis/metrics.k8s.io/v1beta1/namespaces/{namespace}/pods")
            scope = self.scopes[key]
            self.scopes[key] = replace(scope, resources=with_metrics(scope.resources, values), metrics_state="available")
        except Exception:
            self.scopes[key] = replace(self.scopes[key], metrics_state="unavailable")
        self.changed.set()
        try:
            values = await asyncio.to_thread(client.get, f"/api/v1/namespaces/{namespace}/events")
            events = tuple(sorted((KubeEvent(context, namespace, v["metadata"]["uid"],
                v.get("involvedObject", {}).get("name", ""), v.get("involvedObject", {}).get("uid", ""),
                timestamp(v.get("lastTimestamp") or v.get("eventTime") or v["metadata"].get("creationTimestamp")),
                v.get("type", "Normal"), v.get("reason", ""), clean(v.get("message", "")), v.get("count", 1))
                for v in values), key=lambda e: e.at, reverse=True))[:100]
            self.scopes[key] = replace(self.scopes[key], events=events, events_state="available")
        except Exception:
            self.scopes[key] = replace(self.scopes[key], events_state="unavailable")
        self.changed.set()

    async def collect_logs(self, environment: Environment):
        binary = shutil.which("stern")
        if not binary:
            self.stream_state[environment.context] = "stern missing from PATH"
            return
        command = [binary, ".", "--context", environment.context, "-o", "json", "--timestamps",
                   "--tail", "200", "--since", self.config.log_history, "--only-log-lines",
                   "--init-containers=false", "--container-state", "running", "--max-log-requests", "100"]
        for namespace in environment.namespaces:
            command += ["-n", namespace]
        for container in self.config.exclude_containers:
            command += ["--exclude-container", container]
        delay = 1
        while not self.stopping:
            process = None
            stderr_task = None
            try:
                process = await asyncio.create_subprocess_exec(*command, stdin=asyncio.subprocess.DEVNULL,
                    stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE, start_new_session=True,
                    limit=1024*1024)
                self.processes.add(process)
                self.stream_state[environment.context] = "live"

                async def errors():
                    assert process and process.stderr
                    async for raw in process.stderr:
                        text = raw.decode(errors="replace").lower()
                        if any(v in text for v in ("unauthorized", "forbidden", "authenticate", "sign in", "device")):
                            self.stream_state[environment.context] = "authentication/access required"
                        elif any(v in text for v in ("error", "failed", "connection")):
                            self.stream_state[environment.context] = "reconnecting"

                stderr_task = asyncio.create_task(errors())
                assert process.stdout
                async for raw in process.stdout:
                    self.stream_seen[environment.context] += 1
                    if self.logs.append(environment.context, raw.decode(errors="replace")):
                        self.stream_parsed[environment.context] += 1
                    self.stream_state[environment.context] = "live"
                await process.wait()
            except (OSError, ValueError) as error:
                self.stream_state[environment.context] = f"collector unavailable: {type(error).__name__}"
            finally:
                if stderr_task:
                    stderr_task.cancel()
                    await asyncio.gather(stderr_task, return_exceptions=True)
                if process:
                    if process.returncode is None:
                        try:
                            os.killpg(process.pid, signal.SIGTERM)
                            await asyncio.wait_for(process.wait(), 2)
                        except (OSError, TimeoutError):
                            try:
                                os.killpg(process.pid, signal.SIGKILL)
                            except OSError:
                                pass
                    self.processes.discard(process)
            if self.stopping:
                break
            self.stream_state[environment.context] = "reconnecting"
            await asyncio.sleep(delay)
            delay = min(delay*2, 15)
            # On reconnect, avoid repeatedly replaying all history.
            command[command.index("--tail")+1] = "0"

    async def close(self):
        self.stopping = True
        for task in self.tasks:
            task.cancel()
        await asyncio.gather(*self.tasks, return_exceptions=True)
        await asyncio.gather(*(asyncio.to_thread(c.close) for c in self.clients.values()))


class DemoBackend:
    """Deterministic synthetic data. No network or child processes."""
    def __init__(self, config: Config):
        self.config = config
        self.logs = LogBuffer(config.log_buffer)
        self.scopes: dict[tuple[str, str], Scope] = {}
        self.stream_state = {e.context: "demo" for e in config.environments}
        self.refresh = asyncio.Event()
        self.task: asyncio.Task | None = None
        for e in config.environments:
            for ns in e.namespaces:
                workload = "payment-worker" if ns == "payments" else "support-api"
                pod = {"metadata": {"name": workload+"-8bb74-demo1", "namespace": ns, "uid": e.context+ns,
                        "creationTimestamp": utcnow().isoformat(), "labels": {"app": workload}},
                       "spec": {"nodeName": "demo-node", "containers": [{"name": "app", "image": "example/"+workload+":1.2.3", "resources": {"requests": {"cpu": "100m", "memory": "128Mi"}}}]},
                       "status": {"phase": "Running", "conditions": [{"type": "Ready", "status": "True"}],
                       "containerStatuses": [{"name": "app", "ready": True, "restartCount": 1, "state": {"running": {}}}]}}
                dep = {"metadata": {"name": workload, "uid": e.context+ns+"dep", "generation": 1},
                       "spec": {"replicas": 1, "selector": {"matchLabels": {"app": workload}}, "template": {"spec": pod["spec"]}},
                       "status": {"readyReplicas": 1, "updatedReplicas": 1, "availableReplicas": 1, "observedGeneration": 1}}
                resources = tuple(replace(r, cpu=42, memory=96) for r in normalize(e.context, ns, [pod], [dep]))
                event = KubeEvent(e.context, ns, e.context+ns+"event", pod["metadata"]["name"], pod["metadata"]["uid"],
                                  utcnow(), "Warning", "BackOff", "Synthetic retry event for the offline demo", 3)
                self.scopes[(e.context, ns)] = Scope(e.context, ns, resources, (event,), utcnow(), "", "available", "available")

    def tick(self):
        for (context, namespace), scope in self.scopes.items():
            pod = next(r for r in scope.resources if r.kind == "Pod")
            self.logs.append(context, json.dumps({"podName": pod.name, "namespace": namespace,
                "containerName": "app", "timestamp": utcnow().isoformat(),
                "message": json.dumps({"level": "warning", "message": "Synthetic request timed out; retrying", "attempt": 2})}))

    async def start(self):
        self.tick()

        async def loop():
            while True:
                await asyncio.sleep(2)
                self.tick()
                self.scopes = {k: replace(v, observed_at=utcnow()) for k, v in self.scopes.items()}
        self.task = asyncio.create_task(loop())

    async def close(self):
        if self.task:
            self.task.cancel()
            await asyncio.gather(self.task, return_exceptions=True)
