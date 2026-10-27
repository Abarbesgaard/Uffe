"""Portable configuration. Context names are references, never credentials."""
from dataclasses import dataclass
import os
from pathlib import Path
import re
import tomllib
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError


@dataclass(frozen=True)
class Environment:
    name: str
    context: str
    namespaces: tuple[str, ...]


@dataclass(frozen=True)
class Repository:
    namespace: str
    workload: str
    path: str


@dataclass(frozen=True)
class Config:
    environments: tuple[Environment, ...]
    repositories: tuple[Repository, ...] = ()
    timezone: str = "local"
    refresh_seconds: int = 20
    log_history: str = "30m"
    log_buffer: int = 5000
    exclude_containers: tuple[str, ...] = ()

    @property
    def namespaces(self) -> tuple[str, ...]:
        return tuple(dict.fromkeys(n for e in self.environments for n in e.namespaces))

    def repo_for(self, namespace: str, workload: str) -> str:
        return next((r.path for r in self.repositories if r.namespace == namespace and r.workload == workload), "")


def default_path() -> Path:
    return Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config")) / "uffe/config.toml"


def load(path: Path) -> Config:
    try:
        data = tomllib.loads(path.read_text())
    except (OSError, tomllib.TOMLDecodeError) as error:
        raise ValueError(f"Cannot read configuration {path}: {error}") from error
    if data.get("schema_version", 1) != 1:
        raise ValueError("Unsupported schema_version; this Uffe supports version 1")
    environments = []
    for item in data.get("environments", []):
        name, context, namespaces = item.get("name"), item.get("context"), item.get("namespaces")
        if not isinstance(name, str) or not name.strip() or not isinstance(context, str) or not context.strip():
            raise ValueError("Each environment needs a nonempty name and kubeconfig context")
        if not isinstance(namespaces, list) or not namespaces:
            raise ValueError(f"{name}: namespaces must be a nonempty array")
        if any(not isinstance(n, str) or not re.fullmatch(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?", n) for n in namespaces):
            raise ValueError(f"{name}: invalid Kubernetes namespace")
        environments.append(Environment(name.strip(), context.strip(), tuple(dict.fromkeys(namespaces))))
    if not 1 <= len(environments) <= 4:
        raise ValueError("Configure between one and four environments")
    if len({e.context for e in environments}) != len(environments):
        raise ValueError("Each environment must reference a different context")
    if len({e.name for e in environments}) != len(environments):
        raise ValueError("Environment names must be unique")
    timezone = data.get("timezone", "local")
    if timezone != "local":
        try:
            ZoneInfo(timezone)
        except (ZoneInfoNotFoundError, TypeError) as error:
            raise ValueError("timezone must be local or an IANA timezone") from error
    interval, size = data.get("refresh_seconds", 20), data.get("log_buffer", 5000)
    if not isinstance(interval, int) or not 5 <= interval <= 300:
        raise ValueError("refresh_seconds must be between 5 and 300")
    if not isinstance(size, int) or not 100 <= size <= 50000:
        raise ValueError("log_buffer must be between 100 and 50000")
    history = data.get("log_history", "30m")
    if not isinstance(history, str) or not re.fullmatch(r"\d+[smh]", history):
        raise ValueError("log_history must be a duration such as 30m or 2h")
    excludes = data.get("exclude_containers", [])
    if not isinstance(excludes, list) or any(not isinstance(v, str) for v in excludes):
        raise ValueError("exclude_containers must be an array of regular expressions")
    for value in excludes:
        try:
            re.compile(value)
        except re.error as error:
            raise ValueError("Invalid exclude_containers expression") from error
    repositories = tuple(Repository(r["namespace"], r["workload"], str(Path(r["path"]).expanduser()))
                         for r in data.get("repositories", []))
    return Config(tuple(environments), repositories, timezone, interval, history, size, tuple(excludes))


def demo_config() -> Config:
    return Config((Environment("PROD", "demo-prod", ("payments", "support")),
                   Environment("UAT", "demo-uat", ("payments", "support"))))
