#!/usr/bin/env python3
# SPDX-License-Identifier: MIT
"""Checks for the Cortex Bridge wiring page.

Structural checks (Python 3 standard library only):
  * site/ is up to date with generate.py (regenerates in memory and compares bytes)
  * the standalone SVG and the inline SVG in site/index.html both parse as XML
  * the inline drawing is identical to the standalone one (only <style> differs)
  * the smallest diagram font is >= 15 viewBox units (>= 12 px at the page's 880 px minimum width)
  * the page has no colour literals outside its token blocks, and every external link opens in a new tab

Render checks (need Google Chrome or Chromium; set $CHROME to override the binary):
  * screenshots, light and dark, at 1280 px and at 390 px (the phone view runs in a 390 px iframe,
    because headless Chrome will not make its window that narrow)
  * no horizontal page scroll at 1280 px or 390 px
  * at 1280 px the diagram's left and right edges line up with the connector cards
  * print to PDF (A4, as the browser's print dialog would)
Output goes to build/ next to this script (ignored by git). Google Fonts load from the network;
without it Chrome falls back to system fonts and the screenshots differ slightly.

Usage:  python3 check.py [--no-render]
"""
import argparse
import importlib.util
import json
import os
import re
import shutil
import subprocess
import sys
import xml.dom.minidom
from pathlib import Path

HERE = Path(__file__).resolve().parent
SITE = HERE / "site"
BUILD = HERE / "build"
HTML = SITE / "index.html"
SVG = SITE / "cortex-bridge-wiring.svg"

MAX_SHOT = 24000      # tallest screenshot to ask Chrome for, in px

failures = []


def ok(msg):
    print("  ok   " + msg)


def fail(msg):
    print("  FAIL " + msg)
    failures.append(msg)


def load_generator():
    spec = importlib.util.spec_from_file_location("generate", HERE / "generate.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def drawing(svg_text):
    """The drawing part of an SVG document: everything after </desc>, without a <style> block."""
    m = re.search(r"</desc>\s*(?:<style>.*?</style>\s*)?(.*)</svg>", svg_text, re.S)
    return m.group(1).strip() if m else None


def structural():
    print("Structural checks")
    html = HTML.read_text(encoding="utf-8")
    svg = SVG.read_text(encoding="utf-8")

    gen = load_generator()
    body = gen.build_body()
    if gen.standalone_svg(body) == svg and gen.build_page(gen.TEMPLATE.read_text(encoding="utf-8"), body) == html:
        ok("site/ matches generate.py output byte for byte")
    else:
        fail("site/ is out of date: run python3 generate.py")

    try:
        xml.dom.minidom.parseString(svg.encode("utf-8"))
        ok("standalone SVG parses")
    except Exception as e:  # noqa: BLE001
        fail("standalone SVG does not parse: %s" % e)
    m = re.search(r'(<svg class="wd".*?</svg>)', html, re.S)
    if not m:
        fail("no inline SVG in index.html")
        return
    inline = m.group(1)
    try:
        xml.dom.minidom.parseString(inline.encode("utf-8"))
        ok("inline SVG parses")
    except Exception as e:  # noqa: BLE001
        fail("inline SVG does not parse: %s" % e)

    if drawing(inline) is not None and drawing(inline) == drawing(svg):
        ok("inline drawing is identical to the standalone drawing")
    else:
        fail("inline drawing differs from the standalone drawing")

    sizes = [float(x) for x in re.findall(r"font-size:([\d.]+)px", svg)]
    smallest = min(sizes)
    if smallest >= 15:
        ok("smallest diagram font %.1f units = %.1f px at 880 px" % (smallest, smallest * 880 / gen.W))
    else:
        fail("smallest diagram font %.1f units is below 15" % smallest)

    css = re.search(r"<style>(.*?)</style>", html, re.S).group(1)
    for key, value in gen.placeholders(body)[:4]:     # strip the four token blocks
        css = css.replace(value, "")
    literals = re.findall(r"#[0-9a-fA-F]{3,6}\b|rgba?\(", css)
    if literals:
        fail("colour literals outside the token blocks: %s" % literals)
    else:
        ok("all page colours come from tokens")
    links = re.findall(r'<a href="(https?://[^"]+)"([^>]*)>', html)
    bad = [u for u, attrs in links if 'target="_blank"' not in attrs or 'rel="noopener"' not in attrs]
    if bad:
        fail("external links without target=_blank rel=noopener: %s" % bad)
    else:
        ok("%d external links open in a new tab" % len(links))
    missing = [p for p in re.findall(r'src="(img/[^"]+)"', html) if not (SITE / p).exists()]
    if missing:
        fail("missing images: %s" % missing)
    else:
        ok("all referenced images exist in site/img/")


# ---------------------------------------------------------------------------------------------
# Rendering
# ---------------------------------------------------------------------------------------------
def find_chrome():
    for name in (os.environ.get("CHROME"), "google-chrome", "google-chrome-stable", "chromium", "chromium-browser"):
        if name and shutil.which(name):
            return shutil.which(name)
    return None


def chrome(binary, *args, timeout=180):
    cmd = [binary, "--headless=new", "--disable-gpu", "--hide-scrollbars", "--allow-file-access-from-files",
           "--virtual-time-budget=7000", *args]
    return subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)


PROBE_JS = """<script>
window.addEventListener('load',()=>setTimeout(()=>{
  const e=document.documentElement, q=s=>{const n=document.querySelector(s); if(!n) return null; const b=n.getBoundingClientRect(); return [Math.round(b.left),Math.round(b.right)]};
  const r={scrollW:e.scrollWidth, clientW:e.clientWidth, height:e.scrollHeight, svg:q('.diagram-scroll svg'), cards:q('.cards'), bom:q('.bom')};
  const p=document.createElement('pre'); p.id='probe'; p.textContent=JSON.stringify(r); document.body.appendChild(p);
},1500));
</script>"""

PHONE_HARNESS = """<!doctype html><html><head><meta charset="utf-8"><title>390 px</title></head>
<body style="margin:0;background:#888">
<iframe id="f" src="%s" style="width:390px;height:%dpx;border:0;display:block"></iframe>
<script>
document.getElementById('f').addEventListener('load',()=>setTimeout(()=>{
  const d=document.getElementById('f').contentDocument.documentElement;
  const p=document.createElement('pre'); p.id='probe'; p.style.display='none';
  p.textContent=JSON.stringify({scrollW:d.scrollWidth, clientW:d.clientWidth, height:d.ownerDocument.body.scrollHeight});
  document.body.appendChild(p);
},1500));
</script></body></html>"""


def page_copy(theme, probe):
    """A copy of the page in build/ that loads its images from site/ and optionally has a theme and a probe."""
    html = HTML.read_text(encoding="utf-8")
    html = html.replace("<head>", '<head>\n<base href="../site/">', 1)
    if theme == "dark":
        html = html.replace('<html lang="en">', '<html lang="en" data-theme="dark">', 1)
    if probe:
        html = html.replace("</body>", PROBE_JS + "\n</body>", 1)
    name = "page-%s%s.html" % (theme, "-probe" if probe else "")
    (BUILD / name).write_text(html, encoding="utf-8")
    return BUILD / name


def read_probe(binary, url, window):
    r = chrome(binary, "--window-size=%s" % window, "--dump-dom", url)
    m = re.search(r'<pre id="probe"[^>]*>(.*?)</pre>', r.stdout, re.S)
    return json.loads(m.group(1)) if m else None


def render():
    print("Render checks")
    binary = find_chrome()
    if not binary:
        print("  skip no Chrome/Chromium found (set $CHROME); render checks not run")
        return
    BUILD.mkdir(exist_ok=True)
    for theme in ("light", "dark"):
        # 1280 px desktop
        probe = read_probe(binary, page_copy(theme, True).as_uri(), "1280,900")
        if not probe:
            fail("%s 1280: probe did not run" % theme)
            continue
        if probe["scrollW"] == probe["clientW"]:
            ok("%s 1280: no horizontal page scroll" % theme)
        else:
            fail("%s 1280: page scrolls sideways (%s > %s)" % (theme, probe["scrollW"], probe["clientW"]))
        if probe["svg"] == probe["cards"] == probe["bom"]:
            ok("%s 1280: diagram, cards and parts list share edges %s" % (theme, probe["svg"]))
        else:
            fail("%s 1280: edges differ: diagram %s, cards %s, parts list %s" % (theme, probe["svg"], probe["cards"], probe["bom"]))
        shot = BUILD / ("desktop-1280-%s.png" % theme)
        chrome(binary, "--window-size=1280,%d" % min(probe["height"] + 40, MAX_SHOT),
               "--screenshot=%s" % shot, page_copy(theme, False).as_uri())
        ok("%s 1280: %s" % (theme, shot.relative_to(HERE)))

        # 390 px phone, in an iframe
        target = page_copy(theme, False).name
        harness = BUILD / ("phone-%s.html" % theme)
        harness.write_text(PHONE_HARNESS % (target, 20000), encoding="utf-8")
        probe = read_probe(binary, harness.as_uri(), "600,900")
        if not probe:
            fail("%s 390: probe did not run" % theme)
            continue
        if probe["scrollW"] == probe["clientW"] == 390:
            ok("%s 390: no horizontal page scroll" % theme)
        else:
            fail("%s 390: scrollWidth %s, clientWidth %s" % (theme, probe["scrollW"], probe["clientW"]))
        height = probe["height"]
        harness.write_text(PHONE_HARNESS % (target, height), encoding="utf-8")
        shot = BUILD / ("phone-390-%s.png" % theme)
        chrome(binary, "--window-size=390,%d" % min(height, MAX_SHOT), "--screenshot=%s" % shot, harness.as_uri())
        ok("%s 390: %s (page %d px tall%s)" % (theme, shot.relative_to(HERE), height,
                                              ", screenshot cut at %d px" % MAX_SHOT if height > MAX_SHOT else ""))

    svg_shot = BUILD / "standalone-svg.png"
    gen = load_generator()
    chrome(binary, "--window-size=%d,%d" % (gen.W, gen.H), "--screenshot=%s" % svg_shot, SVG.as_uri())
    ok("standalone SVG: %s" % svg_shot.relative_to(HERE))

    pdf = BUILD / "print.pdf"
    chrome(binary, "--no-pdf-header-footer", "--print-to-pdf=%s" % pdf, HTML.as_uri())
    if pdf.exists() and pdf.stat().st_size > 0:
        pages = len(re.findall(rb"/Type\s*/Page[^s]", pdf.read_bytes()))
        ok("print: %s (%d pages; page 1 should hold the header, the whole diagram and its caption)"
           % (pdf.relative_to(HERE), pages))
    else:
        fail("print to PDF failed")


def main():
    ap = argparse.ArgumentParser(description="Check the Cortex Bridge wiring page.")
    ap.add_argument("--no-render", action="store_true", help="only run the structural checks")
    args = ap.parse_args()
    structural()
    if not args.no_render:
        render()
    print()
    if failures:
        print("%d check(s) failed" % len(failures))
        sys.exit(1)
    print("all checks passed" + ("" if args.no_render else "; look at the screenshots in build/"))


if __name__ == "__main__":
    main()
