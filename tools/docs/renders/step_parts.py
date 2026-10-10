# SPDX-License-Identifier: MIT
"""Split a STEP assembly into one STL per part, for the three.js scenes.

    STEP_IN=<assembly.step> PARTS_OUT=<folder> freecadcmd -c "exec(open('step_parts.py').read())"

Writes <folder>/part-NN.stl for every solid and <folder>/parts.json with each
part's STEP label, file and bounding box, so a scene can colour parts by name.
FreeCAD 0.21 (freecadcmd); FreeCAD runs this file through exec(), so the
paths come from the environment.
"""
import json
import os

import FreeCAD as App
import Import
import MeshPart

src = os.environ["STEP_IN"]
out = os.environ["PARTS_OUT"]
os.makedirs(out, exist_ok=True)

doc = App.newDocument("parts")
Import.insert(src, doc.Name)
doc.recompute()

parts = []
for obj in doc.Objects:
    # Leaf geometry only; App::Part containers just place their children.
    if obj.TypeId != "Part::Feature" or obj.Shape.isNull() or not obj.Shape.Solids:
        continue
    placed = obj.Shape.copy()
    placed.Placement = obj.getGlobalPlacement()
    for i, solid in enumerate(placed.Solids):
        name = "part-%02d.stl" % len(parts)
        MeshPart.meshFromShape(Shape=solid, LinearDeflection=0.01, AngularDeflection=0.15,
                               Relative=False).write(os.path.join(out, name))
        bb = solid.BoundBox
        parts.append({"file": name, "label": obj.Label, "solid": i,
                      "bbox": [round(v, 3) for v in (bb.XMin, bb.YMin, bb.ZMin, bb.XMax, bb.YMax, bb.ZMax)],
                      "volume": round(solid.Volume, 3)})

with open(os.path.join(out, "parts.json"), "w") as fh:
    json.dump(parts, fh, indent=1)
print("parts:", len(parts))
for p in parts:
    print(" ", p["file"], p["label"], p["bbox"], p["volume"])
