# Cortex Bridge case

Print files for the Cortex Bridge Rev A4 case. [The Cortex Bridge
board](../../docs/guide/cortex-bridge-hardware.md#the-case) has the parts list,
the screws and the assembly steps.

| File | What it is |
|---|---|
| `bridge_case_base_standard.stl` | Base. Prints as oriented, no supports. |
| `bridge_case_lid_standard.stl` | Lid, single colour. Prints roof-down, no supports. |
| `bridge_case_lid_standard_2colour.3mf` | Lid with inlaid lettering, as one two-part object: give the lettering a contrasting filament. Use instead of the single-colour lid. |
| `bridge_case_lid_standard_letters.stl` | The lettering alone, for slicers that want the two parts as separate STLs. |
| `din_clip.stl` | Optional TS35 DIN rail clip, instead of mounting with foam tape. |
| `*.step` | The same parts for CAD. |

These are the standard case for boards without the optional Ethernet jack. It
has no Ethernet opening and no opening over the factory debug header. Print in
PETG, ASA or ABS.
