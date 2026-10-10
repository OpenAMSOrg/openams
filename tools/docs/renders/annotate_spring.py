#!/usr/bin/env python3
# SPDX-License-Identifier: MIT
"""Mark the buffer spring on Bambu Lab's slide-spring photo.

    python3 annotate_spring.py <photo.jpg> <out.jpg>

The photo is Bambu Lab's "slide-spring-install.jpg" from the filament buffer
page of the Bambu Lab Wiki (inputs/bambu-slide-spring-install.jpg). It is
scaled to 1200 px wide, and an arrow from the label below the buffer points
at the spring that the stronger Cortex Bridge spring replaces. Needs Pillow.
"""
import math
import sys

from PIL import Image, ImageDraw, ImageFont

WIDTH = 1200
RED = (224, 49, 49)
WHITE = (255, 255, 255)
INK = (27, 27, 27)
FONT = "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"
LABEL = "Replace this spring"

# Positions on the 1200 px wide image.
TAIL = (672, 716)      # under the buffer, on the white background
HEAD = (650, 546)      # the lower edge of the spring's coils
LABEL_CENTRE = (672, 748)


def arrow(draw, tail, head, colour, width, head_len, head_half):
    angle = math.atan2(head[1] - tail[1], head[0] - tail[0])
    base = (head[0] - head_len * math.cos(angle), head[1] - head_len * math.sin(angle))
    draw.line([tail, base], fill=colour, width=width)
    nx, ny = -math.sin(angle), math.cos(angle)
    draw.polygon([head,
                  (base[0] + head_half * nx, base[1] + head_half * ny),
                  (base[0] - head_half * nx, base[1] - head_half * ny)], fill=colour)


def main(src, dst):
    im = Image.open(src).convert("RGB")
    im = im.resize((WIDTH, round(im.height * WIDTH / im.width)), Image.LANCZOS)
    draw = ImageDraw.Draw(im)
    # A white outline under the red arrow keeps it visible on the grey case.
    arrow(draw, TAIL, HEAD, WHITE, 18, 44, 30)
    arrow(draw, TAIL, HEAD, RED, 10, 38, 24)
    font = ImageFont.truetype(FONT, 34)
    draw.text(LABEL_CENTRE, LABEL, font=font, fill=INK, anchor="mt",
              stroke_width=4, stroke_fill=WHITE)
    im.save(dst, quality=85, optimize=True)


if __name__ == "__main__":
    if len(sys.argv) != 3:
        sys.exit(__doc__)
    main(sys.argv[1], sys.argv[2])
