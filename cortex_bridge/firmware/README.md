# Cortex Bridge firmware packages

This folder ships the bridge firmware that `cortex-bridge.py update` and the
Mainsail panel install. Customers receive new packages by updating OpenAMS;
nothing else is downloaded.

## What may go here

Only signed, encrypted `.oac2` packages for the Cortex Bridge, as produced by
the offline release host. A package is safe to publish: it contains no
firmware plaintext and no key, and the bridge's boot refuses anything not
signed for it. Never commit unencrypted images, keys, or the release host's
working files.

## Adding a release

1. Copy the package here as `bridge-<version>.oac2`.
2. Add an entry to `manifest.json`:

   ```json
   {
    "schema": 1,
    "packages": [
     {
      "version": 3,
      "file": "bridge-3.oac2",
      "sha256": "<sha256sum of the file>",
      "released": "2026-11-01",
      "notes": "One line a customer can read: what changed."
     }
    ]
   }
   ```

3. Check it: `python3 tools/cortex-bridge.py firmware` lists the package with
   no `[file missing]` or `[checksum mismatch]` note, and
   `python3 -m unittest discover -s test -p 'test_cortex_bridge_setup.py'`
   passes (it checks every listed package).
4. Commit and push. Printers pick it up at their next OpenAMS update, and
   `cortex-bridge.py update` (or the panel) offers it.

Keep older packages listed only while a customer might need to step back to
one; the bridge refuses versions below its stored minimum anyway.
