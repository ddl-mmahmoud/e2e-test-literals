#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")"
exec uv run uvicorn src.e2e_test_literals.service.app:app --port 8888 --host 0.0.0.0
