#!/bin/sh
# SPDX-License-Identifier: GPL-3.0-or-later
set -eu
cd "$(dirname "$0")/../.."
exec timeout 420s make -f tests/klipper_tmc_uart/Makefile test "$@"
