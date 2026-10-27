#!/usr/bin/env bash
set -euo pipefail
uffe_root="${HERDR_PLUGIN_ROOT:-$(cd "$(dirname "$0")/.." && pwd)}"
if ! command -v uv >/dev/null 2>&1; then
  printf '%s\n' 'Uffe needs uv on PATH: https://docs.astral.sh/uv/getting-started/installation/' >&2
  exit 1
fi
uv sync --project "$uffe_root" --locked --no-dev
