# The OpenAMS Mainsail. Sourced by install.sh.
#
# With --mainsail-panel, Mainsail is replaced by the OpenAMS build of it
# (github.com/OpenAMSOrg/mainsail: Mainsail with the OpenAMS panel from
# openams-ui), and Moonraker's [update_manager mainsail] is pointed at that
# repository, so Mainsail's own update button keeps the panel. The stock
# Mainsail is kept as <path>.stock; `install.sh -u` puts it back.
#
# A Mainsail that is a symbolic link or a git checkout is someone's own
# build: it is left alone.
#
# Documented test switches: OPENAMS_MAINSAIL_PATH is the Mainsail folder
# (default ~/mainsail); OPENAMS_MAINSAIL_RELEASE_ZIP is a local release zip
# used instead of downloading the latest release.

MAINSAIL_PATH="${OPENAMS_MAINSAIL_PATH:-${HOME}/mainsail}"
MAINSAIL_REPO="OpenAMSOrg/mainsail"
MAINSAIL_STOCK_REPO="mainsail-crew/mainsail"

# The owner named in a Mainsail folder's release_info.json (empty if none).
mainsail_owner()
{
    python3 - "$1/release_info.json" <<'EOF' 2>/dev/null
import json, sys
try:
    print(json.load(open(sys.argv[1])).get("project_owner", ""))
except (OSError, ValueError):
    print("")
EOF
}

# Point [update_manager mainsail] at $1 (adding the section if there is none).
set_mainsail_updater()
{
    local repo="$1" cfg="${MOONRAKER_CONFIG_DIR}/moonraker.conf" tmp
    tmp="$(mktemp)"
    if moonraker_has_section "update_manager mainsail"; then
        awk -v repo="$repo" '
            /^\[/ { in_section = ($0 ~ /^\[update_manager mainsail\][[:space:]]*$/) }
            in_section && /^repo[[:space:]]*:/ { print "repo: " repo; next }
            { print }
        ' "$cfg" > "$tmp"
        if ! awk '/^\[update_manager mainsail\]/{s=1;next} /^\[/{s=0} s && /^repo[[:space:]]*:/{f=1} END{exit !f}' "$tmp"; then
            awk -v repo="$repo" '{ print } /^\[update_manager mainsail\][[:space:]]*$/ { print "repo: " repo }' "$tmp" > "$tmp.2"
            mv "$tmp.2" "$tmp"
        fi
        cat "$tmp" > "$cfg"
    else
        {
            echo ""
            echo "[update_manager mainsail]"
            echo "type: web"
            echo "channel: stable"
            echo "repo: ${repo}"
            echo "path: ${MAINSAIL_PATH/#$HOME/~}"
        } >> "$cfg"
    fi
    rm -f "$tmp"
}

install_mainsail_panel()
{
    echo -n "Installing the OpenAMS Mainsail (with the OpenAMS panel)... "
    if [ -L "$MAINSAIL_PATH" ] || [ -d "$MAINSAIL_PATH/.git" ]; then
        echo "[SKIPPED] $MAINSAIL_PATH is a link or a git checkout: your own Mainsail build is left alone."
        return 0
    fi
    local work zip
    work="$(mktemp -d)"
    zip="${OPENAMS_MAINSAIL_RELEASE_ZIP:-}"
    if [ -z "$zip" ]; then
        zip="$work/mainsail.zip"
        if ! python3 - "$MAINSAIL_REPO" "$zip" <<'EOF'
import json, sys, urllib.request
repo, out = sys.argv[1], sys.argv[2]
with urllib.request.urlopen("https://api.github.com/repos/%s/releases/latest" % repo, timeout=60) as r:
    release = json.load(r)
asset = next(a for a in release["assets"] if a["name"] == "mainsail.zip")
with urllib.request.urlopen(asset["browser_download_url"], timeout=300) as r, open(out, "wb") as f:
    f.write(r.read())
EOF
        then
            rm -rf "$work"
            echo "[ERROR] Could not download the OpenAMS Mainsail from github.com/${MAINSAIL_REPO}; Mainsail is unchanged." >&2
            return 1
        fi
    fi
    if ! python3 -c 'import sys, zipfile; zipfile.ZipFile(sys.argv[1]).extractall(sys.argv[2])' "$zip" "$work/new" \
            || [ ! -f "$work/new/index.html" ]; then
        rm -rf "$work"
        echo "[ERROR] The OpenAMS Mainsail release is not a Mainsail build; Mainsail is unchanged." >&2
        return 1
    fi
    if [ -d "$MAINSAIL_PATH" ]; then
        # Keep the instance settings, and the stock Mainsail once.
        [ -f "$MAINSAIL_PATH/config.json" ] && cp "$MAINSAIL_PATH/config.json" "$work/new/config.json"
        if [ "$(mainsail_owner "$MAINSAIL_PATH")" != "OpenAMSOrg" ] && [ ! -e "${MAINSAIL_PATH}.stock" ]; then
            mv "$MAINSAIL_PATH" "${MAINSAIL_PATH}.stock"
        else
            rm -rf "$MAINSAIL_PATH"
        fi
    fi
    mkdir -p "$(dirname "$MAINSAIL_PATH")"
    mv "$work/new" "$MAINSAIL_PATH"
    rm -rf "$work"
    set_mainsail_updater "$MAINSAIL_REPO"
    echo "[OK]"
    echo "  Reload Mainsail with Ctrl+Shift+R (Cmd+Shift+R on a Mac): a plain reload can show"
    echo "  the old Mainsail from the browser's cache. The OpenAMS panel is added at the end of"
    echo "  the dashboard's first column."
    restart_moonraker
}

remove_mainsail_panel()
{
    if [ -L "$MAINSAIL_PATH" ] || [ "$(mainsail_owner "$MAINSAIL_PATH")" != "OpenAMSOrg" ]; then
        return 0
    fi
    echo -n "Restoring the stock Mainsail... "
    if [ -d "${MAINSAIL_PATH}.stock" ]; then
        rm -rf "$MAINSAIL_PATH"
        mv "${MAINSAIL_PATH}.stock" "$MAINSAIL_PATH"
        set_mainsail_updater "$MAINSAIL_STOCK_REPO"
        echo "[OK]"
        restart_moonraker
    else
        echo "[SKIPPED] no stock copy was kept; reinstall the stock Mainsail (for example with KIAUH) to remove the panel."
    fi
}
