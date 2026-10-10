# Cortex Bridge relay. Sourced by install.sh.
#
# A Cortex Bridge connects stock AMS units to Klipper over USB. Its relay
# service gives Klipper one serial port per AMS (/dev/oams/<uid>). This step
# installs that service, its udev rule and /dev/oams, the Moonraker component
# that lets Mainsail update the bridge's firmware, and, on a fresh printer,
# writes oams.cfg for the connected AMS units.
#
# It runs when a bridge is plugged in, when the relay is already installed,
# or with --cortex-bridge; --no-cortex-bridge skips it.
#
# Documented test switches: OPENAMS_SYSTEM_ROOT=<dir> writes the system files
# under <dir> instead of / and runs no sudo; OPENAMS_SYSFS_USB=<dir> is the
# sysfs USB device folder to look for a bridge in; OPENAMS_BRIDGE_PYTHON is
# the Python the relay runs with.

CORTEX_BRIDGE_MODE="${CORTEX_BRIDGE_MODE:-auto}"
CORTEX_BRIDGE_UNIT="cortex-bridge"
CORTEX_BRIDGE_INSTALLED=0
CORTEX_BRIDGE_TEMPLATES="${SCRIPT_DIR}/file_templates/cortex_bridge"
SYSTEM_ROOT="${OPENAMS_SYSTEM_ROOT:-}"
SYSFS_USB="${OPENAMS_SYSFS_USB:-/sys/bus/usb/devices}"

cortex_bridge_unit_path()
{
    echo "${SYSTEM_ROOT}/etc/systemd/system/${CORTEX_BRIDGE_UNIT}.service"
}

cortex_bridge_dropin_path()
{
    echo "${SYSTEM_ROOT}/etc/systemd/system/${KLIPPER_SERVICE_NAME}.service.d/cortex-bridge.conf"
}

# Run a command as root. In a test layout it is printed instead.
privileged()
{
    if [ "$NO_SERVICE" = "1" ] || [ -n "$SYSTEM_ROOT" ]; then
        echo "[skipped: test mode] sudo $*"
        return 0
    fi
    sudo "$@"
}

# Copy a rendered file into place as root (or under SYSTEM_ROOT in tests).
# Without systemd (OPENAMS_NO_SERVICE=1) and without a SYSTEM_ROOT, nothing
# outside the printer's own folders is ever written.
place_system_file()
{
    local src="$1" dest="$2"
    if [ -n "$SYSTEM_ROOT" ]; then
        mkdir -p "$(dirname "$dest")"
        cp "$src" "$dest"
        chmod 644 "$dest"
    elif [ "$NO_SERVICE" = "1" ]; then
        echo "[skipped: OPENAMS_NO_SERVICE=1] install ${dest}"
    else
        sudo install -D -m 644 "$src" "$dest"
    fi
}

remove_system_file()
{
    local path="$1"
    [ -e "$path" ] || return 0
    if [ -n "$SYSTEM_ROOT" ]; then
        rm -f "$path"
    elif [ "$NO_SERVICE" = "1" ]; then
        echo "[skipped: OPENAMS_NO_SERVICE=1] remove ${path}"
    else
        sudo rm -f "$path"
    fi
}

# True when a Cortex Bridge is plugged in, running or waiting for firmware.
# A test run (OPENAMS_NO_SERVICE=1) looks only at OPENAMS_SYSFS_USB, never at
# the USB devices of the machine running the tests.
cortex_bridge_connected()
{
    local dev product
    if [ "$NO_SERVICE" = "1" ] && [ -z "${OPENAMS_SYSFS_USB:-}" ]; then
        return 1
    fi
    for dev in "$SYSFS_USB"/*; do
        [ -f "$dev/idVendor" ] || continue
        [ "$(cat "$dev/idVendor" 2>/dev/null)" = "1a86" ] || continue
        case "$(cat "$dev/idProduct" 2>/dev/null)" in
            fe11) return 0;;
            fe10)
                product="$(cat "$dev/product" 2>/dev/null)"
                case "$product" in *Updater|*Fault) return 0;; esac;;
        esac
    done
    return 1
}

# False in a test run without SYSTEM_ROOT: the host's own /etc is not ours.
system_files_visible()
{
    [ -n "$SYSTEM_ROOT" ] || [ "$NO_SERVICE" != "1" ]
}

cortex_bridge_wanted()
{
    case "$CORTEX_BRIDGE_MODE" in
        on) return 0;;
        off) return 1;;
    esac
    if system_files_visible && [ -f "$(cortex_bridge_unit_path)" ]; then
        return 0
    fi
    cortex_bridge_connected
}

# The Python the relay runs with: Klipper's own, which already has pyserial.
cortex_bridge_python()
{
    if [ -n "${OPENAMS_BRIDGE_PYTHON:-}" ]; then
        echo "$OPENAMS_BRIDGE_PYTHON"
        return 0
    fi
    local candidates=() py
    py="$(systemctl show -p ExecStart --value "${KLIPPER_SERVICE_NAME}.service" 2>/dev/null \
        | sed -n 's/.*path=\([^ ;]*\).*/\1/p' | head -n 1)"
    case "$py" in */python*) candidates+=("$py");; esac
    candidates+=("${HOME}/klippy-env/bin/python" "$(dirname "$KLIPPER_PATH")/klippy-env/bin/python" "/usr/bin/python3")
    for py in "${candidates[@]}"; do
        if [ -x "$py" ] && "$py" -c 'import serial' >/dev/null 2>&1; then
            echo "$py"
            return 0
        fi
    done
    if [ -x /usr/bin/python3 ]; then
        echo "Installing pyserial for the system Python (python3-serial)..." >&2
        privileged apt-get install -y python3-serial >&2 || return 1
        echo "/usr/bin/python3"
        return 0
    fi
    return 1
}

render_cortex_bridge_template()
{
    local src="$1" dest="$2" python="$3"
    local logs="${MOONRAKER_CONFIG_DIR%/config}/logs"
    [ -d "$logs" ] || logs="${HOME}"
    sed -e "s|@USER@|$(id -un)|g" \
        -e "s|@GROUP@|$(id -gn)|g" \
        -e "s|@OPENAMS@|${SCRIPT_DIR}|g" \
        -e "s|@PYTHON@|${python}|g" \
        -e "s|@LOG_FILE@|${logs}/cortex-bridge.jsonl|g" \
        -e "s|@KLIPPER_SERVICE@|${KLIPPER_SERVICE_NAME}|g" \
        "$src" > "$dest"
}

# Another relay holding the bridge would keep this one from starting.
warn_competing_gateway()
{
    local other
    system_files_visible || return 0
    for other in cortex-gateway cortex-bridge-gateway; do
        if [ -f "${SYSTEM_ROOT}/etc/systemd/system/${other}.service" ]; then
            echo "[WARNING] ${other}.service is also installed. Only one relay can hold the bridge;"
            echo "          if it serves this bridge, disable it: sudo systemctl disable --now ${other}"
        fi
    done
}

# Let Moonraker restart the relay (its update manager and the Machine page).
allow_moonraker_service()
{
    local asvc="${MOONRAKER_CONFIG_DIR%/config}/moonraker.asvc"
    [ -f "$asvc" ] || return 0
    if ! grep -qx "$CORTEX_BRIDGE_UNIT" "$asvc"; then
        echo "$CORTEX_BRIDGE_UNIT" >> "$asvc"
        echo "Allowed Moonraker to manage the ${CORTEX_BRIDGE_UNIT} service."
        MOONRAKER_COMPONENT_CHANGED=1
    fi
}

disallow_moonraker_service()
{
    local asvc="${MOONRAKER_CONFIG_DIR%/config}/moonraker.asvc"
    [ -f "$asvc" ] || return 0
    if grep -qx "$CORTEX_BRIDGE_UNIT" "$asvc"; then
        local tmp
        tmp="$(mktemp)"
        grep -vx "$CORTEX_BRIDGE_UNIT" "$asvc" > "$tmp"
        cat "$tmp" > "$asvc"
        rm -f "$tmp"
    fi
}

link_bridge_moonraker_component()
{
    local link="${MOONRAKER_PATH}/moonraker/components/openams_bridge"
    if [ ! -d "${MOONRAKER_PATH}/moonraker/components" ]; then
        echo "No Moonraker checkout found at \"${MOONRAKER_PATH}\"; the Mainsail panel cannot update the bridge."
        return 0
    fi
    if [ "$(readlink "$link" 2>/dev/null)" != "${SCRIPT_DIR}/moonraker/openams_bridge" ]; then
        MOONRAKER_COMPONENT_CHANGED=1
    fi
    ln -sfn "${SCRIPT_DIR}/moonraker/openams_bridge" "$link"
    local exclude="${MOONRAKER_PATH}/.git/info/exclude"
    if [ -d "${MOONRAKER_PATH}/.git/info" ] && \
            ! grep -qx "moonraker/components/openams_bridge" "$exclude" 2>/dev/null; then
        echo "moonraker/components/openams_bridge" >> "$exclude"
    fi
    local cfg="${MOONRAKER_CONFIG_DIR}/moonraker.conf"
    if [ -f "$cfg" ] && ! moonraker_has_section openams_bridge; then
        { echo ""; echo "[openams_bridge]"; } >> "$cfg"
        MOONRAKER_COMPONENT_CHANGED=1
    fi
}

unlink_bridge_moonraker_component()
{
    local link="${MOONRAKER_PATH}/moonraker/components/openams_bridge"
    [ -L "$link" ] && rm -f "$link"
    local cfg="${MOONRAKER_CONFIG_DIR}/moonraker.conf"
    if [ -f "$cfg" ] && moonraker_has_section openams_bridge; then
        local tmp
        tmp="$(mktemp)"
        awk '
            /^\[openams_bridge\][ \t]*$/ { skip=1; next }
            /^\[/ { skip=0 }
            !skip { print }
        ' "$cfg" > "$tmp" && cat "$tmp" > "$cfg"
        rm -f "$tmp"
    fi
}

# After an OpenAMS update, Moonraker restarts the relay before Klipper, so
# both run the new code.
cortex_bridge_updater_services()
{
    local cfg="${MOONRAKER_CONFIG_DIR}/moonraker.conf"
    [ -f "$cfg" ] || return 0
    moonraker_has_section "update_manager openams" || return 0
    local tmp
    tmp="$(mktemp)"
    awk -v unit="$CORTEX_BRIDGE_UNIT" '
        /^\[update_manager openams\][ \t]*$/ { inside=1; print; next }
        /^\[/ { inside=0 }
        inside && /^managed_services[ \t]*:/ && index($0, unit) == 0 {
            sub(/:[ \t]*/, ": " unit " ")
        }
        { print }
    ' "$cfg" > "$tmp"
    if ! cmp -s "$tmp" "$cfg"; then
        cat "$tmp" > "$cfg"
        echo "The OpenAMS update entry now restarts the bridge relay too."
        MOONRAKER_COMPONENT_CHANGED=1
    fi
    rm -f "$tmp"
}

install_cortex_bridge()
{
    if ! cortex_bridge_wanted; then
        echo "No Cortex Bridge connected; skipping its relay (use --cortex-bridge to install it anyway)."
        return 0
    fi
    echo "Installing the Cortex Bridge relay..."
    local python
    if ! python="$(cortex_bridge_python)"; then
        echo "[ERROR] No Python with pyserial was found for the bridge relay. Install it with: sudo apt-get install python3-serial" >&2
        return 1
    fi
    echo "  relay Python: ${python}"
    local tmp
    tmp="$(mktemp -d)"
    render_cortex_bridge_template "${CORTEX_BRIDGE_TEMPLATES}/cortex-bridge.service" "$tmp/unit" "$python"
    render_cortex_bridge_template "${CORTEX_BRIDGE_TEMPLATES}/klipper-cortex-bridge.conf" "$tmp/dropin" "$python"
    place_system_file "$tmp/unit" "$(cortex_bridge_unit_path)"
    place_system_file "$tmp/dropin" "$(cortex_bridge_dropin_path)"
    place_system_file "${CORTEX_BRIDGE_TEMPLATES}/99-cortex-bridge.rules" "${SYSTEM_ROOT}/etc/udev/rules.d/99-cortex-bridge.rules"
    place_system_file "${CORTEX_BRIDGE_TEMPLATES}/cortex-bridge.tmpfiles.conf" "${SYSTEM_ROOT}/etc/tmpfiles.d/cortex-bridge.conf"
    rm -rf "$tmp"

    if ! id -nG | tr ' ' '\n' | grep -qx dialout; then
        privileged usermod -aG dialout "$(id -un)"
    fi
    privileged systemd-tmpfiles --create /etc/tmpfiles.d/cortex-bridge.conf
    privileged udevadm control --reload-rules
    privileged udevadm trigger --subsystem-match=tty --action=change
    warn_competing_gateway

    service daemon-reload
    service enable "$CORTEX_BRIDGE_UNIT"
    # --no-block: the relay reports ready only once the AMS answers, and an
    # unpowered AMS must not stall the installer.
    service restart --no-block "$CORTEX_BRIDGE_UNIT"
    allow_moonraker_service
    link_bridge_moonraker_component
    CORTEX_BRIDGE_INSTALLED=1
    echo "Cortex Bridge relay installed [OK]"
}

# On a fresh printer, write the connected AMS units into oams.cfg. Never
# fatal: the command it runs can be repeated at any time.
configure_cortex_bridge_units()
{
    [ "$CORTEX_BRIDGE_INSTALLED" = "1" ] || return 0
    local tool=("python3" "${SCRIPT_DIR}/tools/cortex-bridge.py"
                --config-dir "${MOONRAKER_CONFIG_DIR}"
                --klipper-service "${KLIPPER_SERVICE_NAME}")
    if [ "$NO_SERVICE" = "1" ] || [ -n "$SYSTEM_ROOT" ]; then
        echo "[skipped: test mode] ${tool[*]} config --write --yes"
        return 0
    fi
    echo "Looking for AMS units behind the bridge (up to 45 s)..."
    if ! "${tool[@]}" config --write --yes --wait 45; then
        echo "[NOTE] No AMS was added to oams.cfg yet. When the AMS is connected and powered, run:"
        echo "       python3 ${SCRIPT_DIR}/tools/cortex-bridge.py config --write"
    fi
}

remove_cortex_bridge()
{
    if ! system_files_visible || [ ! -f "$(cortex_bridge_unit_path)" ]; then
        return 0
    fi
    echo -n "Removing the Cortex Bridge relay... "
    service disable --now "$CORTEX_BRIDGE_UNIT" || true
    remove_system_file "$(cortex_bridge_unit_path)"
    remove_system_file "$(cortex_bridge_dropin_path)"
    remove_system_file "${SYSTEM_ROOT}/etc/udev/rules.d/99-cortex-bridge.rules"
    remove_system_file "${SYSTEM_ROOT}/etc/tmpfiles.d/cortex-bridge.conf"
    service daemon-reload
    privileged udevadm control --reload-rules
    unlink_bridge_moonraker_component
    disallow_moonraker_service
    echo "[OK]"
}
