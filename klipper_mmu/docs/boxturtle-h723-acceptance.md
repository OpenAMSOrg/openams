# BoxTurtle H723 bench acceptance

The [release matrix](../tests/release_matrix/README.md) saves USB and CAN
versions of each analog image with the same AFC Lite MMU pin map. Select the
transport that matches the board's actual connection and bootloader: USB on
PA11/PA12 uses no bootloader offset; CAN on PB8/PB9 uses a 128 KiB offset.
Both use the board's 25 MHz crystal. The `-safe` image has hosted diagnostic
startup but denies actuator START; the matching image without `-safe` permits
START after firmware startup reaches READY. None has passed a physical board
test. Use this record to qualify one exact board and wiring revision;
passing a native test or linking an ELF does not replace these observations.

1. **Unpowered wiring:** Record AFC Lite and BoxTurtle revisions. Confirm SW1
   PC4 is DiffA, SW2 PC5 is DiffB, SW11 PE12 is HUB, SW12 PE13 is bay 0,
   SW7–SW10 PE8–PE11 are load switches, and SW6 PE7 is unused: these images
   end LOAD on FPS arrival without a toolhead switch. Remove the old switched-buffer leads from SW11/SW12. Verify no
   STEP, DIR, UART, rewind output, or sensor lead aliases another assigned pin.
   The [manufacturer's v0.1 schematic](https://github.com/xbst/AFC-Lite/blob/master/Docs/AFC-Lite_Schematic.pdf)
   shows each SW connector with 3.3 V, ground, and a signal routed directly
   to its named MCU pin; SW1 reaches PC4 and SW2 reaches PC5 without a shown
   signal-conditioning stage. Confirm the actual board revision has this path.
2. **Electrical inputs:** With motor power isolated, measure common ground,
   supply, each switch's idle/active level and the complete DiffA/DiffB travel.
   Confirm DiffA remains in its expected lower half and DiffB in its expected
   upper half around nominal 1.65 V, with neither input exceeding the MCU's
   0–3.3 V range. With the actuator-disabled image, use the read-only probe
   mode below to record raw ADC counts at two or more known buffer positions,
   then create the measured [calibration record](../host/calibration.md). Do not
   substitute the synthetic example from the tests. Because the schematic shows
   a direct signal path, check source impedance and ADC settling at the selected
   sampling time on the fitted sensor and cable; connector pin compatibility
   alone does not establish accurate readings.
   Probe mode uses this host section, with no calibration file or actuator
   startup configured:

   ```ini
   [klipper_mmu BoxTurtle]
   mcu: mcu
   diff_probe: True
   ```

   Query `KLIPPER_MMU_DIFF_PROBE MMU=BoxTurtle` after each manual buffer
   position change and record both raw counts and reported timestamps. A query
   without a fresh valid pair is an error, not a calibration point. Probe mode
   claims the local ADC pair until MCU reset. After recording the table, reset
   the MCU and replace `diff_probe: True` with `diagnostic_startup: True` plus
   an absolute `diff_calibration_file` path before step 3. A host-only restart
   must not be assumed to release the ADC claim.
3. **Diagnostic image:** After the unpowered and electrical checks, use the
   actuator-disabled image for a supervised startup. Confirm Klipper's normal
   configuration and calibration arrive before diagnostic startup. Record the
   startup state, board identity, pin reservations, fresh paired-ADC timing,
   decoded buffer position, and every local switch transition. Verify actuator
   START is rejected and the motor outputs remain inactive, including after a
   host disconnect and shutdown. Measure TMC current settings and safe-off
   levels without commanding motion.
4. **Actuator image:** Only after the previous checks pass, inspect current,
   direction, STEP pulse spacing, PWM duty and reversal dead time on one isolated
   lane before loading filament. Then run the step-estimated calibration
   ([FPS arrival](fps-arrival.md#defaults-and-per-printer-bounds)), restart,
   and witness LOAD to the real HUB and FPS arrival at the extruder, FOLLOW
   with live Diff feedback, and UNLOAD through HUB-clear
   detection plus the configured post-HUB retract. Measure the actual distance
   from that switch edge to the parked filament end on every lane; the 65 mm
   (47,273 microsteps at the reference mechanics) menu default comes from AFC
   reference geometry and is not a measured BoxTurtle clearance. Check the other
   three lanes stay still,
   the exact retained terminals reach
   OpenAMS and are acknowledged once, and loss of fresh feedback, host lease or
   power causes the specified stop. Repeat on every fitted lane.

Save the image's `manifest.json` with the firmware binary, `.config`, ELF and
dictionary it identifies. Record host/OpenAMS versions, calibration file hash,
wiring and measurement captures, observed timings, and pass/fail results
together under a dated `docs/hardware/` directory. Use a clean product commit
for a release candidate; a manifest marked `product_dirty` is a development
build. A failed step keeps the actuator image out of service until the cause
is corrected and rechecked.
