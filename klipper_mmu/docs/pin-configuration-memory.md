# Pin configuration memory

`km_pin_config_t` contains both role-indexed pin IDs and the complete pin
claim list. It occupies 1,652 bytes in the reviewed build. Startup used to
retain separate copies in the parser, board startup, switch factory, and
H723 resource factory. The parser now exposes one validated
`km_config_pins_view()` with static lifetime. Setup consumers hold a
read-only pointer to that immutable map; the compatibility
`km_config_pins()` API still copies it when independent caller storage is
needed.

The view is created lazily on first use and cached only after every configured
pin parses and the full pin claim plan validates. Failure does not publish a
partial view. Setup remains serialized, and callers must not modify the view.
This does not replace the complete board/peripheral conflict check: timer and
ADC claims still join the pin claims and are validated before physical setup.

The focused comparison used the same pinned upstream archive
`ce7002bedf37e938bb483572949f3703ac6476cb`, a Custom/1 F407 USB configuration,
and the product baseline commit `4cf9a33` for the before image. The baseline
ELF had 17,856 bytes of BSS; the candidate has 12,908 bytes, a measured saving
of 4,948 bytes. The baseline held four 1,652-byte pin plans; the candidate
holds one plan plus a four-byte pointer in board startup.

LTO stack-usage reports show the pin factory changing from a 168-byte
`km_config_pins()` frame to a 176-byte `km_config_pins_view()` frame, while
`km_board_startup_begin()` drops from 416 to 408 bytes. The largest observed
frame in both images is 488 bytes. That individual-frame measurement does
**not** establish total stack fit: root subsequently confirmed that the 104-byte
controller timer frame calls the 472-byte core update, which calls a 408-byte
trajectory planner, before counting further callees and interrupted foreground
work. The old 512-byte reservation is insufficient. See
[the shared stack reservation](stack-reservation.md). ELF,
configuration, size reports, symbol lists, full LTO frame reports, and build
scripts are retained in
that run's `build/pin-config-memory/`. The measurement proves the
F407 footprint change; it does not establish final F0 fit.

Root integration reruns all 63 pin/resource configuration cases, the 27 board
startup cases, 13 PWM fake cases, composed PWM lifecycle, and seven raw ADC
probe cases. All pass. Evidence is under `build/review-pin-memory*`; this
includes consumer fixtures updated for the read-only view contract.
