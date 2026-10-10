#!/usr/bin/env bash
# SPDX-License-Identifier: GPL-3.0-or-later
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_DIR="$(cd "${SCRIPT_DIR}/../.." && pwd)"

echo "=== Building Klipper Clock Adapter Test Suite ==="
timeout 60s make -C "${REPO_DIR}" -f tests/klipper_clock/Makefile build

echo "=== Running Klipper Clock Adapter Test Suite ==="
timeout 15s make -C "${REPO_DIR}" -f tests/klipper_clock/Makefile test

echo "=== Tests Complete Successfully ==="
