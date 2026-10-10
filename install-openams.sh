#!/bin/bash
# Compatibility shim: the installer has moved to install.sh.
# Existing bookmarks, documentation, and scripts that call install-openams.sh
# continue to work unchanged.
SCRIPT_DIR="$( cd "$( dirname "${BASH_SOURCE[0]}" )" && pwd )"
exec "$SCRIPT_DIR/install.sh" "$@"
