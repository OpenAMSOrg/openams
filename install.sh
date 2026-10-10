#!/bin/bash
# OpenAMS installer. See README.md for usage.
# Force script to exit if an error occurs
set -e

KLIPPER_PATH="${HOME}/klipper"
GCO_ROUTINES_PATH="${HOME}/gco-routines"
KLIPPER_SERVICE_NAME=klipper
SYSTEMDDIR="/etc/systemd/system"
MOONRAKER_CONFIG_DIR="${HOME}/printer_data/config"
MOONRAKER_PATH="${MOONRAKER_PATH:-${HOME}/moonraker}"
# Documented test switch: skip every service action, so the installer can run
# against a tree with no systemd (a fake checkout, a container).
NO_SERVICE="${OPENAMS_NO_SERVICE:-0}"
WITH_MCU_ADDON=0
GCO_ROUTINES_SKIP=0
GCO_ROUTINES_REQUIRE=0
WITH_KLIPPER_PATCHES=0
REMOVE_KLIPPER_PATCHES=0
# Spoolman is part of the default install. --no-spoolman skips it;
# --spoolman-url points at a server elsewhere and installs nothing.
NO_SPOOLMAN=0
SPOOLMAN_URL_ARG=""
SPOOLMAN_PORT=7912
SPOOLMAN_DIR="${OPENAMS_SPOOLMAN_DIR:-${HOME}/Spoolman}"
# Documented test switches (no network in tests): the probe URL defaults to the
# local server; OPENAMS_SPOOLMAN_INSTALLER is a script run instead of
# downloading Spoolman's release; OPENAMS_SPOOLMAN_WAIT is how many seconds to
# wait for a freshly installed server; OPENAMS_SPOOLMAN_FAIL_WRITE=1 makes the
# moonraker.conf write fail midway, to exercise the rollback;
# OPENAMS_SNAPSHOT_BASE is the snapshot.debian.org base ensure_pg_config reads
# libpq-dev from, so tests can serve it from file:// URLs.
SPOOLMAN_INFO_URL="${OPENAMS_SPOOLMAN_INFO_URL:-http://localhost:${SPOOLMAN_PORT}/api/v1/info}"
SPOOLMAN_WAIT="${OPENAMS_SPOOLMAN_WAIT:-60}"
SPOOLMAN_SERVER=""

# Fall back to old directory for configuration as default
if [ ! -d "${MOONRAKER_CONFIG_DIR}" ]; then
    echo "\"$MOONRAKER_CONFIG_DIR\" does not exist. Falling back to \"${HOME}/klipper_config\" as default."
    MOONRAKER_CONFIG_DIR="${HOME}/klipper_config"
fi

usage(){ echo "Usage: $0 [-k <klipper path>] [-s <klipper service name>] [-c <configuration path>] [-r <gco-routines checkout>] [-u] [--with-mcu-addon] [--skip-gco-routines | --require-gco-routines] [--with-klipper-patches | --remove-klipper-patches] [--spoolman-url <url> | --no-spoolman] [--cortex-bridge | --no-cortex-bridge] [--mainsail-panel]" 1>&2; exit 1; }
# Parse command line arguments. The one long option is pulled out first so
# getopts never sees it, then the short options are parsed as before.
SHORT_ARGS=()
EXPECT_SPOOLMAN_URL=0
SPOOLMAN_URL_GIVEN=0
for arg in "$@"; do
    if [ "$EXPECT_SPOOLMAN_URL" = "1" ]; then
        SPOOLMAN_URL_ARG="$arg"
        SPOOLMAN_URL_GIVEN=1
        EXPECT_SPOOLMAN_URL=0
        continue
    fi
    case "$arg" in
        --spoolman-url) EXPECT_SPOOLMAN_URL=1;;
        --spoolman-url=*) SPOOLMAN_URL_ARG="${arg#--spoolman-url=}"; SPOOLMAN_URL_GIVEN=1;;
        --no-spoolman) NO_SPOOLMAN=1;;
        --with-mcu-addon) WITH_MCU_ADDON=1;;
        --skip-gco-routines) GCO_ROUTINES_SKIP=1;;
        --require-gco-routines) GCO_ROUTINES_REQUIRE=1;;
        --with-klipper-patches) WITH_KLIPPER_PATCHES=1;;
        --remove-klipper-patches) REMOVE_KLIPPER_PATCHES=1;;
        --cortex-bridge) CORTEX_BRIDGE_ON=1;;
        --no-cortex-bridge) CORTEX_BRIDGE_OFF=1;;
        --mainsail-panel) MAINSAIL_PANEL=1;;
        -h|--help) usage;;
        *) SHORT_ARGS+=("$arg");;
    esac
done
if [ "$EXPECT_SPOOLMAN_URL" = "1" ] || { [ "$SPOOLMAN_URL_GIVEN" = "1" ] && [ -z "$SPOOLMAN_URL_ARG" ]; }; then
    echo "[ERROR] --spoolman-url needs a URL, for example http://192.168.1.20:7912" >&2
    exit 1
fi
if [ "$SPOOLMAN_URL_GIVEN" = "1" ] && [ "$NO_SPOOLMAN" = "1" ]; then
    echo "[ERROR] --spoolman-url and --no-spoolman cannot be combined." >&2
    exit 1
fi
if [[ "$GCO_ROUTINES_SKIP" == 1 && "$GCO_ROUTINES_REQUIRE" == 1 ]]; then
    echo "[ERROR] --skip-gco-routines and --require-gco-routines cannot be combined." >&2
    exit 1
fi
if [[ "${CORTEX_BRIDGE_ON:-0}" == 1 && "${CORTEX_BRIDGE_OFF:-0}" == 1 ]]; then
    echo "[ERROR] --cortex-bridge and --no-cortex-bridge cannot be combined." >&2
    exit 1
fi
if [[ "${CORTEX_BRIDGE_ON:-0}" == 1 ]]; then
    CORTEX_BRIDGE_MODE=on
elif [[ "${CORTEX_BRIDGE_OFF:-0}" == 1 ]]; then
    CORTEX_BRIDGE_MODE=off
fi
set -- ${SHORT_ARGS[@]+"${SHORT_ARGS[@]}"}
while getopts "k:s:c:r:uh" arg; do
    case $arg in
        k) KLIPPER_PATH=$OPTARG;;
        s) KLIPPER_SERVICE_NAME=$OPTARG;;
        c) MOONRAKER_CONFIG_DIR=$OPTARG;;
        r) GCO_ROUTINES_PATH=$OPTARG;;
        u) UNINSTALL=1;;
        h) usage;;
    esac
done

if [ "$WITH_KLIPPER_PATCHES" = "1" ] && [ "$REMOVE_KLIPPER_PATCHES" = "1" ]; then
    echo "[ERROR] --with-klipper-patches and --remove-klipper-patches cannot be used together." 1>&2
    exit 1
fi
if [ "$WITH_KLIPPER_PATCHES" = "1" ] && [ -n "${UNINSTALL:-}" ]; then
    echo "[ERROR] --with-klipper-patches cannot be combined with -u." 1>&2
    exit 1
fi
if [ "$REMOVE_KLIPPER_PATCHES" = "1" ] && [ -n "${UNINSTALL:-}" ]; then
    echo "[ERROR] -u already removes the Klipper patches if they are applied; drop --remove-klipper-patches." 1>&2
    exit 1
fi

# Find SRCDIR from the pathname of this script
SCRIPT_DIR="$( cd "$( dirname "${BASH_SOURCE[0]}" )" && pwd )"
SRCDIR="$SCRIPT_DIR/src"
SCRIPTSDIR="$SCRIPT_DIR/scripts"
MMU_INSTALLER="$SCRIPT_DIR/klipper_mmu/tools/install.py"
PATCH_HELPER="$SCRIPT_DIR/tools/klipper_patches.py"
source "$SCRIPT_DIR/install_helpers/gco-routines.sh"
source "$SCRIPT_DIR/install_helpers/cortex-bridge.sh"
source "$SCRIPT_DIR/install_helpers/mainsail.sh"

# The single place a service is touched. With OPENAMS_NO_SERVICE=1 the call is
# printed and skipped, so the rest of the installer can be exercised without
# systemd and without sudo.
service()
{
    if [ "$NO_SERVICE" = "1" ]; then
        echo "[skipped: OPENAMS_NO_SERVICE=1] systemctl $*"
        return 0
    fi
    sudo systemctl "$@"
}

# Verify Klipper has been installed
check_klipper()
{
    if [ "$NO_SERVICE" = "1" ]; then
        echo "OPENAMS_NO_SERVICE=1: not looking for the \"$KLIPPER_SERVICE_NAME\" service."
        return 0
    fi
    if [ "$(sudo systemctl list-units --full -all -t service --no-legend | grep -F "$KLIPPER_SERVICE_NAME.service")" ]; then
        echo "Klipper service found with name \"$KLIPPER_SERVICE_NAME\"."
    else
        echo "[ERROR] Klipper service with name \"$KLIPPER_SERVICE_NAME\" not found, please install Klipper first or specify name with -s."
        exit -1
    fi
}

# The klipper-mmu installer only accepts a full Klipper source checkout, not a
# klippy/ tree alone.
klipper_source_checkout()
{
    [ -f "${KLIPPER_PATH}/Makefile" ] && [ -d "${KLIPPER_PATH}/klippy" ]
}

# klipper-mmu owns the klippy/extras/klipper_mmu extra name and refuses to
# install beside a klippy/extras/klipper_mmu.py. Refuse the whole run rather
# than half-install: nothing in src/ may ever claim that name.
check_mmu_name_clash()
{
    local clash clash_found=0
    for clash in "${SRCDIR}"/klipper_mmu*.py; do
        if [ -e "$clash" ]; then
            echo "[ERROR] \"$clash\" collides with the klipper_mmu Klipper extra; rename or remove it."
            clash_found=1
        fi
    done
    if [ -e "${KLIPPER_PATH}/klippy/extras/klipper_mmu.py" ] || [ -L "${KLIPPER_PATH}/klippy/extras/klipper_mmu.py" ]; then
        echo "[ERROR] \"${KLIPPER_PATH}/klippy/extras/klipper_mmu.py\" exists and would compete with the klipper_mmu Klipper extra; remove it first."
        clash_found=1
    fi
    return $clash_found
}

check_folders()
{
    if [ ! -d "$KLIPPER_PATH/klippy/extras/" ]; then
        echo "[ERROR] Klipper installation not found in directory \"$KLIPPER_PATH\". Exiting."
        exit -1
    fi
    echo "Klipper installation found at $KLIPPER_PATH"

    if [ ! -f "${MOONRAKER_CONFIG_DIR}/moonraker.conf" ]; then
        echo "[ERROR] Moonraker configuration not found in directory \"$MOONRAKER_CONFIG_DIR\". Exiting."
        exit -1
    fi
    echo "Moonraker configuration found at $MOONRAKER_CONFIG_DIR"

    if ! check_mmu_name_clash; then
        echo "[ERROR] A klipper_mmu name clash was found; nothing was installed. Exiting."
        exit -1
    fi
}

# Link OpenAMS extension modules to Klipper
link_extension()
{
    echo -n "Linking OpenAMS extension to Klipper... "
    for file in "${SRCDIR}"/*.py; do
        # klipper_mmu is the add-on's own name, never an OpenAMS module.
        case "$(basename "$file")" in
            klipper_mmu*) continue;;
        esac
        ln -sf "${file}" "${KLIPPER_PATH}/klippy/extras/"
    done
    echo "[OK]"
}

# Link the openams_spoolman Moonraker component into a Moonraker checkout
# (C4 §1, §6-§8). Skipped, with a message, when no Moonraker checkout is
# found at MOONRAKER_PATH; does not touch moonraker.conf (a separate task).
link_moonraker_component()
{
    if [ ! -d "${MOONRAKER_PATH}/moonraker/components" ]; then
        echo "No Moonraker checkout found at \"${MOONRAKER_PATH}\"; skipping the openams_spoolman component link."
        return 0
    fi
    echo -n "Linking the openams_spoolman Moonraker component... "
    # Moonraker loads components only when it starts: a link that is new or
    # points somewhere else needs a restart, even when moonraker.conf already
    # has [openams_spoolman] and no other step restarts it.
    if [ "$(readlink "${MOONRAKER_PATH}/moonraker/components/openams_spoolman" 2>/dev/null)" != \
            "${SCRIPT_DIR}/moonraker/openams_spoolman" ]; then
        MOONRAKER_COMPONENT_CHANGED=1
    fi
    ln -sfn "${SCRIPT_DIR}/moonraker/openams_spoolman" "${MOONRAKER_PATH}/moonraker/components/openams_spoolman"
    # Keep Moonraker's own checkout clean for its update manager: the link is
    # an untracked file there.
    local exclude="${MOONRAKER_PATH}/.git/info/exclude"
    if [ -d "${MOONRAKER_PATH}/.git/info" ] && \
            ! grep -qx "moonraker/components/openams_spoolman" "${exclude}" 2>/dev/null; then
        echo "moonraker/components/openams_spoolman" >> "${exclude}"
    fi
    echo "[OK]"
}

# Remove the component link made by link_moonraker_component (uninstall).
unlink_moonraker_component()
{
    local link="${MOONRAKER_PATH}/moonraker/components/openams_spoolman"
    if [ -L "${link}" ]; then
        rm -f "${link}"
        echo "Removed the openams_spoolman Moonraker component link."
    fi
}

# Link OpenAMS helper scripts to Klipper
link_scripts()
{
    echo -n "Linking OpenAMS scripts to Klipper... "
    for file in "${SCRIPTSDIR}"/*.py; do
        ln -sf "${file}" "${KLIPPER_PATH}/scripts/$(basename "$file")"
    done
    echo "[OK]"
}

# Run klipper_mmu/tools/install.py; that tool owns every klipper-mmu rule.
run_mmu_installer()
{
    local action="$1"
    if [ ! -f "$MMU_INSTALLER" ]; then
        echo "[ERROR] \"$MMU_INSTALLER\" is missing; klipper-mmu cannot be installed from this checkout."
        return 1
    fi
    echo -n "Running the klipper-mmu installer (${action})... "
    if ! python3 "$MMU_INSTALLER" "$action" "$KLIPPER_PATH"; then
        echo "[FAILED] klipper-mmu ${action} did not succeed."
        return 1
    fi
    echo "[OK]"
}

# Link the read-only klipper-mmu host extra. Klipper only loads an extra when a
# config section names it, so the link is harmless without one.
link_klipper_mmu_host()
{
    if ! klipper_source_checkout; then
        echo "No Klipper source checkout at \"$KLIPPER_PATH\"; skipping the klipper_mmu host extra."
        return 0
    fi
    run_mmu_installer install-host
    echo "The klipper_mmu extra stays dormant until a [klipper_mmu ...] section names it."
}

# Opt-in: add the klipper-mmu MCU add-on to the Klipper source tree. This is
# the only step that edits Klipper's own files, so say exactly what changes.
install_klipper_mmu_mcu()
{
    if [ -f "${KLIPPER_PATH}/src/Kconfig" ] && [ -f "${KLIPPER_PATH}/src/Makefile" ]; then
        :
    else
        echo "[ERROR] \"$KLIPPER_PATH\" has no Klipper src/Kconfig and src/Makefile to add the klipper-mmu hooks to."
        return 1
    fi
    echo "Adding the optional klipper-mmu MCU add-on to the Klipper source tree."
    echo "  This edits two files of your Klipper checkout:"
    echo "    src/Kconfig:  one optional include, osource \"src/klipper_mmu/mcu/Kconfig\""
    echo "    src/Makefile: one optional include, -include src/klipper_mmu/mcu/Makefile"
    echo "  Each line sits between '# BEGIN klipper-mmu add-on' and '# END klipper-mmu add-on' markers."
    echo "  It also creates the symlink src/klipper_mmu -> ${SCRIPT_DIR}/klipper_mmu."
    echo "  Undo: 'bash install.sh -u', or 'python3 klipper_mmu/tools/install.py remove ${KLIPPER_PATH}'."
    run_mmu_installer install
    echo "A Klipper firmware build is needed before an MMU section takes effect."
}

# Remove whatever klipper-mmu installed, if present (uninstall).
remove_klipper_mmu()
{
    echo "Removing klipper-mmu from Klipper: the host extra first, then the MCU add-on."
    echo "Either may already be absent; both actions are no-ops then."
    if ! klipper_source_checkout; then
        echo "No Klipper source checkout at \"$KLIPPER_PATH\"; nothing to remove."
        return 0
    fi
    run_mmu_installer remove-host
    if [ -f "${KLIPPER_PATH}/src/Kconfig" ] && [ -f "${KLIPPER_PATH}/src/Makefile" ]; then
        run_mmu_installer remove
    else
        echo "No Klipper src/Kconfig and src/Makefile; no MCU add-on hooks to remove."
    fi
}

# Run tools/klipper_patches.py; that tool owns every Klipper-patch rule (checksums,
# the tested base, local changes, what was applied). Its own messages go straight
# to the terminal. OPENAMS_KLIPPER_PATCHES_DIR points it at another series (a test
# switch); OPENAMS_KLIPPER_PATCHES_EXACT_BASE=1 accepts only the tested base, not a
# newer Klipper that left the patched files alone (see klipper_patches/can/README.md).
run_patch_helper()
{
    local action="$1"
    local args=()
    if [ ! -f "$PATCH_HELPER" ]; then
        echo "[ERROR] \"$PATCH_HELPER\" is missing; the Klipper patches cannot be handled from this checkout."
        return 1
    fi
    if [ -n "${OPENAMS_KLIPPER_PATCHES_DIR:-}" ]; then
        args+=(--series "$OPENAMS_KLIPPER_PATCHES_DIR")
    fi
    if [ "${OPENAMS_KLIPPER_PATCHES_EXACT_BASE:-0}" = "1" ]; then
        args+=(--exact-base)
    fi
    python3 "$PATCH_HELPER" ${args[@]+"${args[@]}"} "$action" "$KLIPPER_PATH"
}

# Opt-in: apply the pinned Klipper CAN patch series to the Klipper working tree.
# Like the MCU add-on, this edits Klipper's own files, so say exactly what changes.
install_klipper_patches()
{
    if ! klipper_source_checkout; then
        echo "[ERROR] No Klipper source checkout at \"$KLIPPER_PATH\" to apply the CAN patches to."
        return 1
    fi
    echo "Applying the optional Klipper CAN patch series (klipper_patches/can)."
    echo "  This edits files of your Klipper checkout's working tree (host CAN code, MCU CAN firmware"
    echo "  sources and CAN docs); it makes no commit and changes no branch."
    echo "  It is checked first: the series checksums, a compatible Klipper revision, and no local"
    echo "  changes in the files it touches. If any check fails nothing is changed."
    echo "  Klipper will then show as modified, so Moonraker's update manager flags it."
    echo "  Undo: 'bash install.sh --remove-klipper-patches' or 'bash install.sh -u'."
    echo "Running the Klipper patch helper (apply):"
    if ! run_patch_helper apply; then
        echo "[FAILED] the Klipper patches were not applied."
        return 1
    fi
    echo "[OK]"
    echo "MCU firmware needs a rebuild and reflash before the firmware-side fixes take effect."
}

# Reverse the patch series if it is applied (used by --remove-klipper-patches).
remove_klipper_patches()
{
    if ! klipper_source_checkout; then
        echo "No Klipper source checkout at \"$KLIPPER_PATH\"; no Klipper patches to remove."
        return 0
    fi
    echo "Running the Klipper patch helper (remove):"
    if ! run_patch_helper remove; then
        echo "[FAILED] the Klipper patches were not removed."
        return 1
    fi
    echo "[OK]"
}

# Uninstall step: remove the patch series only when it is applied. Refuses (and
# stops the uninstall, so nothing is half-removed) if the patched files were edited.
remove_klipper_patches_if_applied()
{
    if ! klipper_source_checkout; then
        return 0
    fi
    local status_rc=0
    run_patch_helper status >/dev/null 2>&1 || status_rc=$?
    if [ "$status_rc" = "10" ]; then
        echo "No Klipper patches are applied; nothing to remove."
        return 0
    fi
    remove_klipper_patches
}

# Restart Moonraker service
restart_moonraker()
{
    echo -n "Restarting Moonraker... "
    service restart moonraker
    MOONRAKER_RESTARTED=1
    echo "[OK]"
}

# ---------------------------------------------------------------------------
# Spoolman. Part of the default install: the openams_spoolman Moonraker
# component needs a Spoolman server, and the UIs read spool colors and
# materials through Moonraker's [spoolman] proxy.
# ---------------------------------------------------------------------------

# Print the server: value of moonraker.conf's [spoolman] section (empty when
# the section or the option is missing).
moonraker_spoolman_server()
{
    local cfg="${MOONRAKER_CONFIG_DIR}/moonraker.conf"
    [ -f "$cfg" ] || return 0
    awk '
        /^\[/ { in_s = ($0 ~ /^\[spoolman\][ \t]*$/); next }
        in_s && /^server[ \t]*[:=]/ {
            sub(/^server[ \t]*[:=][ \t]*/, ""); sub(/[ \t]+$/, ""); print; exit
        }
    ' "$cfg"
}

moonraker_has_section()
{
    grep -Eq "^\\[$1\\][[:space:]]*\$" "${MOONRAKER_CONFIG_DIR}/moonraker.conf" 2>/dev/null
}

spoolman_answers()
{
    curl -fsS --max-time 5 -o /dev/null "$SPOOLMAN_INFO_URL" >/dev/null 2>&1
}

# True when a [spoolman] server URL is the Spoolman this installer puts on
# this host (localhost or 127.0.0.1, its port).
spoolman_is_this_host()
{
    case "${1%/}" in
        "http://localhost:${SPOOLMAN_PORT}"|"http://127.0.0.1:${SPOOLMAN_PORT}") return 0;;
    esac
    return 1
}

# Spoolman's own installer needs pg_config and, when it is missing, runs
# "sudo apt-get install -y libpq-dev" itself. On a Debian release whose security
# pocket was retired (Debian 11 after end of support, still common on Klipper
# hosts) apt still lists libpq-dev, but every mirror answers 404 for the .deb in
# the pool, so that install fails. libpq-dev must match the installed libpq5
# exactly, and only that exact version is kept by snapshot.debian.org, so fetch
# the .deb from there, check its SHA-1 and install that.
# OPENAMS_SNAPSHOT_BASE is the snapshot base URL.
# Never fails its caller: anything that goes wrong here is a [WARNING], and
# Spoolman's own installer then reports what it is still missing.
ensure_pg_config()
{
    command -v pg_config > /dev/null 2>&1 && return 0
    # Not a Debian-family host; leave every other distribution to Spoolman.
    if ! command -v apt-get > /dev/null 2>&1 || ! command -v dpkg-query > /dev/null 2>&1; then
        return 0
    fi
    if sudo apt-get install -y libpq-dev; then
        echo "[OK] Installed libpq-dev."
        return 0
    fi

    local base version arch hash deb tmp got rc=0
    base="${OPENAMS_SNAPSHOT_BASE:-https://snapshot.debian.org}"
    version="$(dpkg-query -W -f='${Version}' libpq5 2> /dev/null)" || version=""
    if [ -z "$version" ]; then
        echo "[WARNING] apt could not install libpq-dev and libpq5 is not installed, so there is no matching version on ${base}."
        return 0
    fi
    arch="$(dpkg --print-architecture 2> /dev/null)" || arch=""
    if [ -z "$arch" ]; then
        echo "[WARNING] dpkg --print-architecture failed; not installing libpq-dev ${version}."
        return 0
    fi
    # The API answers {"result": [{"architecture": ..., "hash": ...}, ...]};
    # the version's "+" has to be percent-encoded in that URL.
    hash="$(curl -fsSL "${base}/mr/binary/libpq-dev/$(python3 -c 'import sys, urllib.parse; print(urllib.parse.quote(sys.argv[1]))' "$version")/binfiles" | python3 -c '
import json, sys
try:
    entries = json.load(sys.stdin)["result"]
except (KeyError, TypeError, ValueError):
    sys.exit(1)
for entry in entries:
    if entry.get("architecture") == sys.argv[1]:
        print(entry.get("hash", ""))
        sys.exit(0)
sys.exit(1)
' "$arch")" || hash=""
    if [ -z "$hash" ]; then
        echo "[WARNING] ${base} has no libpq-dev ${version} for ${arch}; Spoolman's installer will report what is still missing."
        return 0
    fi
    tmp="$(mktemp -d)" || tmp=""
    if [ -z "$tmp" ]; then
        echo "[WARNING] Could not make a temporary directory for the libpq-dev ${version} download."
        return 0
    fi
    # apt takes the version and the architecture from the file name.
    deb="libpq-dev_${version}_${arch}.deb"
    if ! curl -fsSL -o "${tmp}/${deb}" "${base}/file/${hash}"; then
        rm -rf "$tmp"
        echo "[WARNING] Could not download libpq-dev ${version} for ${arch} from ${base}."
        return 0
    fi
    got="$(sha1sum "${tmp}/${deb}" | cut -d " " -f 1)" || got=""
    if [ "$got" != "$hash" ]; then
        rm -rf "$tmp"
        echo "[WARNING] The libpq-dev ${version} downloaded from ${base} has SHA-1 ${got}, not the ${hash} the archive lists; nothing was installed."
        return 0
    fi
    (cd "$tmp" && sudo apt-get install -y "./${deb}") || rc=$?
    rm -rf "$tmp"
    if [ "$rc" = "0" ]; then
        echo "[OK] Installed libpq-dev ${version} for ${arch} from ${base}, whose pool no longer has it."
    else
        echo "[WARNING] Could not install the libpq-dev ${version} downloaded from ${base}."
    fi
    return 0
}

# Install Spoolman on this host with its own release installer
# (scripts/install.sh in the release zip): download the latest release zip,
# unpack it into SPOOLMAN_DIR and run the installer with -systemd=yes, which
# answers its only prompt (the systemd service, named Spoolman, port 7912).
# The installer also prompts when run as root; verify_ready already refuses
# root. It calls sudo, so the user needs passwordless sudo or a terminal.
install_spoolman_server()
{
    if [ -n "${OPENAMS_SPOOLMAN_INSTALLER:-}" ]; then
        bash "$OPENAMS_SPOOLMAN_INSTALLER" < /dev/null
        return $?
    fi
    if [ "$NO_SERVICE" = "1" ]; then
        echo "[skipped: OPENAMS_NO_SERVICE=1] install Spoolman into ${SPOOLMAN_DIR}"
        return 2
    fi
    local tool zip url
    for tool in curl unzip; do
        if ! command -v "$tool" > /dev/null 2>&1; then
            echo "[WARNING] \"$tool\" is needed to install Spoolman and was not found."
            return 1
        fi
    done
    zip="$(mktemp)"
    url="$(curl -fsSL https://api.github.com/repos/Donkie/Spoolman/releases/latest \
        | grep -o 'https://[^"]*spoolman.zip' | head -n 1)" || url=""
    if [ -z "$url" ] || ! curl -fsSL -o "$zip" "$url"; then
        rm -f "$zip"
        echo "[WARNING] Could not download the latest Spoolman release."
        return 1
    fi
    mkdir -p "$SPOOLMAN_DIR"
    if ! unzip -qo "$zip" -d "$SPOOLMAN_DIR"; then
        rm -f "$zip"
        echo "[WARNING] Could not unpack the Spoolman release into ${SPOOLMAN_DIR}."
        return 1
    fi
    rm -f "$zip"
    # A Spoolman that never started usually has a broken Python environment
    # (wrong-architecture build); it is rebuilt, the data is kept elsewhere.
    if [ "${SPOOLMAN_REINSTALL:-0}" = "1" ]; then
        rm -rf "${SPOOLMAN_DIR}/.venv"
    fi
    ensure_pg_config
    # Spoolman's installer lets uv pick the newest Python. Pin one for which
    # every Spoolman dependency ships ready-made builds, 32-bit ARM included:
    # with a brand-new Python, pydantic-core is compiled on the printer, and a
    # 32-bit Raspberry Pi OS on a 64-bit kernel compiles it for the wrong
    # architecture. A UV_PYTHON the user set still wins.
    (cd "$SPOOLMAN_DIR" && UV_PYTHON="${UV_PYTHON:-3.12}" \
        bash ./scripts/install.sh -systemd=yes < /dev/null)
}

# Return codes of install_spoolman_server: 0 installed, 1 failed, 2 skipped on purpose
# (OPENAMS_NO_SERVICE=1).
# Decide which Spoolman server OpenAMS uses, installing one when none exists.
# Sets SPOOLMAN_SERVER (empty: nothing to configure). Runs before Klipper is
# stopped, because an install can take minutes. A failure here is only a
# warning: the printer keeps working, without spool colors and materials.
resolve_spoolman()
{
    SPOOLMAN_SERVER=""
    if [ "$NO_SPOOLMAN" = "1" ]; then
        echo "Spoolman setup skipped (--no-spoolman). To enable it later, run ./install.sh again without that flag, or with --spoolman-url <url>. [SKIPPED]"
        return 0
    fi
    local existing
    existing="$(moonraker_spoolman_server)"
    # This host's own Spoolman, configured by an earlier run but not
    # answering (an install that failed), is installed again; any other
    # configured server is kept as it is.
    if [ -n "$existing" ] && [ -z "$SPOOLMAN_URL_ARG" ] \
            && spoolman_is_this_host "$existing" && ! spoolman_answers; then
        echo "moonraker.conf names Spoolman on this host ($existing), but it does not answer; installing it again..."
        SPOOLMAN_REINSTALL=1
    elif [ -n "$existing" ]; then
        SPOOLMAN_SERVER="$existing"
        if [ -n "$SPOOLMAN_URL_ARG" ] && [ "${SPOOLMAN_URL_ARG%/}" != "${existing%/}" ]; then
            echo "[WARNING] moonraker.conf already has [spoolman] server: $existing; keeping it and ignoring --spoolman-url $SPOOLMAN_URL_ARG."
        else
            echo "Using the Spoolman server from moonraker.conf: $existing [SKIPPED install]"
        fi
        return 0
    fi
    if [ -n "$SPOOLMAN_URL_ARG" ]; then
        SPOOLMAN_SERVER="${SPOOLMAN_URL_ARG%/}"
        echo "Using the Spoolman server given with --spoolman-url: $SPOOLMAN_SERVER [SKIPPED install]"
        return 0
    fi
    if spoolman_answers; then
        SPOOLMAN_SERVER="http://localhost:${SPOOLMAN_PORT}"
        echo "Spoolman already answers on this host at $SPOOLMAN_SERVER [SKIPPED install]"
        return 0
    fi
    [ "${SPOOLMAN_REINSTALL:-0}" = "1" ] || \
        echo "No Spoolman server found; installing Spoolman on this host (port ${SPOOLMAN_PORT})..."
    local rc=0
    install_spoolman_server || rc=$?
    if [ "$rc" = "2" ]; then
        return 0
    elif [ "$rc" != "0" ]; then
        echo "[WARNING] Spoolman was not installed, so spool colors and materials will not show. Install it yourself and run ./install.sh --spoolman-url <url>, or see https://github.com/Donkie/Spoolman."
        return 0
    fi
    SPOOLMAN_SERVER="http://localhost:${SPOOLMAN_PORT}"
    local waited=0
    until spoolman_answers; do
        if [ "$waited" -ge "$SPOOLMAN_WAIT" ]; then
            echo "[WARNING] Spoolman is installed but does not answer at $SPOOLMAN_INFO_URL yet; OpenAMS is configured for it anyway."
            return 0
        fi
        sleep 2
        waited=$((waited + 2))
    done
    echo "Spoolman installed and answering at $SPOOLMAN_SERVER [OK]"
}

# Write [spoolman] server: and [openams_spoolman] into moonraker.conf, each
# only when missing, then restart Moonraker. Backs the file up and restores it
# if the write fails.
configure_spoolman()
{
    [ -n "$SPOOLMAN_SERVER" ] || return 0
    echo -n "Configuring Spoolman in moonraker.conf... "
    local cfg="${MOONRAKER_CONFIG_DIR}/moonraker.conf"
    if [ ! -f "$cfg" ]; then
        echo "[SKIPPED] moonraker.conf not found."
        return 0
    fi
    local need_server=0 need_section=0 need_component=0
    if [ -z "$(moonraker_spoolman_server)" ]; then
        need_server=1
        moonraker_has_section spoolman && need_section=0 || need_section=1
    fi
    moonraker_has_section openams_spoolman || need_component=1
    if [ "$need_component" = "1" ] && [ ! -e "${MOONRAKER_PATH}/moonraker/components/openams_spoolman" ]; then
        echo "[WARNING] the openams_spoolman component is not linked into Moonraker (no checkout at \"${MOONRAKER_PATH}\"); not adding [openams_spoolman]."
        return 0
    fi
    if [ "$need_server" = "0" ] && [ "$need_component" = "0" ]; then
        echo "already configured [SKIPPED]"
        return 0
    fi

    local bak_cfg tmp_cfg
    bak_cfg="$(mktemp)"
    tmp_cfg="$(mktemp)"
    cp -p "$cfg" "$bak_cfg"
    local rc=0
    {
        if [ "$need_server" = "1" ]; then
            if [ "$need_section" = "1" ]; then
                { echo ""; echo "[spoolman]"; echo "server: ${SPOOLMAN_SERVER}"; } >> "$cfg" || rc=1
            else
                # A [spoolman] section without server: gets the option inserted.
                awk -v server="$SPOOLMAN_SERVER" '
                    { print }
                    /^\[spoolman\][ \t]*$/ { print "server: " server }
                ' "$cfg" > "$tmp_cfg" && cat "$tmp_cfg" > "$cfg" || rc=1
            fi
        fi
        if [ "$rc" = "0" ] && [ "${OPENAMS_SPOOLMAN_FAIL_WRITE:-0}" = "1" ]; then
            rc=1
        fi
        if [ "$rc" = "0" ] && [ "$need_component" = "1" ]; then
            { echo ""; echo "[openams_spoolman]"; } >> "$cfg" || rc=1
        fi
    }
    if [ "$rc" != "0" ]; then
        cat "$bak_cfg" > "$cfg"
        rm -f "$tmp_cfg" "$bak_cfg"
        echo "[ERROR] Could not write moonraker.conf; it was restored from its backup." >&2
        return 1
    fi
    rm -f "$tmp_cfg" "$bak_cfg"
    echo "[OK]"
    restart_moonraker
}

# Uninstall: remove only [openams_spoolman]. Spoolman, its data and [spoolman]
# are never touched; they hold the spool database and may serve other tools.
remove_spoolman_section()
{
    echo -n "Removing [openams_spoolman] from moonraker.conf... "
    local cfg="${MOONRAKER_CONFIG_DIR}/moonraker.conf"
    if [ ! -f "$cfg" ]; then
        echo "[SKIPPED] moonraker.conf not found."
    elif ! moonraker_has_section openams_spoolman; then
        echo "not present [SKIPPED]"
    else
        local bak_cfg tmp_cfg
        bak_cfg="$(mktemp)"
        tmp_cfg="$(mktemp)"
        cp -p "$cfg" "$bak_cfg"
        awk '
            /^\[openams_spoolman\][ \t]*$/ { skip=1; next }
            /^\[/ { skip=0 }
            !skip { print }
        ' "$cfg" > "$tmp_cfg" || { rm -f "$tmp_cfg" "$bak_cfg"; return 1; }
        cat "$tmp_cfg" > "$cfg" || { cat "$bak_cfg" > "$cfg"; rm -f "$tmp_cfg" "$bak_cfg"; return 1; }
        rm -f "$tmp_cfg" "$bak_cfg"
        echo "[OK]"
        restart_moonraker
    fi
    echo "Spoolman, its data and the [spoolman] section of moonraker.conf were left in place; remove them yourself if you no longer want Spoolman."
}

# Migrate [update_manager openams] from klipper_openams to openams.
# Rewrites the section in place when its path or origin still references the
# old klipper_openams repository.  Idempotent: if the section already has the
# correct values, or does not exist, nothing is changed.
migrate_updater()
{
    local moonraker_cfg="${MOONRAKER_CONFIG_DIR}/moonraker.conf"
    [[ -f "$moonraker_cfg" ]] || return 0

    # Collect the existing [update_manager openams] section (if any)
    local old_section
    old_section="$(awk '
        /^\[update_manager openams\]$/ { in_section=1; print; next }
        /^\[/ { if (in_section) in_section=0 }
        in_section { print }
    ' "$moonraker_cfg")"

    [[ -n "$old_section" ]] || return 0  # section does not exist

    # Only rewrite if the section still references the old klipper_openams repo
    if ! echo "$old_section" | grep -qi "klipper_openams"; then
        return 0  # already up to date
    fi

    echo "Migrating [update_manager openams] from klipper_openams to openams..."
    echo "  Old section:"
    echo "$old_section" | sed 's/^/    /'

    # Remove the old section
    local tmp_cfg bak_cfg
    tmp_cfg="$(mktemp)"
    bak_cfg="$(mktemp)"
    cp -p "$moonraker_cfg" "$bak_cfg"
    awk '
        /^\[update_manager openams\]$/ { skip=1; next }
        /^\[/ { if (skip) skip=0 }
        !skip { print }
    ' "$moonraker_cfg" > "$tmp_cfg" || { rm -f "$tmp_cfg" "$bak_cfg"; return 1; }
    cat "$tmp_cfg" > "$moonraker_cfg" || { cp -p "$bak_cfg" "$moonraker_cfg"; rm -f "$tmp_cfg" "$bak_cfg"; return 1; }
    rm -f "$tmp_cfg" "$bak_cfg"

    # Write the new section.  The path field is expanded to the real checkout
    # location (SCRIPT_DIR) rather than the placeholder ~/openams from the
    # template, so non-standard install paths are handled correctly.
    local new_section
    new_section="$(sed "s|path: ~/openams|path: ${SCRIPT_DIR}|" "${SCRIPT_DIR}/file_templates/moonraker_update.txt")"
    {
        echo ""
        echo "$new_section"
        echo ""
    } >> "$moonraker_cfg"

    echo "  New section:"
    echo "$new_section" | sed 's/^/    /'
    echo "  Migration complete [OK]"
    restart_moonraker
}

# Remove symlinks in klippy/extras and scripts that point into an old
# klipper_openams checkout and have no matching file in our src/ or scripts/.
# Files that still exist in src/ or scripts/ will be re-pointed by
# link_extension()/link_scripts(); only truly orphaned symlinks are removed.
# Symlinks that do not reference klipper_openams are never touched.
remove_stale_klipper_openams_symlinks()
{
    echo -n "Checking for stale klipper_openams symlinks... "
    local removed=0 count=0 link target basename

    while IFS= read -r link; do
        [[ -L "$link" ]] || continue
        target="$(readlink "$link")"
        [[ "$target" == *klipper_openams* ]] || continue
        count=$((count + 1))
        basename="$(basename "$link")"
        # If our src/ still has this file, link_extension() will re-point it
        [[ -f "${SRCDIR}/${basename}" ]] && continue
        # File is gone from src/ - remove the stale symlink
        rm "$link"
        printf '\n  Removed stale symlink: %s -> %s\n' "$link" "$target"
        removed=$((removed + 1))
    done < <(find "${KLIPPER_PATH}/klippy/extras" -maxdepth 1 -name '*.py' -type l 2>/dev/null)

    while IFS= read -r link; do
        [[ -L "$link" ]] || continue
        target="$(readlink "$link")"
        [[ "$target" == *klipper_openams* ]] || continue
        count=$((count + 1))
        basename="$(basename "$link")"
        [[ -f "${SCRIPTSDIR}/${basename}" ]] && continue
        rm "$link"
        printf '\n  Removed stale symlink: %s -> %s\n' "$link" "$target"
        removed=$((removed + 1))
    done < <(find "${KLIPPER_PATH}/scripts" -maxdepth 1 -name '*.py' -type l 2>/dev/null)

    if [[ $count -eq 0 ]]; then
        echo "[NONE]"
    elif [[ $removed -eq 0 ]]; then
        echo "[OK - will be re-pointed]"
    else
        echo "[OK] $removed removed"
    fi
}

# Add OpenAMS update manager section to moonraker.conf.
# Uses the real checkout location (SCRIPT_DIR) for the path field, so
# non-standard install paths (not ~/openams) are handled correctly.
add_updater()
{
    echo -e -n "Adding update manager to moonraker.conf... "

    if grep -q '\[update_manager openams\]' "${MOONRAKER_CONFIG_DIR}/moonraker.conf"; then
        echo "[update_manager openams] already exists in moonraker.conf [SKIPPED]"
        return
    fi

    {
        echo ""
        sed "s|path: ~/openams|path: ${SCRIPT_DIR}|" "${SCRIPT_DIR}/file_templates/moonraker_update.txt"
        echo ""
    } >> "${MOONRAKER_CONFIG_DIR}/moonraker.conf"

    echo "[OK]"
    restart_moonraker
}

# True when the file's content is a version of $2 that OpenAMS shipped: the
# current one, or (in a git checkout) any version in the history of that path.
is_shipped_macro_version()
{
    local file="$1" name="$2" hash commit
    hash="$(git hash-object "$file" 2>/dev/null)" || return 1
    [ -n "$hash" ] || return 1
    [ "$hash" = "$(git hash-object "${SCRIPT_DIR}/${name}" 2>/dev/null)" ] && return 0
    git -C "$SCRIPT_DIR" rev-parse --git-dir >/dev/null 2>&1 || return 1
    while IFS= read -r commit; do
        [ "$(git -C "$SCRIPT_DIR" rev-parse "${commit}:${name}" 2>/dev/null)" = "$hash" ] && return 0
    done < <(git -C "$SCRIPT_DIR" log --format=%H -- "$name" 2>/dev/null)
    return 1
}

# Install a shipped macro file into the config directory as a symlink to the
# checkout's file. An existing copy is migrated; user content is never deleted.
link_shipped_macro()
{
    local name="$1"
    local dest="${MOONRAKER_CONFIG_DIR}/${name}"
    local src="${SCRIPT_DIR}/${name}"
    local backup

    if [ -L "$dest" ]; then
        if [ "$(readlink "$dest")" = "$src" ]; then
            echo "  ${name}: already linked [OK]"
            return
        fi
        ln -sfn "$src" "$dest"
        echo "  ${name}: was a link to somewhere else; now linked to ${src} [OK]"
    elif [ -e "$dest" ]; then
        if is_shipped_macro_version "$dest" "$name"; then
            rm -f "$dest"
            ln -s "$src" "$dest"
            echo "  ${name}: was an unmodified copy of a shipped version; replaced by a link to ${src}, so updates now take effect [OK]"
        else
            backup="${dest}.bak-$(date +%Y%m%d-%H%M%S)"
            mv "$dest" "$backup"
            ln -s "$src" "$dest"
            echo "  ${name}: this file was customized. It was kept as ${backup}"
            echo "    and ${name} is now a link to ${src}, so updates take effect."
            echo "    Customizations belong in your own file (for example oams.cfg), not in ${name}:"
            echo "    use the hook macros _OAMS_HOOK_BEFORE_HEAT, _OAMS_HOOK_AFTER_CUT,"
            echo "    _OAMS_HOOK_AFTER_RETRACT and _OAMS_HOOK_AFTER_FEED, OAMS_CUT for the cutter,"
            echo "    and CLEAN_NOZZLE. See \"Customizing the toolchange\" in docs/guide/configure.md."
            echo "    Move what you changed from the .bak file into those macros."
        fi
    else
        ln -s "$src" "$dest"
        echo "  ${name}: linked to ${src} [OK]"
    fi
}

# Install OpenAMS config files into the Moonraker configuration directory
install_config()
{
    echo -n "Installing OpenAMS config files... "

    if [ ! -f "${MOONRAKER_CONFIG_DIR}/oams.cfg" ]; then
        # A Cortex Bridge printer starts from its own sample: its AMS units
        # are added by configure_cortex_bridge_units, not by hand.
        if [ "$CORTEX_BRIDGE_INSTALLED" = "1" ]; then
            cp "${SCRIPT_DIR}/oams_cortex_bridge_sample.cfg" "${MOONRAKER_CONFIG_DIR}/oams.cfg"
        else
            cp "${SCRIPT_DIR}/oams_sample.cfg" "${MOONRAKER_CONFIG_DIR}/oams.cfg"
        fi
        echo -n "oams.cfg installed... "
    else
        echo -n "oams.cfg already exists [SKIPPED]... "
    fi

    # The shipped macros are linked, not copied, so an update takes effect.
    # Customizations belong in the owner's own file, through the hook macros.
    echo
    link_shipped_macro "oams_macros.cfg"
    # Optional gco-routines opt-in. Linking it enables nothing: it only takes
    # effect when included after [gco_routines] is added (see README).
    link_shipped_macro "oams_macros_ordered.cfg"

    echo "[OK]"
}

# Add OpenAMS config include to printer.cfg
add_printer_includes()
{
    echo -n "Adding OpenAMS include to printer.cfg... "
    local printer_cfg="${MOONRAKER_CONFIG_DIR}/printer.cfg"
    local tmp_cfg

    if [ ! -f "$printer_cfg" ]; then
        echo "[SKIPPED] printer.cfg not found."
        return
    fi

    if grep -q '^\[include[[:space:]]*oams\.cfg\]$' "$printer_cfg"; then
        echo "oams.cfg already included [SKIPPED]"
        return
    fi

    local bak_cfg
    tmp_cfg="$(mktemp)"
    bak_cfg="$(mktemp)"
    cp -p "$printer_cfg" "$bak_cfg"

    if grep -q '^\[printer\]$' "$printer_cfg"; then
        awk '
        !inserted && /^\[printer\]$/ {
            print "[include oams.cfg]"
            print ""
            inserted=1
        }
        { print }
        ' "$printer_cfg" > "$tmp_cfg" || { rm -f "$tmp_cfg" "$bak_cfg"; return 1; }
    else
        {
            echo "[include oams.cfg]"
            echo ""
            cat "$printer_cfg"
        } > "$tmp_cfg" || { rm -f "$tmp_cfg" "$bak_cfg"; return 1; }
    fi

    cat "$tmp_cfg" > "$printer_cfg" || { cp -p "$bak_cfg" "$printer_cfg"; rm -f "$tmp_cfg" "$bak_cfg"; return 1; }
    rm -f "$tmp_cfg" "$bak_cfg"
    echo "[OK]"
}

# Remove OpenAMS config include from printer.cfg without deleting nearby config
remove_printer_includes()
{
    echo -n "Removing OpenAMS includes from printer.cfg... "
    local printer_cfg="${MOONRAKER_CONFIG_DIR}/printer.cfg"
    local tmp_cfg

    if [ ! -f "$printer_cfg" ]; then
        echo "[SKIPPED] printer.cfg not found."
        return
    fi

    local bak_cfg
    tmp_cfg="$(mktemp)"
    bak_cfg="$(mktemp)"
    cp -p "$printer_cfg" "$bak_cfg"

    awk '
    BEGIN { skip_blank=0 }
    /^\[include[[:space:]]*oams\.cfg\]$/ {
        skip_blank=1
        next
    }
    skip_blank && /^$/ {
        skip_blank=0
        next
    }
    {
        skip_blank=0
        print
    }
    ' "$printer_cfg" > "$tmp_cfg" || { rm -f "$tmp_cfg" "$bak_cfg"; return 1; }

    cat "$tmp_cfg" > "$printer_cfg" || { cp -p "$bak_cfg" "$printer_cfg"; rm -f "$tmp_cfg" "$bak_cfg"; return 1; }
    rm -f "$tmp_cfg" "$bak_cfg"
    echo "[OK]"
}

# Remove OpenAMS update manager section from moonraker.conf
remove_updater()
{
    echo -n "Removing OpenAMS update manager from moonraker.conf... "
    local moonraker_cfg="${MOONRAKER_CONFIG_DIR}/moonraker.conf"
    local tmp_cfg

    if [ ! -f "$moonraker_cfg" ]; then
        echo "[SKIPPED] moonraker.conf not found."
        return
    fi

    local bak_cfg
    tmp_cfg="$(mktemp)"
    bak_cfg="$(mktemp)"
    cp -p "$moonraker_cfg" "$bak_cfg"

    awk '
    BEGIN { skip=0 }
    /^\[update_manager openams\]$/ { skip=1; next }
    /^\[/ { if (skip) skip=0 }
    !skip { print }
    ' "$moonraker_cfg" > "$tmp_cfg" || { rm -f "$tmp_cfg" "$bak_cfg"; return 1; }

    cat "$tmp_cfg" > "$moonraker_cfg" || { cp -p "$bak_cfg" "$moonraker_cfg"; rm -f "$tmp_cfg" "$bak_cfg"; return 1; }
    rm -f "$tmp_cfg" "$bak_cfg"

    echo "[OK]"
    restart_moonraker
}


restart_klipper()
{
    echo -n "Restarting Klipper... "
    service restart "$KLIPPER_SERVICE_NAME"
    echo "[OK]"
}

start_klipper()
{
    echo -n "Starting Klipper... "
    service start "$KLIPPER_SERVICE_NAME" || return $?
    echo "[OK]"
}

stop_klipper()
{
    echo -n "Stopping Klipper... "
    service stop "$KLIPPER_SERVICE_NAME"
    echo "[OK]"
}

uninstall()
{
    if [ -f "${KLIPPER_PATH}/klippy/extras/oams.py" ]; then
        echo -n "Uninstalling OpenAMS... "
        for file in "${SRCDIR}"/*.py; do
            # Never unlink the add-on's own extra name.
            case "$(basename "$file")" in
                klipper_mmu*) continue;;
            esac
            rm -f "${KLIPPER_PATH}/klippy/extras/$(basename "$file")"
        done
        for file in "${SCRIPTSDIR}"/*.py; do
            rm -f "${KLIPPER_PATH}/scripts/$(basename "$file")"
        done
        echo "[OK]"
        read -p "Remove oams.cfg, oams_macros.cfg and oams_macros_ordered.cfg from ${MOONRAKER_CONFIG_DIR}? [y/N] " confirm
        if [[ "$confirm" =~ ^[Yy]$ ]]; then
            rm -f "${MOONRAKER_CONFIG_DIR}/oams.cfg"
            rm -f "${MOONRAKER_CONFIG_DIR}/oams_macros.cfg"
            rm -f "${MOONRAKER_CONFIG_DIR}/oams_macros_ordered.cfg"
            echo "Config files removed."
        else
            echo "Config files kept."
        fi
    else
        echo "oams.py not found in \"${KLIPPER_PATH}/klippy/extras/\". Is it installed?"
        echo "[FAILED]"
        return 1
    fi
}

# Helper functions
verify_ready()
{
    if [ "$EUID" -eq 0 ]; then
        echo "[ERROR] This script must not run as root. Exiting."
        exit -1
    fi
}

# Run steps
verify_ready
check_klipper
if [ "$REMOVE_KLIPPER_PATCHES" = "1" ]; then
    # A stand-alone action: nothing else is installed or removed.
    stop_klipper
    PATCHES_RC=0
    remove_klipper_patches || PATCHES_RC=$?
    start_klipper
    exit "$PATCHES_RC"
fi
check_folders
# Network/ownership checks must succeed before we interrupt the service.
if [[ -z "${UNINSTALL:-}" ]]; then
    prepare_gco_routines
    resolve_spoolman
fi
KLIPPER_STOPPED=0
restore_klipper_on_exit()
{
    local result=$?
    if [[ "$KLIPPER_STOPPED" == 1 ]]; then
        echo "Installation failed; attempting to restore the Klipper service."
        start_klipper || echo "[ERROR] Could not start Klipper; inspect its service logs." >&2
    fi
    return "$result"
}
trap restore_klipper_on_exit EXIT
stop_klipper
KLIPPER_STOPPED=1
if [[ -z "${UNINSTALL:-}" ]]; then
    install_gco_routines
    migrate_updater
    remove_stale_klipper_openams_symlinks
    link_extension
    link_moonraker_component
    install_cortex_bridge
    link_klipper_mmu_host
    if [ "$WITH_MCU_ADDON" = "1" ]; then
        install_klipper_mmu_mcu
    fi
    if [ "$WITH_KLIPPER_PATCHES" = "1" ]; then
        install_klipper_patches
    fi
    link_scripts
    add_updater
    if [ "$CORTEX_BRIDGE_INSTALLED" = "1" ]; then
        cortex_bridge_updater_services
    fi
    install_config
    add_printer_includes
    configure_cortex_bridge_units
    configure_spoolman
    if [ "${MAINSAIL_PANEL:-0}" = "1" ]; then
        install_mainsail_panel
    fi
    if [ "${MOONRAKER_COMPONENT_CHANGED:-0}" = "1" ] && [ "${MOONRAKER_RESTARTED:-0}" != "1" ]; then
        restart_moonraker
    fi
else
    # First, so a failure in the OpenAMS unlink below cannot leave the add-on
    # behind; both steps are idempotent and report absence, not an error.
    remove_klipper_patches_if_applied
    remove_klipper_mmu
    remove_cortex_bridge
    uninstall
    unlink_moonraker_component
    remove_printer_includes
    remove_updater
    remove_spoolman_section
    remove_mainsail_panel
fi
KLIPPER_STOPPED=0
start_klipper
