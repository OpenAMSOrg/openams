#!/bin/bash
# OpenAMS preflight for agents: what is on this printer host, read-only.
#
# Usage: bash tools/agent/preflight.sh [--url http://127.0.0.1:7125]
#            [--klipper-path DIR] [--config-dir DIR]
#   --klipper-path / --config-dir name the printer to look at on a host with
#   several (otherwise: the klipper service's directory, ~/printer_data/config).
#
# Prints one KEY=value per line (values may contain spaces; nothing is
# quoted), then zero or more STOP_REASON=... and ASK=... lines, and last
# VERDICT=OK, VERDICT=ASK or VERDICT=STOP. It changes nothing: it reads
# files, runs `systemctl show`/`ip`/`git` queries and HTTP GETs only.
# install.agents.md says what every key means and what to do with it.

URL=http://127.0.0.1:7125
KPATH_ARG=""
CFG_ARG=""
while [ $# -gt 0 ]; do
    case "$1" in
        --url) URL="$2"; shift 2;;
        --url=*) URL="${1#--url=}"; shift;;
        --klipper-path) KPATH_ARG="$2"; shift 2;;
        --config-dir) CFG_ARG="$2"; shift 2;;
        -h|--help) sed -n '2,10p' "$0"; exit 0;;
        *) echo "unknown argument: $1" >&2; exit 2;;
    esac
done

STOPS=()
ASKS=()
say() { printf '%s=%s\n' "$1" "$2"; }
have() { command -v "$1" > /dev/null 2>&1; }
http_get() { curl -s --max-time 5 "$URL$1" 2>/dev/null; }
# A JSON field by dotted path, or empty. python3 is on every Klipper host.
json_get() { python3 -c '
import json, sys
try:
    v = json.load(sys.stdin)
    for k in sys.argv[1].split("."):
        v = v[int(k)] if isinstance(v, list) else v[k]
    print(v if not isinstance(v, (dict, list)) else json.dumps(v))
except Exception:
    pass' "$1"; }

# --- the host ---------------------------------------------------------------
say USER "$(id -un)"
say HOME "$HOME"
if [ "$(id -u)" = 0 ]; then
    say IS_ROOT yes
    STOPS+=("running as root: run as the user that owns Klipper, never with sudo")
else
    say IS_ROOT no
fi
. /etc/os-release 2>/dev/null
say OS "${PRETTY_NAME:-unknown}"
say OS_CODENAME "${VERSION_CODENAME:-unknown}"
say ARCH "$(uname -m)"
say PYTHON3 "$(python3 -V 2>&1 | cut -d' ' -f2)"
if sudo -n true 2> /dev/null; then say SUDO_NOPASSWD yes; else
    say SUDO_NOPASSWD no
    ASKS+=("sudo asks for a password: install.sh needs sudo for systemctl (and Spoolman); ask the human to run it or to allow sudo")
fi
for t in git curl unzip; do
    if have "$t"; then say "HAVE_$(echo "$t" | tr a-z A-Z)" yes; else say "HAVE_$(echo "$t" | tr a-z A-Z)" no; fi
done
have git || STOPS+=("git is missing")
say DISK_FREE_MB "$(df -Pm "$HOME" | awk 'NR == 2 { print $4 }')"
if curl -s --max-time 5 -o /dev/null https://github.com; then say NETWORK_GITHUB yes; else
    say NETWORK_GITHUB no
    ASKS+=("github.com does not answer: install with --skip-gco-routines and --no-spoolman, or fix the network")
fi

# --- Klipper ----------------------------------------------------------------
units=$(systemctl list-units --full --all --type=service --no-legend 2> /dev/null \
    | awk '{ print $1 }' | grep -E '^klipper([-_][A-Za-z0-9_-]+)?\.service$' | grep -v '^klipper-mcu\.service$' \
    | sed 's/\.service$//' | tr '\n' ' ')
say KLIPPER_SERVICES "${units% }"
count=$(echo "$units" | wc -w)
if [ "$count" = 0 ]; then
    STOPS+=("no klipper service: install Klipper first")
elif [ "$count" -gt 1 ]; then
    ASKS+=("several Klipper services ($units): ask the human which printer, then pass -s <service> -k <path> -c <config dir>")
fi
first=$(echo "$units" | awk '{ print $1 }')
kpath=""
if [ -n "$first" ]; then
    kpath=$(systemctl show -p WorkingDirectory --value "$first.service" 2> /dev/null)
    say KLIPPER_SERVICE_STATE "$(systemctl is-active "$first.service" 2> /dev/null)"
fi
[ -n "$kpath" ] && [ -d "$kpath/klippy" ] || kpath="$HOME/klipper"
[ -n "$KPATH_ARG" ] && kpath="$KPATH_ARG"
if [ -d "$kpath/klippy/extras" ]; then
    say KLIPPER_PATH "$kpath"
    say KLIPPER_BRANCH "$(git -C "$kpath" rev-parse --abbrev-ref HEAD 2> /dev/null)"
    say KLIPPER_COMMIT "$(git -C "$kpath" describe --tags --always 2> /dev/null)"
    say KLIPPER_DIRTY_FILES "$(git -C "$kpath" status --porcelain --untracked-files=no 2> /dev/null | wc -l)"
    if git -C "$kpath" remote get-url origin 2> /dev/null | grep -qi kalico; then say KLIPPER_FLAVOR kalico; else say KLIPPER_FLAVOR klipper; fi
else
    say KLIPPER_PATH missing
    STOPS+=("no Klipper checkout at $kpath")
fi
kenv="$HOME/klippy-env/bin/python"
[ -x "$kenv" ] && say KLIPPY_ENV_PYTHON "$("$kenv" -V 2>&1 | cut -d' ' -f2)" || say KLIPPY_ENV_PYTHON missing
if [ -e "$kpath/klippy/extras/klipper_mmu.py" ]; then
    say KLIPPER_MMU_NAME_CLASH yes
    STOPS+=("$kpath/klippy/extras/klipper_mmu.py exists: install.sh refuses; ask the human what installed it")
else
    say KLIPPER_MMU_NAME_CLASH no
fi

# --- Moonraker and the config directory ----------------------------------------
say MOONRAKER_PATH "$([ -d "$HOME/moonraker/moonraker" ] && echo "$HOME/moonraker" || echo missing)"
cfg="$HOME/printer_data/config"
[ -d "$cfg" ] || cfg="$HOME/klipper_config"
[ -n "$CFG_ARG" ] && cfg="$CFG_ARG"
say CONFIG_DIR "$([ -d "$cfg" ] && echo "$cfg" || echo missing)"
say PRINTER_CFG "$([ -f "$cfg/printer.cfg" ] && echo yes || echo no)"
say MOONRAKER_CONF "$([ -f "$cfg/moonraker.conf" ] && echo yes || echo no)"
[ -f "$cfg/moonraker.conf" ] || STOPS+=("no moonraker.conf in $cfg: pass -c <config dir> or install Moonraker")

info=$(http_get /server/info)
if [ -n "$info" ]; then
    say MOONRAKER_HTTP yes
    say KLIPPY_STATE "$(echo "$info" | json_get result.klippy_state)"
else
    say MOONRAKER_HTTP no
    ASKS+=("Moonraker does not answer at $URL: the print state cannot be checked; ask the human whether the printer is idle")
fi
pstate=$(http_get "/printer/objects/query?print_stats=state" | json_get result.status.print_stats.state)
say PRINT_STATE "${pstate:-unknown}"
case "$pstate" in
    printing|paused) STOPS+=("the printer is $pstate: never install or restart Klipper during a print");;
esac

# --- what is installed already --------------------------------------------------
# readlink -f names a path even when nothing is there: look first.
oams_py="$kpath/klippy/extras/oams.py"
if [ ! -L "$oams_py" ] && [ ! -e "$oams_py" ]; then say OAMS_EXTRA none
else
    link=$(readlink -f "$oams_py" 2> /dev/null)
    if [ ! -e "$link" ]; then say OAMS_EXTRA "broken ($oams_py points to a missing $link)"
    elif echo "$link" | grep -q klipper_openams; then say OAMS_EXTRA "klipper_openams ($link)"
    else say OAMS_EXTRA "openams ($link)"; fi
fi
say OPENAMS_CHECKOUT "$([ -d "$HOME/openams/.git" ] && git -C "$HOME/openams" log --oneline -1 2> /dev/null || echo none)"
say OPENAMS_ORIGIN "$(git -C "$HOME/openams" remote get-url origin 2> /dev/null || echo none)"
say LEGACY_KLIPPER_OPENAMS "$([ -d "$HOME/klipper_openams" ] && echo yes || echo no)"
if [ -f "$cfg/printer.cfg" ]; then
    grep -qE '^\[include[[:space:]]*oams\.cfg\]' "$cfg/printer.cfg" && say PRINTER_INCLUDES_OAMS yes || say PRINTER_INCLUDES_OAMS no
fi
for f in oams.cfg oams_macros.cfg oams_macros_ordered.cfg; do
    say "HAS_$(echo "$f" | tr a-z. A-Z_)" "$([ -f "$cfg/$f" ] && echo yes || echo no)"
done
if [ -f "$cfg/moonraker.conf" ]; then
    for s in "update_manager openams" spoolman openams_spoolman; do
        key="MOONRAKER_HAS_$(echo "$s" | tr 'a-z ' 'A-Z_')"
        grep -q "^\[$s\]" "$cfg/moonraker.conf" && say "$key" yes || say "$key" no
    done
fi
if curl -s --max-time 3 http://localhost:7912/api/v1/info > /dev/null 2>&1; then say SPOOLMAN_LOCAL yes; else say SPOOLMAN_LOCAL no; fi

# --- other MMU software -------------------------------------------------------
foreign=""
if [ -d "$cfg" ]; then
    grep -rlqE '^\[(AFC|AFC_[A-Za-z_]+)( |\])' "$cfg" 2> /dev/null && foreign="$foreign AFC"
    grep -rlqE '^\[(mmu|mmu_machine)\]' "$cfg" 2> /dev/null && foreign="$foreign HappyHare"
    grep -rlqiE '^\[ercf' "$cfg" 2> /dev/null && foreign="$foreign ERCF"
fi
ls "$kpath/klippy/extras" 2> /dev/null | grep -qiE '^AFC' && foreign="$foreign AFC-extras"
ls "$kpath/klippy/extras" 2> /dev/null | grep -qE '^mmu(_|\.py|$)' && foreign="$foreign HappyHare-extras"
say FOREIGN_MMU "${foreign# }"
[ -n "$foreign" ] && ASKS+=("another MMU system is installed (${foreign# }): ask the human before installing OpenAMS beside it")

# --- CAN ------------------------------------------------------------------------
cans=$(ip -br link show type can 2> /dev/null | awk '{ print $1 }' | tr '\n' ' ')
say CAN_INTERFACES "${cans% }"
for c in $cans; do
    details=$(ip -details link show "$c" 2> /dev/null)
    say "CAN_${c}_STATE" "$(echo "$details" | grep -oE 'state [A-Z]+' | head -1 | cut -d' ' -f2)"
    say "CAN_${c}_BITRATE" "$(echo "$details" | grep -oE 'bitrate [0-9]+' | head -1 | cut -d' ' -f2)"
    say "CAN_${c}_QLEN" "$(echo "$details" | grep -oE 'qlen [0-9]+' | head -1 | cut -d' ' -f2)"
done

# --- verdict --------------------------------------------------------------------
for r in "${STOPS[@]}"; do say STOP_REASON "$r"; done
for a in "${ASKS[@]}"; do say ASK "$a"; done
if [ ${#STOPS[@]} -gt 0 ]; then say VERDICT STOP
elif [ ${#ASKS[@]} -gt 0 ]; then say VERDICT ASK
else say VERDICT OK; fi
