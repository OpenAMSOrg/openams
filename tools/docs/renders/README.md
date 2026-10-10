# Cortex Bridge pictures

`render.sh` regenerates the board, case, coupler and buffer-spring pictures used by the
[Cortex Bridge guide](../../../docs/guide/cortex-bridge-hardware.md) and the
[wiring page](../cortex-bridge-wiring/).

```sh
tools/docs/renders/render.sh all "$HW_REPO"   # or: board, case, coupler, spring
```

| Step | Reads | Writes |
|---|---|---|
| `board` | the hardware repository's board file (its one top-level `*.kicad_pcb`) | `docs/images/cortex-bridge-a4-top.png`, `-bottom.png` |
| `case` | the board (as fitted, no DNP parts), the hardware repository's `mechanical/enclosure/build_enclosure.py`, and `hardware/cortex-bridge-case/*.stl` here | `docs/images/cortex-bridge-case-assembled.png`, `-exploded.png`; `tools/docs/cortex-bridge-wiring/site/img/case-*.png`, `print-*.png` |
| `coupler` | `inputs/ptfe-coupler-v5-assembly.step` (the coupler with two ECAS04 collets, a 3 mm ID tube in its 3 end and a 2 mm ID tube in its 2 end), `step_parts.py` and `coupler_scene.html` | `docs/images/ptfe-coupler-v5.png`, and its copy in the wiring page's `site/img/` |
| `spring` | `inputs/bambu-slide-spring-install.jpg` and `annotate_spring.py` | `docs/images/buffer-spring-replace.jpg`, and its copy in the wiring page's `site/img/` |

`step_parts.py` splits a STEP assembly into one STL per part, with a
`parts.json` of the parts' STEP names and sizes; `coupler_scene.html` colours
the coupler's parts by those names (grey body, blue collet plungers, metal
collet parts, clear tubes) and shows the 3 end on top, with both digits
facing the camera.

The hardware repository is only read. The case is rebuilt with
`CASE_VARIANT=standard`, with its output sent to `build/` (ignored by git),
so the committed print files are never overwritten. The print-part pictures
use the STLs in `hardware/cortex-bridge-case/`, the files customers print.

`case_scene.html` is the three.js scene for the case pictures. It holds the
camera angles, colours, the exploded offsets and the drawn screws (M2 × 10
from below, M2 countersunk for the DIN clip). Headless Chrome renders it with
SwiftShader, and ImageMagick trims and scales the result.

`inputs/bambu-slide-spring-install.jpg` is Bambu Lab's photo of the opened
filament buffer, from the [Bambu Lab Wiki](https://wiki.bambulab.com/ams/filament-buffer-connection-board/slide-spring-install.jpg).
`annotate_spring.py` scales it and adds the arrow and the "Replace this
spring" label. Wherever the picture is shown, credit it as "Photo: Bambu Lab
Wiki, arrow added", linked to that address.

Set `IMAGES_OUT` and `SITE_IMG_OUT` to send the pictures somewhere else, for
example to compare a fresh render with the committed ones.

Requirements: the KiCad 10 flatpak (`org.kicad.KiCad`), FreeCAD 0.21
(`freecadcmd`), Google Chrome, ImageMagick, and Python 3 with Pillow. The case scene loads
three.js 0.160.0 from unpkg.com, so the `case` step needs network access.
