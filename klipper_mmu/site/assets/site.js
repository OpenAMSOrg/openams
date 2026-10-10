// klipper-mmu docs: theme toggle, and the manual's nav. One filament runs
// down the nav, fed as you read: a spool that turns pixel-true to the fed
// length, and stations that light up as you pass them.

(function () {
  var root = document.documentElement;
  var themeBtn = document.getElementById('theme-toggle');
  function effective() {
    return root.dataset.theme || (matchMedia('(prefers-color-scheme: dark)').matches ? 'dark' : 'light');
  }
  function paintTheme() { themeBtn.textContent = effective() === 'dark' ? 'Light' : 'Dark'; }
  themeBtn.addEventListener('click', function () {
    var next = effective() === 'dark' ? 'light' : 'dark';
    root.dataset.theme = next;
    try { localStorage.setItem('kmmu-theme', next); } catch (e) {}
    paintTheme();
  });
  paintTheme();

  // The manual's chapters. No href yet = not written: dimmed, dashed
  // station, no link, no toggle.
  var MANUAL = [
    { id: 'index', href: 'index.html', title: 'Start here' },
    { id: 'devices', href: 'devices.html', title: 'Supported units' },
    { id: 'quick-start', href: 'quick-start.html', title: 'Quick start: BoxTurtle' },
    { id: 'pins', href: 'pins.html', title: 'Your MCU and your pins' },
    { id: 'calibration', href: 'calibration.html', title: 'Calibration' },
    { id: 'commands', href: 'commands.html', title: 'Commands' },
    { id: 'troubleshooting', href: 'troubleshooting.html', title: 'When things go wrong' },
    { id: 'inside', href: 'inside.html', title: 'How it works inside' }
  ];

  var page = document.body.dataset.page || '';
  var curIdx = 0;
  for (var mi = 0; mi < MANUAL.length; mi++) if (MANUAL[mi].id === page) curIdx = mi;

  var reduce = matchMedia('(prefers-reduced-motion: reduce)').matches;
  var nav = document.getElementById('toc');
  if (!nav) return;
  nav.setAttribute('tabindex', '-1');

  function loadState() { try { return JSON.parse(localStorage.getItem('kmmu-toc')) || {}; } catch (e) { return {}; } }
  function saveState(s) { try { localStorage.setItem('kmmu-toc', JSON.stringify(s)); } catch (e) {} }
  var uiState = loadState();

  // --- Furniture: spool, tube, tip, and the chapter list ---
  var body = document.createElement('div'); body.className = 'toc-body';
  var spoolDock = document.createElement('div'); spoolDock.className = 'toc-spool';
  spoolDock.setAttribute('aria-hidden', 'true');
  var SPOOL_R = 20;
  var spool = document.createElementNS('http://www.w3.org/2000/svg', 'svg');
  spool.setAttribute('class', 'spool'); spool.setAttribute('viewBox', '-30 -30 60 60');
  spool.innerHTML =
    '<path class="spool-flow" d="M20 0 V 34"/>' +
    '<g class="spool-turn"><circle class="spool-flange" r="27"/>' +
    '<circle class="spool-cut spool-mark" cx="0" cy="-23.5" r="2.4"/><circle class="spool-cut" cx="23.5" cy="0" r="2.2"/>' +
    '<circle class="spool-cut" cx="0" cy="23.5" r="2.2"/><circle class="spool-cut" cx="-23.5" cy="0" r="2.2"/>' +
    '<circle class="spool-wound" r="16"/><circle class="spool-wind" r="13.5"/><circle class="spool-wind" r="16"/><circle class="spool-wind" r="18.5"/>' +
    '<line class="spool-tick" x1="12.5" y1="0" x2="19.5" y2="0"/><line class="spool-tick" x1="-12.5" y1="0" x2="-19.5" y2="0"/>' +
    '<circle class="spool-hub" r="8"/><circle class="spool-hole" r="3.2"/></g>';
  spoolDock.appendChild(spool);
  var tube = document.createElement('div'); tube.className = 'toc-tube'; tube.setAttribute('aria-hidden', 'true');
  var fed = document.createElement('div'); fed.className = 'fed'; tube.appendChild(fed);
  var tip = document.createElement('div'); tip.className = 'tip'; tip.setAttribute('aria-hidden', 'true');
  var list = document.createElement('ol'); list.className = 'chapters';
  body.appendChild(spoolDock); body.appendChild(tube); body.appendChild(tip); body.appendChild(list);
  nav.appendChild(body);
  var spoolTurn = spool.querySelector('.spool-turn');
  var spoolFlow = spool.querySelector('.spool-flow');

  function station(color, dashed, small) {
    var s = document.createElement('span');
    s.className = 'station' + (small ? ' small' : '');
    s.style.color = color;
    if (dashed) s.style.borderStyle = 'dashed';
    s.setAttribute('aria-hidden', 'true');
    body.appendChild(s);
    return s;
  }
  // A fold wraps a list so it can animate open/closed as one unit
  // (grid-template-rows 0fr -> 1fr).
  function fold(ol) {
    var f = document.createElement('div'); f.className = 'fold'; f.appendChild(ol);
    return f;
  }
  function entryRow(text, href, color, isChild) {
    var li = document.createElement('li'); li.className = 'entry' + (isChild ? ' child' : '');
    var a = document.createElement('a'); a.href = href; a.textContent = text;
    li.appendChild(a);
    return { li: li, link: a, station: station(color, false, isChild) };
  }
  // Read a page's own sections/h3s from its DOM: the single source of
  // truth, current page or fetched, so nothing is duplicated in the manifest.
  function readEntries(rootDoc, hrefPrefix) {
    var out = [];
    rootDoc.querySelectorAll('main section[id][data-lane]').forEach(function (sec) {
      var color = 'var(--lane-' + sec.dataset.lane + ')';
      var e = entryRow(sec.dataset.station, hrefPrefix + '#' + sec.id, color, false);
      e.top = sec; e.children = [];
      var subOl = null;
      sec.querySelectorAll('h3[id]').forEach(function (h3) {
        if (!subOl) { subOl = document.createElement('ol'); subOl.className = 'entries sub'; }
        var c = entryRow(h3.dataset.station || h3.textContent, hrefPrefix + '#' + h3.id, color, true);
        c.top = h3; subOl.appendChild(c.li); e.children.push(c);
      });
      if (subOl) { e.fold = fold(subOl); e.li.appendChild(e.fold); }
      out.push(e);
    });
    return out;
  }

  // --- Chapters ---
  var chapters = []; // { m, i, li, chevron?, link?, entriesOl?, fold?, entries, state, stationEl }
  MANUAL.forEach(function (m, i) {
    var li = document.createElement('li'); li.className = 'chapter';
    var row = document.createElement('div'); row.className = 'chapter-row';
    var ch = { m: m, i: i, li: li, row: row, entries: [], state: 'collapsed' };
    if (!m.href) {
      li.className += ' soon';
      var dim = document.createElement('span'); dim.className = 'chapter-link dim'; dim.textContent = m.title;
      dim.title = 'Coming soon';
      dim.insertAdjacentHTML('beforeend', '<span class="vh"> (coming soon)</span>');
      row.appendChild(dim);
      ch.stationEl = station('var(--ink-3)', true, false);
      li.appendChild(row);
    } else {
      var btn = document.createElement('button'); btn.className = 'chevron'; btn.type = 'button';
      btn.innerHTML = '<svg viewBox="0 0 12 12" aria-hidden="true" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><path d="M3 2 L8 6 L3 10"/></svg>';
      btn.setAttribute('aria-expanded', 'false');
      var a = document.createElement('a'); a.className = 'chapter-link'; a.href = m.href; a.textContent = m.title;
      row.appendChild(btn); row.appendChild(a);
      var ol = document.createElement('ol'); ol.className = 'entries';
      ch.chevron = btn; ch.link = a; ch.entriesOl = ol; ch.fold = fold(ol);
      btn.setAttribute('aria-controls', 'entries-' + i);
      ol.id = 'entries-' + i;
      btn.addEventListener('click', function () { toggleChapter(ch); });
      ch.stationEl = station('var(--ink-3)', false, false);
      li.appendChild(row); li.appendChild(ch.fold);
    }
    chapters.push(ch);
    list.appendChild(li);
  });

  var current = chapters[curIdx];
  function openChapter(ch, entries) {
    ch.entries = entries;
    entries.forEach(function (e) { ch.entriesOl.appendChild(e.li); });
    ch.state = 'open';
    ch.fold.classList.add('open');
    ch.chevron.setAttribute('aria-expanded', 'true');
    layout();
  }

  function toggleChapter(ch) {
    var willOpen = ch.state !== 'open';
    if (willOpen && ch.state === 'collapsed' && ch !== current) {
      ch.state = 'loading';
      ch.chevron.classList.add('loading');
      fetch(ch.m.href).then(function (r) {
        if (!r.ok) throw new Error('http ' + r.status);
        return r.text();
      }).then(function (html) {
        var doc = new DOMParser().parseFromString(html, 'text/html');
        ch.chevron.classList.remove('loading');
        openChapter(ch, readEntries(doc, ch.m.href));
        uiState[ch.i] = true; saveState(uiState);
      }).catch(function () {
        // No fetch (file://, or the page is missing): drop the toggle,
        // keep a plain chapter link.
        ch.chevron.remove(); ch.fold.remove(); ch.state = 'plain';
      });
      return;
    }
    ch.fold.classList.toggle('open', willOpen);
    ch.chevron.setAttribute('aria-expanded', String(willOpen));
    ch.state = willOpen ? 'open' : 'collapsed';
    uiState[ch.i] = willOpen; saveState(uiState);
    layout();
  }

  if (current && current.entriesOl) openChapter(current, readEntries(document, ''));
  chapters.forEach(function (ch) {
    if (ch !== current && ch.chevron && uiState[ch.i]) toggleChapter(ch);
  });

  // --- Section-level reactive fold: a section's h3 children show only
  // while that section is active, or focus is inside them. ---
  function setActiveSection(entry) {
    current.entries.forEach(function (e) {
      if (!e.fold) return;
      e.fold.classList.toggle('open', e === entry || e.li.contains(document.activeElement));
    });
  }

  // --- Geometry: doc position (T) of each current entry vs. its station's
  // position (y) in .toc-body, so scroll position maps onto the nav. A row
  // folded away hides its station. segs colour the fed filament: each entry's
  // lane from its station down, earlier chapters in their own lane. ---
  var entriesFlat = [], segs = [], tubeTop = 0, navBottom = 0;
  function place(el, st, bodyTop) {
    var shut = !!el.closest('.fold:not(.open)');
    st.style.display = shut ? 'none' : '';
    if (shut) return false;
    var r = el.getClientRects()[0];
    st.style.top = (r.top - bodyTop + Math.min(r.height, 30) / 2) + 'px';
    return true;
  }
  function navMid(el) { return parseFloat(el.style.top) || 0; }

  function layout() {
    var bodyTop = body.getBoundingClientRect().top;
    entriesFlat = []; segs = [];
    chapters.forEach(function (ch, i) {
      place(ch.row, ch.stationEl, bodyTop);
      var chY = navMid(ch.stationEl), first = true;
      if (i < curIdx) segs.push({ y: chY, color: 'var(--lane-' + (i % 4 + 1) + ')' });
      ch.entries.forEach(function (e) {
        [e].concat(e.children).forEach(function (x) {
          if (!place(x.link, x.station, bodyTop) || i > curIdx) return;
          var y = navMid(x.station), color = x.station.style.color;
          if (ch === current) {
            if (first) segs.push({ y: chY, color: color });
            entriesFlat.push({ T: x.top.getBoundingClientRect().top + scrollY, y: y });
          }
          first = false;
          segs.push({ y: y, color: color });
        });
      });
    });
    if (!entriesFlat.length) {
      var cy = navMid(current.stationEl);
      entriesFlat.push({ T: 0, y: cy });
      segs.push({ y: cy, color: current.entries.length ? current.entries[0].station.style.color : 'var(--lane-1)' });
    }
    navBottom = list.getBoundingClientRect().bottom - bodyTop;
    var sr = spool.getBoundingClientRect();
    tubeTop = sr.top + sr.height / 2 - bodyTop;
    tube.style.top = tubeTop + 'px';
    tube.style.height = Math.max(0, navBottom - tubeTop) + 'px';
    fed.innerHTML = '';
    segs.forEach(function (sg, k) {
      var top = k ? sg.y : tubeTop, end = k + 1 < segs.length ? segs[k + 1].y : navBottom;
      var span = document.createElement('span');
      span.style.top = (top - tubeTop) + 'px'; span.style.height = Math.max(0, end - top) + 'px';
      span.style.background = sg.color;
      fed.appendChild(span);
    });
    render();
    kick();
  }

  // Fed target in document space, then mapped into nav space by walking the
  // flattened list of the current chapter's visible entries.
  function docTarget() {
    var doc = document.documentElement;
    if (scrollY + innerHeight >= doc.scrollHeight - 4) return doc.scrollHeight;
    return scrollY + innerHeight * 0.42;
  }
  function clamp01(x) { return x < 0 ? 0 : x > 1 ? 1 : x; }
  function navTarget(p) {
    if (p <= entriesFlat[0].T) return entriesFlat[0].y;
    for (var i = 0; i < entriesFlat.length - 1; i++) {
      var a = entriesFlat[i], b = entriesFlat[i + 1];
      if (p <= b.T) return a.y + clamp01((p - a.T) / (b.T - a.T || 1)) * (b.y - a.y);
    }
    // Past the last entry: interpolate to the next chapter's station, or the list end.
    var last = entriesFlat[entriesFlat.length - 1];
    var nextCh = chapters[curIdx + 1];
    var end = nextCh ? navMid(nextCh.stationEl) : navBottom;
    var docEnd = document.documentElement.scrollHeight;
    return last.y + clamp01((p - last.T) / (docEnd - last.T || 1)) * (end - last.y);
  }
  function colorAt(y) {
    var c = segs.length ? segs[0].color : 'var(--lane-1)';
    segs.forEach(function (sg) { if (sg.y <= y) c = sg.color; });
    return c;
  }

  var tipY = 0, lastT = 0, raf = 0;
  function render() {
    var col = colorAt(tipY), fedLen = Math.max(0, tipY - tubeTop);
    fed.style.height = fedLen + 'px';
    tip.style.top = tipY + 'px'; tip.style.color = col;
    // Earlier chapters are fed through; the current one up to the tip.
    chapters.forEach(function (ch, i) {
      ch.stationEl.classList.toggle('hit', i <= curIdx);
      ch.entries.forEach(function (e) {
        [e].concat(e.children).forEach(function (x) {
          x.station.classList.toggle('hit', i < curIdx || (i === curIdx && navMid(x.station) <= tipY + 2));
        });
      });
    });
    var k = spool.getBoundingClientRect().width / 60 || 1;
    spoolTurn.setAttribute('transform', 'rotate(' + (fedLen / (2 * Math.PI * SPOOL_R * k) * 360).toFixed(2) + ')');
    spoolFlow.style.strokeDashoffset = (-fedLen / k).toFixed(2);
    markActive();
    updateBarProgress(col);
  }

  function markActive() {
    var p = docTarget(), activeChild = null, activeSection = null;
    current.entries.forEach(function (e) {
      if (e.top.getBoundingClientRect().top + scrollY <= p) activeSection = e;
      if (e.fold && e.fold.classList.contains('open')) e.children.forEach(function (c) {
        if (c.top.getBoundingClientRect().top + scrollY <= p) activeChild = c;
      });
    });
    var activeEntry = activeChild || activeSection;
    current.entries.forEach(function (e) {
      e.link.classList.toggle('active', e === activeEntry);
      e.link.removeAttribute('aria-current');
      e.station.classList.toggle('active', e === activeEntry);
      if (e.fold) e.children.forEach(function (c) {
        c.link.classList.toggle('active', c === activeEntry);
        c.link.removeAttribute('aria-current');
        c.station.classList.toggle('active', c === activeEntry);
      });
    });
    if (activeEntry) {
      activeEntry.link.setAttribute('aria-current', 'location');
      setActiveSection(activeSection);
      if (!activeEntry.link.closest('.fold:not(.open)')) keepVisible(activeEntry.link);
    }
  }
  function keepVisible(el) {
    var er = el.getBoundingClientRect(), nr = nav.getBoundingClientRect();
    if (er.top < nr.top) nav.scrollTop -= (nr.top - er.top) + 8;
    else if (er.bottom > nr.bottom) nav.scrollTop += (er.bottom - nr.bottom) + 8;
  }
  function updateBarProgress(color) {
    var fill = document.getElementById('bar-progress-fill');
    if (!fill) return;
    var doc = document.documentElement;
    fill.style.width = (clamp01(scrollY / Math.max(1, doc.scrollHeight - innerHeight)) * 100) + '%';
    fill.style.background = color;
  }

  function frame(now) {
    var dt = Math.min(64, lastT ? now - lastT : 16); lastT = now;
    var t = navTarget(docTarget());
    tipY += (t - tipY) * (1 - Math.pow(0.84, dt / 16));
    render();
    if (Math.abs(t - tipY) > 0.4) raf = requestAnimationFrame(frame);
    else { raf = 0; lastT = 0; }
  }
  function kick() {
    if (reduce) { tipY = navTarget(docTarget()); render(); }
    else if (!raf) raf = requestAnimationFrame(frame);
  }

  addEventListener('scroll', kick, { passive: true });
  addEventListener('resize', layout);
  if (document.fonts && document.fonts.ready) document.fonts.ready.then(layout);
  if (window.ResizeObserver) new ResizeObserver(layout).observe(list);
  document.addEventListener('focusin', markActive);
  layout();

  // --- Narrow-viewport drawer: the same nav, opened off-canvas. ---
  var tocBtn = document.getElementById('toc-toggle');
  var scrim = document.getElementById('scrim');
  function closeDrawer(focusBtn) {
    nav.classList.remove('open'); scrim.hidden = true;
    tocBtn.setAttribute('aria-expanded', 'false');
    document.body.classList.remove('locked');
    if (focusBtn) tocBtn.focus();
  }
  function openDrawer() {
    nav.classList.add('open'); scrim.hidden = false;
    tocBtn.setAttribute('aria-expanded', 'true');
    document.body.classList.add('locked');
    nav.focus();
  }
  if (tocBtn) {
    tocBtn.addEventListener('click', function () {
      if (nav.classList.contains('open')) closeDrawer(true); else openDrawer();
    });
    scrim.addEventListener('click', function () { closeDrawer(false); });
    list.addEventListener('click', function (e) { if (e.target.closest('a') && nav.classList.contains('open')) closeDrawer(false); });
    document.addEventListener('keydown', function (e) {
      if (e.key === 'Escape' && nav.classList.contains('open')) closeDrawer(true);
    });
  }
})();

// Code blocks: <pre><code class="language-X"> gets a label and token colours,
// and every block gets a copy button in its top-right corner. Rules are tried
// left to right at each position; none may contain a capturing group (the
// group number picks the token class).
(function () {
  var LANGS = {
    sh: ['shell', [[/#.*/, 'c'], [/"[^"]*"|'[^']*'/, 's'], [/\b[A-Z_]+(?==)/, 'k'],
      [/(?<=\s)--?[\w-]+/, 'f'], [/^[\w.\/-]+/, 'x']]],
    ini: ['printer.cfg', [[/[#;].*/, 'c'], [/^\[[^\]]*\]/, 'h'], [/^\w+(?=\s*[:=])/, 'k'],
      [/\b(?:True|False)\b|(?<=:\s*)-?\d+(?:\.\d+)?$/, 'n']]],
    gcode: ['console', [[/^[A-Z][A-Z0-9_]+/, 'x'], [/\b[A-Z_]+(?==)/, 'k'], [/(?<==)\S+/, 's']]],
    json: ['json', [[/"[^"]*"(?=\s*:)/, 'k'], [/"[^"]*"/, 's'], [/-?\b\d+\b/, 'n']]],
    log: ['output', [[/^klipper_mmu \w+:/, 'c'], [/\battempt \d+\b/, 'k'], [/\b\w+(?==)/, 'k'],
      [/(?<==)\S+/, 's'], [/\d+(?:\.\d+)?(?: ?(?:mm|%|s))?/, 'n']]]
  };
  var ICON_COPY = '<svg viewBox="0 0 16 16" aria-hidden="true" fill="none" stroke="currentColor" stroke-width="1.5" stroke-linejoin="round"><rect x="5.5" y="5.5" width="8" height="8" rx="1.5"/><path d="M10.5 3.5 V3 A1.5 1.5 0 0 0 9 1.5 H4 A1.5 1.5 0 0 0 2.5 3 V9 A1.5 1.5 0 0 0 4 10.5 H4.5"/></svg>';
  var ICON_DONE = '<svg viewBox="0 0 16 16" aria-hidden="true" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><path d="M3 8.5 L6.5 12 L13 4.5"/></svg>';
  function esc(t) { return t.replace(/&/g, '&amp;').replace(/</g, '&lt;'); }
  function highlight(code, rules) {
    var re = new RegExp(rules.map(function (r) { return '(' + r[0].source + ')'; }).join('|'), 'g');
    code.innerHTML = code.textContent.split('\n').map(function (line) {
      var out = '', last = 0, m;
      re.lastIndex = 0;
      while ((m = re.exec(line)) && m[0]) {
        var g = 1; while (!m[g]) g++;
        out += esc(line.slice(last, m.index)) + '<span class="t-' + rules[g - 1][1] + '">' + esc(m[0]) + '</span>';
        last = re.lastIndex;
      }
      return '<span class="line">' + out + esc(line.slice(last)) + '</span>';
    }).join('\n');
  }
  // Clipboard API where allowed; otherwise select the text and try the old copy command.
  function copy(code, btn) {
    function done(ok) {
      btn.innerHTML = ok ? ICON_DONE : ICON_COPY;
      btn.classList.toggle('done', ok);
      btn.title = ok ? 'Copied' : 'Press Ctrl+C to copy';
      setTimeout(function () { btn.innerHTML = ICON_COPY; btn.classList.remove('done'); btn.title = 'Copy'; }, 1600);
    }
    var text = code.textContent;
    if (navigator.clipboard && window.isSecureContext) {
      navigator.clipboard.writeText(text).then(function () { done(true); }, function () { done(false); });
      return;
    }
    var range = document.createRange(), sel = getSelection();
    range.selectNodeContents(code); sel.removeAllRanges(); sel.addRange(range);
    var ok = false;
    try { ok = document.execCommand('copy'); } catch (e) {}
    done(ok);
  }
  document.querySelectorAll('pre > code').forEach(function (code) {
    var pre = code.parentNode, lang = LANGS[(code.className.match(/language-(\w+)/) || [])[1]];
    if (lang) { highlight(code, lang[1]); pre.dataset.lang = lang[0]; }
    var box = document.createElement('div'), btn = document.createElement('button');
    box.className = 'codeblock';
    pre.parentNode.insertBefore(box, pre); box.appendChild(pre);
    btn.type = 'button'; btn.className = 'copy'; btn.title = 'Copy';
    btn.setAttribute('aria-label', 'Copy to clipboard');
    btn.innerHTML = ICON_COPY;
    btn.addEventListener('click', function () { copy(code, btn); });
    box.appendChild(btn);
  });
})();
