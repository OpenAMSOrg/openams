# Host DiffA/DiffB calibration encoder

`encode_calibration(record)` in `host/calibration.py` turns one measured table into bytes. It does no I/O, does not start firmware, and does not move the MMU.

The encoder is pure. The opt-in host extra reads a measured JSON record from `diff_calibration_file`, calls this encoder, and places the bytes in one ordinary `config_km_diff_calibration` config command with a fresh OID before `finalize_config`. That command participates in Klipper's configuration CRC; a changed file takes effect through normal MCU reconfiguration and reset, not a live update.

Store measured provenance outside this encoder. It does not import or validate MMU travel or timing captures.

For an AFC Lite BoxTurtle, the actuator-disabled firmware can acquire raw
counts before a calibration object exists. Configure `diff_probe: True` on the
`[klipper_mmu BoxTurtle]` host section without `diagnostic_startup`,
`diff_calibration_file`, or `openams_motion`. At each known, manually positioned
buffer location, run `KLIPPER_MMU_DIFF_PROBE MMU=BoxTurtle` and record the fresh
`a_raw`, `b_raw`, and observation times. Measure the actual reference and signal
voltages separately. Probe mode never starts a motor and requires an MCU reset
before switching to the calibrated diagnostic startup mode. See the
[staged BoxTurtle record](../docs/boxturtle-h723-acceptance.md).

The file path must be absolute (a leading `~` is expanded), the UTF-8 JSON file must be at most 8192 bytes, and duplicate JSON keys are rejected. File output adds no command. The host requires protocol and integration constants 1, the Diff buffer and calibration constants 1, and `KLIPPER_MMU_ACTUATION` 0 or 1. Value 1 also requires hosted diagnostic startup. The MCU validates and publishes one immutable calibration object; shutdown denies it until MCU reset. With diagnostic startup enabled, its init command follows the ordinary calibration config command. Calibration delivery does not itself grant motion permission or qualify hardware.

## Record

Exact keys:

- channel_map: `a_low_b_high` (wire value 0, DiffA low and DiffB high) or `b_low_a_high` (wire value 1, DiffB low and DiffA high).
- vref_mv: ADC reference, millivolts, integer 1..65535.
- min_valid_mv: lowest valid sensor voltage, millivolts, integer 0..vref_mv-1.
- max_valid_mv: highest valid sensor voltage, millivolts. Use 0 when the upper bound is unused, otherwise an integer from min_valid_mv+1 through vref_mv.
- path_tolerance_counts: tolerance in ADC counts, integer 1..4095.
- points: list of 2 to 8 points.

Each point has exact keys `a_raw`, `b_raw`, and `position_permille`. All three are integers. Boolean values are invalid. `a_raw` is the physical DiffA 12-bit count and `b_raw` is the physical DiffB 12-bit count, each 0..4095. Do not put millivolts in those fields. `position_permille` is position along the measured path in permille. Point 0 is position 0 and the last point is position 1000. Each middle point is 1..999, and positions strictly increase. Both raw channels are nondecreasing from point to point. A step that leaves both raw counts unchanged is invalid.

## Encoding

Little-endian, length 6*n+6, at most 54 bytes:

1. u8 channel_map
2. u8 point count
3. four u16 values: vref_mv, min_valid_mv, max_valid_mv, path_tolerance_counts
4. each point: u16 canonical low-channel raw, u16 canonical high-channel raw
5. each middle point only: u16 position_permille

For `b_low_a_high`, the encoder swaps each measured physical A/B pair into canonical low/high order; the decoder applies the same map to live physical samples. Endpoint positions are omitted because they are fixed at 0 and 1000. The MCU checks the record again.

## SYNTHETIC EXAMPLE

Not a measurement. Do not treat these numbers as captured calibration.

- channel_map: a_low_b_high
- vref_mv: 3300
- min_valid_mv: 150
- max_valid_mv: 3200
- path_tolerance_counts: 25
- points: (a_raw 200, b_raw 2200, position_permille 0), (a_raw 1000, b_raw 3000, position_permille 500), (a_raw 2000, b_raw 3900, position_permille 1000)

Three points encode to 24 bytes.
