# Pinned upstream reference sources

`Happy-Hare-c54c736043a49f901ab685fa031d4f4edd066dc6.tar.gz` is an
unmodified source archive from the public
[Happy Hare repository](https://github.com/moggieuk/Happy-Hare) at commit
`c54c736043a49f901ab685fa031d4f4edd066dc6`. Its SHA-256 is
`18673d41c2682d5f52df0893ded95c5e359ff029f9e46640f54db571ddc9726a`.
All 456 Happy Hare paths recorded in the project's
`source-inventory.json` inventory were
byte-checked against the archive and matched. The archive includes upstream's
license and is a development reference, not a firmware or host build input.

`AFC-Klipper-Add-On-5ea84ea6fab75b9b49db090f309154d1e52272bb.tar.gz`
is a `git archive` of the local AFC repository at committed revision
`5ea84ea6fab75b9b49db090f309154d1e52272bb`. Its SHA-256 is
`d6aa55b25637a76ea1e407b68caa7f45a0f7a`. All 148 inventoried AFC
paths match the archive byte for byte. The original inventory was made from a
working tree, but these inventoried files match the committed revision. The
archive includes AFC's GPL-3.0 license and is likewise a reference only.

To inspect the source without relying on `/tmp`, extract the archive here:

```sh
mkdir -p references/worktree
tar --no-same-owner -xzf references/Happy-Hare-c54c736043a49f901ab685fa031d4f4edd066dc6.tar.gz -C references/worktree
mkdir -p references/worktree/AFC-Klipper-Add-On-5ea84ea6fab75b9b49db090f309154d1e52272bb
tar --no-same-owner -xzf references/AFC-Klipper-Add-On-5ea84ea6fab75b9b49db090f309154d1e52272bb.tar.gz -C references/worktree/AFC-Klipper-Add-On-5ea84ea6fab75b9b49db090f309154d1e52272bb
```

`references/worktree/` is ignored because the pinned archive is the committed
source of truth. Implement shared behavior in this add-on and its OpenAMS
adapter; do not edit the extracted reference tree.
