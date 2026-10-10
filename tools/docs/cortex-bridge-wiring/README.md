# Cortex Bridge wiring page

The customer page for wiring a Cortex Bridge (Rev A4): the wiring diagram, connector details,
wiring order, power-on check, and the printed case with its parts list. `site/` is the
publishable folder: `index.html`, `cortex-bridge-wiring.svg` and `img/`.

The diagram is drawn once in `generate.py`. The same drawing goes into the page (inline, themed
light/dark from the page's colour tokens) and into a standalone SVG with its own light background
and fixed colours, for Markdown docs.

## Regenerate

```sh
python3 generate.py          # writes site/index.html and site/cortex-bridge-wiring.svg
python3 generate.py --docs   # also writes docs/images/cortex-bridge-wiring.svg (the guide's copy)
```

Python 3 standard library only. The guide's `docs/images/cortex-bridge-wiring.svg` comes from
`--docs`; do not edit it by hand.

## Check

```sh
python3 check.py              # structural checks + headless Chrome renders
python3 check.py --no-render  # structural checks only
```

Structural checks: `site/` matches `generate.py` byte for byte, both SVGs parse, the inline
drawing equals the standalone one, the smallest diagram label is 12 px at the page's 880 px
minimum diagram width, all colours come from tokens, and external links open in a new tab.

Render checks need Google Chrome or Chromium (`$CHROME` overrides the binary). They write
screenshots (light and dark at 1280 px and 390 px, the standalone SVG) and `print.pdf` to
`build/`, which git ignores. They also check that the page never scrolls sideways and that the
diagram lines up with the text column. No other helpers are needed (no PIL). Fonts load from
Google Fonts, so screenshots taken offline use fallback fonts.

## Files

| File | Kind | What it holds |
|---|---|---|
| `generate.py` | input | The drawing (`build_body`), colour tokens, diagram class rules |
| `page.tpl.html` | input | Page text and CSS; placeholders for tokens, diagram CSS and the inline SVG |
| `site/img/` (8 images) | input | Made by [`tools/docs/renders/render.sh`](../renders/) (`case`, `coupler` and `spring` steps); do not edit by hand: `case-assembled.png`, `case-assembled-right.png`, `case-exploded.png`, `print-base.png`, `print-lid.png`, `print-clip.png`, `ptfe-coupler-v5.png`, and `buffer-spring-replace.jpg` (Bambu Lab Wiki photo with an added arrow, credited on the page) |
| `check.py` | tool | The checks above |
| `site/index.html` | generated | The page |
| `site/cortex-bridge-wiring.svg` | generated | Standalone diagram |
| `docs/images/cortex-bridge-wiring.svg` (repo) | generated | Copy for the guide, with `--docs` |

Edit the text in `page.tpl.html` and the drawing in `generate.py`, then regenerate. Do not
hand-edit generated files. Keep diagram text at 15 viewBox units or more.
