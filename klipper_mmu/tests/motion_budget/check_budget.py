#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
"""Check provisional BoxTurtle Kconfig motion defaults against AFC travel."""
from pathlib import Path
import re


ROOT = Path(__file__).resolve().parents[2]
KCONFIG = (ROOT / "mcu/Kconfig.control").read_text()
TMC_KCONFIG = (ROOT / "mcu/Kconfig.tmc").read_text()


def default(symbol: str) -> int:
    match = re.search(
        rf"config {symbol}\n(?:(?!\nconfig ).)*?default (\d+) if KLIPPER_MMU_DEVICE_BOXTURTLE",
        KCONFIG,
        re.S,
    )
    assert match, f"missing BoxTurtle default for {symbol}"
    return int(match.group(1))


def plain_default(symbol: str, text: str) -> int:
    """The one unconditional default; the shared feeder mechanics use these."""
    match = re.search(
        rf"config {symbol}\n(?:(?!\nconfig ).)*?default (\d+)\n", text, re.S
    )
    assert match, f"missing default for {symbol}"
    return int(match.group(1))


# The same arithmetic as mcu/feeder_mechanics.h, in the same units: rotation
# distance is micrometres of filament per output rotation.
full_steps = plain_default("KLIPPER_MMU_FEEDER_FULL_STEPS", KCONFIG)
microsteps = plain_default("KLIPPER_MMU_TMC_MICROSTEPS", TMC_KCONFIG)
gear_a = plain_default("KLIPPER_MMU_FEEDER_GEAR_RATIO_A", KCONFIG)
gear_b = plain_default("KLIPPER_MMU_FEEDER_GEAR_RATIO_B", KCONFIG)
rotation_um = plain_default("KLIPPER_MMU_FEEDER_ROTATION_DISTANCE_UM", KCONFIG)
assert (full_steps, microsteps, gear_a, gear_b, rotation_um) == (200, 16, 50, 10, 22000)
steps_per_m = full_steps * microsteps * gear_a * 1000000 // (gear_b * rotation_um)
steps_per_mm = steps_per_m / 1000
# The reference BoxTurtle mechanics: 200 full steps, 16 microsteps, a 50:10
# gearbox and a 22 mm wheel, i.e. 727.27 microsteps per millimetre.
assert steps_per_m == 727272

feed = default("KLIPPER_MMU_FEED_SPEED_MM_S")
retract = default("KLIPPER_MMU_RETRACT_SPEED_MM_S")
accel = default("KLIPPER_MMU_ACCELERATION_MM_S2")
hub_timeout = default("KLIPPER_MMU_HUB_TIMEOUT") / 1000
arrival_timeout = default("KLIPPER_MMU_ARRIVAL_TIMEOUT") / 1000
retract_timeout = default("KLIPPER_MMU_RETRACT_TIMEOUT") / 1000
cal_retract_timeout = default("KLIPPER_MMU_CAL_RETRACT_TIMEOUT") / 1000
clear_mm = default("KLIPPER_MMU_UNLOAD_HUB_CLEAR_MM")
fps_min_mm = default("KLIPPER_MMU_FPS_ARRIVAL_MIN_MM")
fps_max_mm = default("KLIPPER_MMU_FPS_ARRIVAL_MAX_MM")

# The core takes microsteps per second against its 200,000 ceiling
# (KM_MOTION_MAX_VELOCITY_STEPS_S), so convert once here to check the menu.
assert 0 < feed * steps_per_mm <= 200000 and 0 < retract * steps_per_mm <= 200000
# The AFC reference distances, now spelled in millimetres of filament.
assert clear_mm == 65 and fps_min_mm == 100 and fps_max_mm == 1900


def move_seconds(distance_mm: float, speed_mm_s: float) -> float:
    return distance_mm / speed_mm_s + speed_mm_s / accel


# Require at least 20% over the ideal trapezoid estimate.
assert move_seconds(155, feed) * 1.2 <= hub_timeout
assert move_seconds(1725, feed) * 1.2 <= arrival_timeout
assert move_seconds(1725, retract) * 1.2 <= retract_timeout
# FPS arrival's outer travel limit covers the reference Bowden and is reached
# before the arrival deadline, so NO_PROGRESS reports travel, not time.
assert move_seconds(fps_max_mm, feed) * 1.2 <= arrival_timeout
# A step-estimated PTFE calibration retracts from arrival back past HUB.
assert move_seconds(fps_max_mm, retract) * 1.2 <= cal_retract_timeout
# LOAD retries: the last attempt feeds at half speed within twice both
# deadlines. Retracting behind HUB from maximum travel fits the retract
# deadline, though with only about 15% over the ideal estimate.
assert move_seconds(155, feed / 2) * 1.2 <= 2 * hub_timeout
assert move_seconds(fps_max_mm, feed / 2) * 1.2 <= 2 * arrival_timeout
assert move_seconds(fps_max_mm + clear_mm, retract) * 1.15 <= retract_timeout
# The whole LOAD ends within KM_LOAD_BUDGET_MS, which holds one worst-case
# attempt and stays below OpenAMS's 300 s disconnect backstop.
budget = int(re.search(r"#define KM_LOAD_BUDGET_MS (\d+)u", (
    ROOT / "include/klipper_mmu/operation_core.h").read_text()).group(1)) / 1000
assert hub_timeout + arrival_timeout <= budget and budget * 1.2 <= 300
print("BoxTurtle provisional motion defaults fit the reference travel budget.")
