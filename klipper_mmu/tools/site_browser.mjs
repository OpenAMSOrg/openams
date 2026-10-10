#!/usr/bin/env node
// SPDX-License-Identifier: GPL-3.0-or-later
// tools/site_browser.mjs: a dependency-free Chrome DevTools Protocol driver
// for eyeballing (and scripting checks against) a local page, such as the
// docs site's scroll-reactive nav. No npm packages: Node's global
// WebSocket talks CDP directly to a headless Chrome we launch ourselves.
//
// Usage:
//   node tools/site_browser.mjs URL [--size WxH] [--dark] [--timeout MS]
//     [--scroll Y] [--click SELECTOR] [--key NAME] [--wait MS]
//     [--eval EXPR] [--shot FILE] ...
//
// Steps run in the order given on the command line, as many times as you
// like, e.g.:
//   node tools/site_browser.mjs http://localhost:8000/ --size 1440x900 \
//     --wait 300 --shot top.png --scroll 2000 --wait 400 --shot mid.png
//
// --scroll Y       window.scrollTo(0, Y)
// --click SEL      click the first element matching a CSS selector
// --key NAME       dispatch a real key press (e.g. Escape) at the page
// --wait MS        pause between steps
// --eval EXPR      evaluate a JS expression in the page, print its JSON
// --shot FILE      save a full-viewport PNG screenshot to FILE
//
// Console errors and uncaught exceptions in the page are printed to
// stderr as they happen and summarised at the end. The whole run is
// bounded by --timeout (default 60000ms); Chrome is always killed on exit.
//
// Set CHROME_PATH to override which browser binary is launched.

import { spawn, spawnSync } from 'node:child_process';
import { mkdtempSync, readFileSync, existsSync, rmSync, writeFileSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join } from 'node:path';

function help() {
  console.log(`site_browser.mjs URL [options] [steps...]

Options:
  --size WxH       viewport size, e.g. 1440x900 (default 1280x800)
  --dark           emulate prefers-color-scheme: dark
  --timeout MS     hard overall timeout, kills Chrome (default 60000)
  --help           show this message

Steps (run in the order given, may repeat):
  --scroll Y       scroll to document Y position
  --click SEL      click the first element matching a CSS selector
  --key NAME       dispatch a key press, e.g. Escape
  --wait MS        pause
  --eval EXPR      evaluate JS in the page, print the JSON result
  --shot FILE      save a full-viewport PNG screenshot

Env:
  CHROME_PATH      override the browser binary (default: google-chrome[-stable])
`);
}

function parseArgs(argv) {
  if (argv.includes('--help') || argv.length === 0) { help(); process.exit(0); }
  const opts = { size: '1280x800', dark: false, timeout: 60000, steps: [] };
  let url = null;
  for (let i = 0; i < argv.length; i++) {
    const a = argv[i];
    if (a === '--size') opts.size = argv[++i];
    else if (a === '--dark') opts.dark = true;
    else if (a === '--timeout') opts.timeout = Number(argv[++i]);
    else if (a === '--scroll') opts.steps.push({ type: 'scroll', value: Number(argv[++i]) });
    else if (a === '--click') opts.steps.push({ type: 'click', value: argv[++i] });
    else if (a === '--key') opts.steps.push({ type: 'key', value: argv[++i] });
    else if (a === '--wait') opts.steps.push({ type: 'wait', value: Number(argv[++i]) });
    else if (a === '--eval') opts.steps.push({ type: 'eval', value: argv[++i] });
    else if (a === '--shot') opts.steps.push({ type: 'shot', value: argv[++i] });
    else if (!url && !a.startsWith('--')) url = a;
    else throw new Error('unrecognised argument: ' + a);
  }
  if (!url) throw new Error('a URL is required (see --help)');
  const m = /^(\d+)x(\d+)$/.exec(opts.size);
  if (!m) throw new Error('--size must look like 1440x900');
  opts.width = Number(m[1]); opts.height = Number(m[2]);
  opts.url = url;
  return opts;
}

function findChrome() {
  if (process.env.CHROME_PATH) return process.env.CHROME_PATH;
  for (const c of ['google-chrome', 'google-chrome-stable', 'chromium', 'chromium-browser']) {
    if (spawnSync('which', [c]).status === 0) return c;
  }
  return 'google-chrome'; // let the spawn below fail with a clear ENOENT
}

// Minimal CDP client over a single page-target WebSocket.
class CDP {
  constructor(ws) {
    this.ws = ws;
    this.nextId = 0;
    this.pending = new Map();
    this.listeners = new Map();
    ws.addEventListener('message', (ev) => {
      const msg = JSON.parse(ev.data);
      if (msg.id !== undefined && this.pending.has(msg.id)) {
        const p = this.pending.get(msg.id);
        this.pending.delete(msg.id);
        if (msg.error) p.reject(new Error(msg.error.message));
        else p.resolve(msg.result);
      } else if (msg.method) {
        for (const cb of this.listeners.get(msg.method) || []) cb(msg.params);
      }
    });
  }
  send(method, params = {}) {
    const id = ++this.nextId;
    return new Promise((resolve, reject) => {
      this.pending.set(id, { resolve, reject });
      this.ws.send(JSON.stringify({ id, method, params }));
    });
  }
  on(method, cb) {
    if (!this.listeners.has(method)) this.listeners.set(method, []);
    this.listeners.get(method).push(cb);
  }
}

function sleep(ms) { return new Promise((r) => setTimeout(r, ms)); }

async function waitFor(predicate, timeoutMs, pollMs = 50) {
  const start = Date.now();
  while (Date.now() - start < timeoutMs) {
    const v = predicate();
    if (v) return v;
    await sleep(pollMs);
  }
  return null;
}

async function main() {
  const opts = parseArgs(process.argv.slice(2));
  const userDataDir = mkdtempSync(join(tmpdir(), 'site-browser-'));
  const chrome = findChrome();
  const args = [
    '--headless=new', '--disable-gpu', '--no-sandbox', '--hide-scrollbars',
    '--remote-debugging-port=0', '--user-data-dir=' + userDataDir,
    '--window-size=' + opts.width + ',' + opts.height,
  ];
  const proc = spawn(chrome, args, { stdio: 'ignore' });
  let exitCode = 0;
  const timer = setTimeout(() => {
    console.error('site_browser: hard timeout (' + opts.timeout + 'ms) reached, killing Chrome');
    proc.kill('SIGKILL');
    process.exit(1);
  }, opts.timeout);
  timer.unref?.();

  try {
    const portFile = join(userDataDir, 'DevToolsActivePort');
    const found = await waitFor(() => existsSync(portFile) && readFileSync(portFile, 'utf8'), 10000);
    if (!found) throw new Error('Chrome never wrote DevToolsActivePort (did it start?)');
    const port = Number(found.split('\n')[0]);

    const res = await fetch('http://127.0.0.1:' + port + '/json/new?about:blank', { method: 'PUT' })
      .catch(() => fetch('http://127.0.0.1:' + port + '/json/new?about:blank'));
    const target = await res.json();
    const ws = new WebSocket(target.webSocketDebuggerUrl);
    await new Promise((resolve, reject) => {
      ws.addEventListener('open', resolve);
      ws.addEventListener('error', () => reject(new Error('WebSocket to Chrome failed')));
    });
    const client = new CDP(ws);

    await client.send('Page.enable');
    await client.send('Runtime.enable');
    await client.send('DOM.enable');

    const consoleIssues = [];
    client.on('Runtime.consoleAPICalled', (p) => {
      if (p.type === 'error') {
        consoleIssues.push(p.args.map((a) => a.value ?? a.description ?? String(a.type)).join(' '));
      }
    });
    client.on('Runtime.exceptionThrown', (p) => {
      const d = p.exceptionDetails;
      consoleIssues.push('uncaught exception: ' + (d.exception?.description || d.text));
    });

    await client.send('Emulation.setDeviceMetricsOverride', {
      width: opts.width, height: opts.height, deviceScaleFactor: 1, mobile: false,
    });
    if (opts.dark) {
      await client.send('Emulation.setEmulatedMedia', {
        features: [{ name: 'prefers-color-scheme', value: 'dark' }],
      });
    }

    const loaded = new Promise((resolve) => client.on('Page.loadEventFired', resolve));
    await client.send('Page.navigate', { url: opts.url });
    const gotLoad = await Promise.race([loaded.then(() => true), sleep(20000).then(() => false)]);
    if (!gotLoad) console.error('site_browser: warning: load event never fired within 20s');

    for (const step of opts.steps) {
      if (step.type === 'scroll') {
        await client.send('Runtime.evaluate', { expression: 'window.scrollTo(0, ' + step.value + ')' });
      } else if (step.type === 'click') {
        const sel = JSON.stringify(step.value);
        const r = await client.send('Runtime.evaluate', {
          expression: '(function(){var e=document.querySelector(' + sel + ');if(!e)return "not found";e.click();return "ok";})()',
          returnByValue: true,
        });
        if (r.result.value !== 'ok') console.error('site_browser: --click ' + step.value + ': ' + r.result.value);
      } else if (step.type === 'key') {
        const keyMap = { Escape: { key: 'Escape', code: 'Escape', windowsVirtualKeyCode: 27 } };
        const k = keyMap[step.value] || { key: step.value, code: step.value };
        await client.send('Input.dispatchKeyEvent', { type: 'keyDown', ...k });
        await client.send('Input.dispatchKeyEvent', { type: 'keyUp', ...k });
      } else if (step.type === 'wait') {
        await sleep(step.value);
      } else if (step.type === 'eval') {
        const r = await client.send('Runtime.evaluate', { expression: step.value, returnByValue: true, awaitPromise: true });
        if (r.exceptionDetails) {
          console.error('site_browser: --eval error: ' + (r.exceptionDetails.text || ''));
          exitCode = 1;
        } else {
          console.log(JSON.stringify(r.result.value));
        }
      } else if (step.type === 'shot') {
        const shot = await client.send('Page.captureScreenshot', { format: 'png' });
        writeFileSync(step.value, Buffer.from(shot.data, 'base64'));
      }
    }

    if (consoleIssues.length) {
      console.error('site_browser: ' + consoleIssues.length + ' console error(s)/exception(s):');
      for (const line of consoleIssues) console.error('  - ' + line);
    } else {
      console.error('site_browser: no console errors');
    }
  } catch (err) {
    console.error('site_browser: ' + err.message);
    exitCode = 1;
  } finally {
    clearTimeout(timer);
    proc.kill('SIGKILL');
    try { rmSync(userDataDir, { recursive: true, force: true }); } catch (e) { /* best effort */ }
  }
  process.exit(exitCode);
}

main();
