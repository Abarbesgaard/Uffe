#!/usr/bin/env bash
set -euo pipefail
uffe_root="${HERDR_PLUGIN_ROOT:-$(cd "$(dirname "$0")/.." && pwd)}"
exec "$uffe_root/.venv/bin/uffe" workspace
