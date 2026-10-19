# Sourced by install-openams.sh. Preparation happens before stopping Klipper;
# the working tree is advanced only after Klipper has stopped.
GCO_ROUTINES_URL="https://github.com/OpenAMSOrg/gco-routines.git"
GCO_HELPER_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

# GCO_ROUTINES_SKIP=1 (--skip-gco-routines) skips the step. GCO_ROUTINES_REQUIRE=1
# (--require-gco-routines) makes any problem fatal; otherwise a problem only
# skips the step (GCO_ROUTINES_SKIP is set) and OpenAMS installs without it.
GCO_ROUTINES_SKIP="${GCO_ROUTINES_SKIP:-0}"
GCO_ROUTINES_REQUIRE="${GCO_ROUTINES_REQUIRE:-1}"  # install.sh sets 0 unless --require-gco-routines

gco_error()
{
    GCO_ROUTINES_REASON="$*"
    if [[ "$GCO_ROUTINES_REQUIRE" == 1 ]]; then
        printf '[ERROR] gco-routines: %s\n' "$*" >&2
    fi
    return 1
}

check_gco_destination()
{
    local destination="${KLIPPER_PATH}/klippy/extras/gco_routines"
    local source="${GCO_ROUTINES_PATH}/klippy_extra/gco_routines"
    if [[ -e "$destination" || -L "$destination" ]]; then
        if [[ ! -L "$destination" ]] || [[ "$(readlink -m "$destination")" != "$source" ]]; then
            gco_error "$destination already exists and is not this checkout's symlink. Back up and migrate the existing installation explicitly; nothing was replaced."
            return 1
        fi
    fi
}

check_gco_checkout()
{
    local top origin branch
    top="$(git -C "$GCO_ROUTINES_PATH" rev-parse --show-toplevel 2>/dev/null)" || {
        gco_error "$GCO_ROUTINES_PATH exists but is not a Git checkout. Use -r with a new directory or migrate it explicitly."
        return 1
    }
    [[ "$top" == "$GCO_ROUTINES_PATH" ]] || {
        gco_error "$GCO_ROUTINES_PATH is not a repository root."
        return 1
    }
    origin="$(git -C "$GCO_ROUTINES_PATH" config --get remote.origin.url)" || {
        gco_error "$GCO_ROUTINES_PATH has no origin remote."
        return 1
    }
    case "${origin,,}" in
        https://github.com/openamsorg/gco-routines.git|https://github.com/openamsorg/gco-routines|git@github.com:openamsorg/gco-routines.git) ;;
        *) gco_error "Refusing to update a checkout with a different origin: $origin"; return 1;;
    esac
    branch="$(git -C "$GCO_ROUTINES_PATH" symbolic-ref --quiet --short HEAD)" || {
        gco_error "Checkout has a detached HEAD; select main explicitly before installing."
        return 1
    }
    [[ "$branch" == main ]] || {
        gco_error "Checkout is on $branch, not main; it was left unchanged."
        return 1
    }
    [[ -z "$(git -C "$GCO_ROUTINES_PATH" status --porcelain)" ]] || {
        gco_error "Checkout has local changes; commit or relocate them before installing."
        return 1
    }
}

prepare_gco_routines()
{
    if [[ "$GCO_ROUTINES_SKIP" == 1 ]]; then
        echo "gco-routines: skipped (--skip-gco-routines)."
        return 0
    fi
    GCO_ROUTINES_REASON=""
    if ! prepare_gco_routines_checked; then
        if [[ "$GCO_ROUTINES_REQUIRE" == 1 ]]; then
            return 1
        fi
        printf '[WARNING] gco-routines: %s\n' "${GCO_ROUTINES_REASON:-the step could not be completed.}" >&2
        printf '[WARNING] gco-routines: existing files and links were left untouched; skipping this step and continuing with the OpenAMS install. Use --skip-gco-routines to silence this, or --require-gco-routines to make it fatal.\n' >&2
        GCO_ROUTINES_SKIP=1
    fi
    return 0
}

prepare_gco_routines_checked()
{
    command -v git >/dev/null || { gco_error "git is required."; return 1; }
    command -v python3 >/dev/null || { gco_error "python3 is required."; return 1; }
    GCO_ROUTINES_PATH="$(python3 -c 'import os, sys; print(os.path.realpath(os.path.expanduser(sys.argv[1])))' "$GCO_ROUTINES_PATH")"
    check_gco_destination || return 1
    if [[ ! -e "$GCO_ROUTINES_PATH" ]]; then
        git clone --branch main --single-branch "$GCO_ROUTINES_URL" "$GCO_ROUTINES_PATH" || {
            gco_error "could not clone $GCO_ROUTINES_URL (network problem?)."
            return 1
        }
    fi
    check_gco_checkout || return 1
    git -C "$GCO_ROUTINES_PATH" fetch --no-tags origin refs/heads/main:refs/remotes/origin/main || {
        gco_error "could not fetch updates from origin (network problem?)."
        return 1
    }
    GCO_ROUTINES_COMMIT="$(git -C "$GCO_ROUTINES_PATH" rev-parse refs/remotes/origin/main)"
    git -C "$GCO_ROUTINES_PATH" merge-base --is-ancestor HEAD "$GCO_ROUTINES_COMMIT" || {
        gco_error "Local history cannot fast-forward to origin/main; no files were replaced."
        return 1
    }
    local required
    for required in tools/install_gco_routines.py klippy_extra/gco_routines/__init__.py; do
        git -C "$GCO_ROUTINES_PATH" cat-file -e "$GCO_ROUTINES_COMMIT:$required" || {
            gco_error "The fetched repository is missing $required."
            return 1
        }
    done
    printf 'gco-routines prepared at %s (%s).\n' "$GCO_ROUTINES_PATH" "$GCO_ROUTINES_COMMIT"
}

install_gco_routines()
{
    [[ "$GCO_ROUTINES_SKIP" == 1 ]] && return 0
    # Recheck local ownership/state before changing any existing source files.
    check_gco_destination || return 1
    check_gco_checkout || return 1
    git -C "$GCO_ROUTINES_PATH" merge-base --is-ancestor HEAD "$GCO_ROUTINES_COMMIT" || {
        gco_error "Local history changed after preparation; refusing to install."
        return 1
    }
    git -C "$GCO_ROUTINES_PATH" merge --ff-only "$GCO_ROUTINES_COMMIT" || return 1
    python3 "$GCO_ROUTINES_PATH/tools/install_gco_routines.py" --klipper "$KLIPPER_PATH" || return 1
    printf 'gco-routines installed. Existing macros and render modes were not changed.\n'
    report_gco_baseline
}

# Installing changes nothing, but [gco_routines] loads only on a Klipper whose
# klippy/gcode.py it was tested against; any other stops Klipper at startup.
# Say which this one is, so nobody enables it on a Klipper it refuses.
report_gco_baseline()
{
    local result baseline sha
    result="$(python3 "$GCO_HELPER_DIR/gco_baseline.py" --klipper "$KLIPPER_PATH" --gco-routines "$GCO_ROUTINES_PATH" 2>/dev/null)"
    baseline="$(sed -n 's/^GCO_BASELINE=//p' <<<"$result")"
    sha="$(sed -n 's/^GCODE_PY_SHA256=//p' <<<"$result")"
    case "$baseline" in
        yes)
            printf 'gco-routines: this Klipper is a version it accepts. Enabling it is opt-in; see %s/docs/guide/install.md.\n' "$GCO_ROUTINES_PATH";;
        no)
            printf '[WARNING] gco-routines: this Klipper is not a version it accepts (klippy/gcode.py sha256 %s).\n' "$sha" >&2
            printf '[WARNING] gco-routines: do not add [gco_routines] or include oams_macros_ordered.cfg: Klipper would stop at startup. OpenAMS works fully without them.\n' >&2;;
        *)
            printf 'gco-routines: could not tell whether this Klipper is a version it accepts; check %s/docs/guide/install.md before enabling it.\n' "$GCO_ROUTINES_PATH";;
    esac
}
