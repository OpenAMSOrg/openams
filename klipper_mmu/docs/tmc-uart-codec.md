# Framing for Klipper's shared TMC UART driver

The portable TMC2209 controller produces raw four-byte read requests and
eight-byte write requests, and consumes eight-byte responses. Klipper's MCU
UART engine instead accepts packed serial bits: start bit, eight data bits,
stop bit, least-significant bit first. These occupy five or ten bytes for the
supported packet sizes.

`km_tmc_uart_encode()` adds that framing and returns the packed length.
`km_tmc_uart_decode()` removes and validates the framing of a ten-byte reply.
They perform no I/O, timing, scheduling or allocation. They leave output unchanged
on failure and allow overlapping input/output. The encoded output buffer must
hold ten bytes; bytes after the returned length remain unchanged.

Protocol validation stays in `km_tmc2209_decode()`: sync byte, response address,
register and CRC are checked there after serial framing is decoded. Neither
codec function rewrites the sync byte or recalculates CRC. The current portable
controller uses sync `0x05`; the inspected Klipper host uses `0xf5` for requests.
Both supplied forms are packed exactly as Klipper's `_add_serial_bits()` packs
them. This does not claim equal physical timing or silently enable the engine's
special sync-nibble timing path.

The codec is ready for the shared-driver transport adapter. It does not replace
the existing UART binding yet, initialize a peripheral or establish chip
readiness. Local UART API lifecycle repairs and transport integration are still
required. See the acceptance record in `docs/plan/status-history.md`.
