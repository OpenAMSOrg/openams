# SPDX-License-Identifier: GPL-3.0-or-later
"""tools/thumb_cost.py end to end: build a bench() driver with
arm-none-eabi-gcc, execute the ELF in Unicorn, and check that the instruction
counts are exact and repeatable, that libgcc helper calls are attributed, and
that the real step segment generator can be measured for both targets."""
import os
from pathlib import Path
import shutil
import sys
import tempfile
import textwrap
import unittest

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[1]
sys.path.insert(0, str(ROOT / "tools"))
import thumb_cost  # noqa: E402


def setUpModule():
    """Re-execute under the tools interpreter when unicorn is missing here.

    sys.orig_argv is the command line that started this process (`python3 -m
    unittest ...`, or check.py's `python -c loader ...`), so the same run
    repeats under the other interpreter instead of sys.argv[0], which is the
    module path or a shell-looking string depending on how it was started.
    """
    if thumb_cost.Uc is None:
        py = thumb_cost.TOOLS_PYTHON
        if py.exists() and os.path.abspath(py) != os.path.abspath(sys.executable):
            argv = list(getattr(sys, "orig_argv", [])) or [sys.executable] + sys.argv
            argv[0] = str(py)
            os.execv(str(py), argv)
        raise unittest.SkipTest(
            f"unicorn/pyelftools unavailable: {thumb_cost._RUNTIME_ERROR}")
    if shutil.which("arm-none-eabi-gcc") is None:
        raise unittest.SkipTest("arm-none-eabi-gcc is not installed")


LOOP_DRIVER = """\
#include <stdint.h>

static volatile uint32_t sink;

__attribute__((noinline)) static uint32_t atom(uint32_t x) {
    return x + 1u;
}

#ifndef ITER
#define ITER 8
#endif

void bench(void) {
    uint32_t acc = 0;
    for (uint32_t i = 0; i < ITER; i++) {
        acc = atom(acc);
    }
    sink = acc;
}
"""

MUL64_DRIVER = """\
#include <stdint.h>

static volatile uint64_t sink;
static volatile uint32_t a = 0x12345678u;
static volatile uint32_t b = 0x9abcdef0u;

void bench(void) {
    uint64_t acc = 1;
    for (int i = 0; i < 8; i++) {
        acc = acc * a + b;
    }
    acc = acc * 0x123456789abcdef0ULL + (uint64_t)a * (uint64_t)b;
    sink = acc;
}
"""

# One hard tick of a from-rest start with harsh limits, the tick in the first
# 30 ms that moves the most phase, handed to the generator exactly once.
STEP_SEGMENTS_DRIVER = """\
#include <stdint.h>
#include "klipper_mmu/rate.h"
#include "klipper_mmu/step_segments.h"

#define TICK_US 1000

static km_rate_t best_start;
static km_rate_t best_end;
static km_step_segment_t segs[KM_STEP_SEGMENTS_MAX];
static volatile int segment_count;
static volatile int64_t driver_microstep;

static int64_t phase_steps(const km_rate_t *a, const km_rate_t *b) {
    return (b->phase_whole - a->phase_whole) * 4294967296LL
         + ((int64_t)b->phase_frac - (int64_t)a->phase_frac);
}

void bench(void) {
    km_rate_t r;
    km_rate_init(&r);
    km_rate_set_limits(&r, 200000, 5000000, 5000000, 100000000);
    km_rate_set_target_rate(&r, 200000);
    km_rate_set_target_phase(&r, 200000);

    int64_t best_steps = -1;
    for (int i = 0; i < 30; i++) {
        km_rate_t start = r;
        km_rate_advance(&r, TICK_US);
        int64_t steps = phase_steps(&start, &r);
        if (steps < 0) steps = -steps;
        if (steps > best_steps) {
            best_steps = steps;
            best_start = start;
            best_end = r;
        }
    }

    int64_t m = 0;
    uint32_t anchor = 0;
    uint8_t n = 0;
    int rc = km_step_segments_generate(&best_start, &best_end, TICK_US, 1, 1, false,
                                       &m, &anchor, segs, &n, KM_STEP_SEGMENTS_MAX);
    segment_count = (rc == 0) ? (int)n : -rc;
    driver_microstep = m;
}
"""


class ThumbCostTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls._tmp = tempfile.TemporaryDirectory(prefix="thumb_cost-test-")
        cls.dir = Path(cls._tmp.name)

    @classmethod
    def tearDownClass(cls):
        cls._tmp.cleanup()

    def driver(self, name, source):
        path = self.dir / name
        path.write_text(textwrap.dedent(source))
        return path

    def loop_count(self, iterations):
        driver = self.driver(f"loop-{iterations}.c", LOOP_DRIVER)
        res = thumb_cost.measure("m0plus", driver, defines=[f"ITER={iterations}"])
        self.assertEqual(res["stopped"], "bkpt")
        # atom() is `adds r0, #1; bx lr`: every one of the calls costs exactly
        # two instructions, so the per-function split is exact.
        self.assertEqual(res["functions"]["atom"], 2 * iterations)
        return res

    def test_loop_count_is_exact_and_repeatable(self):
        # Trip counts past the unroller so the loop shape does not change.
        once, twice, many = (self.loop_count(n) for n in (100, 101, 116))
        self.assertEqual(sum(many["functions"].values()), many["instructions"])
        # Two identical runs give the identical number, not a sampled one.
        self.assertEqual(self.loop_count(101)["instructions"], twice["instructions"])
        # The loop overhead is constant per iteration, so the run is linear in
        # the trip count with six instructions per iteration here.
        self.assertEqual(many["instructions"] - twice["instructions"],
                         15 * (twice["instructions"] - once["instructions"]))

    def test_libgcc_helper_calls_are_counted(self):
        driver = self.driver("mul64.c", MUL64_DRIVER)
        res = thumb_cost.measure("m0plus", driver)
        self.assertEqual(res["stopped"], "bkpt")
        # libgcc aliases __muldi3 to __aeabi_lmul at one address; one name wins.
        self.assertIn("__aeabi_lmul", res["functions"])
        self.assertNotIn("__muldi3", res["functions"])
        helper = res["helpers"]["__aeabi_lmul"]
        self.assertGreaterEqual(helper["calls"], 8)
        self.assertEqual(helper["instructions"], res["functions"]["__aeabi_lmul"])

    def test_step_segments_measurement(self):
        """Report the cost of one hard tick. The total is dominated by the 30
        km_rate_advance calls that find the tick to generate, so read
        km_step_segments_generate and emit_run out of the function list for the
        generator's own share; this asserts only that the run is complete."""
        driver = self.driver("segments.c", STEP_SEGMENTS_DRIVER)
        sources = [ROOT / "src/step_segments.c", ROOT / "src/rate.c"]
        for target in ("m0plus", "m7"):
            res = thumb_cost.measure(target, driver, sources, [ROOT / "include"])
            self.assertEqual(res["stopped"], "bkpt")
            print("\n" + thumb_cost.format_report(res, top=10))
            self.assertGreater(res["functions"].get("km_step_segments_generate", 0), 0)


if __name__ == "__main__":
    unittest.main()
