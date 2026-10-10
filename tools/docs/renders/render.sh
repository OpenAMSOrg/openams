#!/usr/bin/env bash
# SPDX-License-Identifier: MIT
# Regenerate the Cortex Bridge pictures used by the guide and the wiring page.
#
#   tools/docs/renders/render.sh <step> [hardware-repo]
#
# Steps:
#   board    KiCad renders of the board        -> docs/images/cortex-bridge-a4-{top,bottom}.png
#   case     case renders (assembled, exploded, print parts)
#                                              -> docs/images/cortex-bridge-case-{assembled,exploded}.png
#                                                 tools/docs/cortex-bridge-wiring/site/img/{case-*,print-*}.png
#   coupler  the coupler v5 with its collets and tubes, rendered from its STEP
#                                              -> docs/images/ptfe-coupler-v5.png
#                                                 tools/docs/cortex-bridge-wiring/site/img/ptfe-coupler-v5.png
#   spring   Bambu Lab's buffer spring photo, with an arrow at the spring
#                                              -> docs/images/buffer-spring-replace.jpg
#                                                 tools/docs/cortex-bridge-wiring/site/img/buffer-spring-replace.jpg
#   all      all of the above
#
# "board" and "case" read the Cortex Bridge hardware repository; pass its
# path, or set HW_REPO. They only read it:
# the board file is never saved, and the case build writes to build/ here.
#
# Needs: the KiCad 10 flatpak (org.kicad.KiCad), freecadcmd (FreeCAD 0.21),
# google-chrome, ImageMagick (convert), python3 with Pillow, and network access for the
# three.js modules that case_scene.html loads from unpkg.com.
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO="$(cd "$HERE/../../.." && pwd)"
# IMAGES_OUT and SITE_IMG_OUT redirect the results, e.g. to compare a fresh
# render with the committed pictures.
IMAGES="${IMAGES_OUT:-$REPO/docs/images}"
SITE_IMG="${SITE_IMG_OUT:-$REPO/tools/docs/cortex-bridge-wiring/site/img}"
BUILD="$HERE/build"
KICAD_CLI=(flatpak run --command=kicad-cli org.kicad.KiCad)
PORT="${RENDER_PORT:-8766}"

step="${1:-}"
HW_REPO="${2:-${HW_REPO:-}}"

need_hw() {
    if [ -z "$HW_REPO" ] || [ ! -d "$HW_REPO" ]; then
        echo "error: pass the hardware repository path, or set HW_REPO" >&2
        exit 2
    fi
    HW_REPO="$(cd "$HW_REPO" && pwd)"
    # The board file is the repository's one top-level KiCad board.
    local boards=("$HW_REPO"/*.kicad_pcb)
    if [ "${#boards[@]}" -ne 1 ] || [ ! -f "${boards[0]}" ]; then
        echo "error: $HW_REPO must hold exactly one top-level *.kicad_pcb" >&2
        exit 2
    fi
    HW_PCB="$(basename "${boards[0]}")"
}

# The KiCad flatpak can only write under the home directory, so the build
# folder lives in this checkout, not in /tmp.
render_board() {
    need_hw
    mkdir -p "$BUILD/board" "$IMAGES"
    for side in top bottom; do
        (cd "$HW_REPO" && "${KICAD_CLI[@]}" pcb render --side "$side" --width 1600 --height 1067 \
            --quality high --floor --background opaque \
            -o "$BUILD/board/$side.png" "$HW_PCB")
        cp "$BUILD/board/$side.png" "$IMAGES/cortex-bridge-a4-$side.png"
    done
}

# shoot <folder> <width> <height> <out.png> <query>: serve the folder and
# screenshot its view.html with headless Chrome (SwiftShader WebGL).
shoot() {
    timeout 150 google-chrome --headless=new --no-sandbox --use-angle=swiftshader \
        --enable-unsafe-swiftshader --hide-scrollbars --default-background-color=00000000 \
        --window-size="$2,$3" --virtual-time-budget=30000 \
        --screenshot="$4" "http://127.0.0.1:$PORT/view.html?$5" >/dev/null 2>&1
}

serve() {
    python3 -m http.server "$PORT" --bind 127.0.0.1 --directory "$1" >/dev/null 2>&1 &
    SERVER=$!
    trap 'kill "$SERVER" 2>/dev/null' EXIT
    sleep 1
}

unserve() {
    kill "$SERVER" 2>/dev/null
    trap - EXIT
}

render_case() {
    need_hw
    local mesh="$BUILD/case/mesh" png="$BUILD/case/png"
    rm -rf "$BUILD/case"
    mkdir -p "$mesh/print" "$png" "$IMAGES" "$SITE_IMG"

    # The board as fitted (no DNP parts), for the exploded view.
    (cd "$HW_REPO" && "${KICAD_CLI[@]}" pcb export step --subst-models --no-dnp \
        --variant JLCPCB_ASSEMBLY --force --output "$BUILD/case/board_asfitted.step" \
        "$HW_PCB")

    # Assembly-pose meshes of the standard case and the board. The print
    # files go to build/ too, so the hardware repository's out/ is untouched.
    (cd "$HW_REPO/mechanical/enclosure" && CASE_VARIANT=standard \
        BOARD_STEP="$BUILD/case/board_asfitted.step" ENCLOSURE_OUT="$BUILD/case/out" \
        PREVIEW_DIR="$mesh" freecadcmd -c "exec(open('build_enclosure.py').read())") \
        > "$BUILD/case/freecad.log" 2>&1

    # The print-part pictures show the files customers actually print.
    cp "$REPO/hardware/cortex-bridge-case/bridge_case_base_standard.stl" \
       "$REPO/hardware/cortex-bridge-case/bridge_case_lid_standard.stl" \
       "$REPO/hardware/cortex-bridge-case/bridge_case_lid_standard_letters.stl" \
       "$REPO/hardware/cortex-bridge-case/din_clip.stl" "$mesh/print/"
    cp "$HERE/case_scene.html" "$BUILD/case/view.html"

    serve "$BUILD/case"
    for scene in assembled assembled_right exploded print_base print_lid print_clip; do
        shoot "$BUILD/case" 1600 1200 "$png/$scene.png" "scene=$scene"
    done
    unserve

    convert "$png/assembled.png" -trim +repage -resize "1000x1000>" "$IMAGES/cortex-bridge-case-assembled.png"
    convert "$png/exploded.png" -trim +repage -resize "1000x1000>" "$IMAGES/cortex-bridge-case-exploded.png"
    local pair
    for pair in assembled:case-assembled assembled_right:case-assembled-right exploded:case-exploded \
                print_base:print-base print_lid:print-lid print_clip:print-clip; do
        convert "$png/${pair%%:*}.png" -trim +repage -resize "1200x1200>" "$SITE_IMG/${pair#*:}.png"
    done
}

render_coupler() {
    rm -rf "$BUILD/coupler"
    mkdir -p "$BUILD/coupler/mesh" "$IMAGES" "$SITE_IMG"
    # One mesh per part of the coupler, collets and tubes, plus parts.json.
    STEP_IN="$HERE/inputs/ptfe-coupler-v5-assembly.step" PARTS_OUT="$BUILD/coupler/mesh" \
        freecadcmd -c "exec(open('$HERE/step_parts.py').read())" > "$BUILD/coupler/freecad.log" 2>&1
    cp "$HERE/coupler_scene.html" "$BUILD/coupler/view.html"
    serve "$BUILD/coupler"
    shoot "$BUILD/coupler" 1200 1600 "$BUILD/coupler/coupler.png" "view=1,-0.3,0.4"
    unserve
    convert "$BUILD/coupler/coupler.png" -trim +repage -resize "600x640>" "$IMAGES/ptfe-coupler-v5.png"
    cp "$IMAGES/ptfe-coupler-v5.png" "$SITE_IMG/ptfe-coupler-v5.png"
}

render_spring() {
    mkdir -p "$IMAGES" "$SITE_IMG"
    python3 "$HERE/annotate_spring.py" "$HERE/inputs/bambu-slide-spring-install.jpg" \
        "$IMAGES/buffer-spring-replace.jpg"
    cp "$IMAGES/buffer-spring-replace.jpg" "$SITE_IMG/buffer-spring-replace.jpg"
}

case "$step" in
    board) render_board ;;
    case) render_case ;;
    coupler) render_coupler ;;
    spring) render_spring ;;
    all) render_board; render_case; render_coupler; render_spring ;;
    *) sed -n '2,/^set -euo/p' "$0" | grep '^#' | sed 's/^# \{0,1\}//'; exit 2 ;;
esac
