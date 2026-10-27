# Uffe

Read MVP.md before changing behavior. Kubernetes access is strictly read-only.
Never send to a real agent during automated tests: use the fake transport/runner.
Keep user profiles, kubeconfig, credentials, live logs and snapshots out of git.
Run `uv run pytest` and `uv run ruff check .` before reporting a change complete.
Test live behavior in a new temporary herdr workspace; preserve existing workspaces.
