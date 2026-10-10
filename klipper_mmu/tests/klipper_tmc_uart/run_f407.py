#!/usr/bin/env python3
"""Native F407 ownership regression using installed pinned Klipper sources."""
# SPDX-License-Identifier: GPL-3.0-or-later
import argparse
import hashlib
import json
from pathlib import Path
import shutil
import subprocess
import tempfile

CASES = ("ordinary-first", "local-first", "unreserved-local", "reserved-local")
SOURCES = ("src/tmcuart.c", "src/tmcuart.h", "src/stm32/gpio.c",
           "src/stm32/gpioperiph.c", "src/stm32/internal.h",
           "src/stm32/gpio_local.h", "src/stm32/gpio.h")


def sha256(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def write_shims(root):
    shim = root / "shim"
    (shim / "board").mkdir(parents=True)
    (shim / "autoconf.h").write_text(
        "#define CONFIG_MACH_STM32F407 1\n#define CONFIG_MACH_STM32F4 1\n"
        "#define CONFIG_MACH_STM32F0 0\n#define CONFIG_MACH_STM32H7 0\n"
        "#define CONFIG_WANT_GPIO_LOCAL 1\n"
        "#define CONFIG_WANT_TMCUART_LOCAL 1\n"
        "#define CONFIG_TMCUART_LOCAL_MAX_CHANNELS 4\n")
    (shim / "stm32f4xx.h").write_text('''
#ifndef TEST_STM32F4XX_H
#define TEST_STM32F4XX_H
#include <stdint.h>
typedef struct {
    volatile uint32_t MODER, OTYPER, OSPEEDR, PUPDR, IDR, ODR, BSRR, LCKR;
    volatile uint32_t AFR[2];
} GPIO_TypeDef;
extern GPIO_TypeDef ports[9];
#define GPIOA (&ports[0])
#define GPIOB (&ports[1])
#define GPIOC (&ports[2])
#define GPIOD (&ports[3])
#define GPIOE (&ports[4])
#define GPIOF (&ports[5])
#define GPIOG (&ports[6])
#define GPIOH (&ports[7])
#define GPIOI (&ports[8])
#endif
'''.replace("\n+", "\n").lstrip())
    (shim / "board/gpio.h").write_text('#include "stm32/gpio.h"\n')
    (shim / "board/irq.h").write_text('''
#include <stdint.h>
typedef uint32_t irqstatus_t;
irqstatus_t irq_save(void);
void irq_restore(irqstatus_t);
void irq_disable(void);
void irq_enable(void);
'''.lstrip())
    (shim / "board/misc.h").write_text('''
#include <stdint.h>
uint32_t timer_read_time(void);
uint32_t timer_from_us(uint32_t);
'''.lstrip())
    (shim / "command.h").write_text('''
#include <stddef.h>
#include <stdint.h>
#define ARRAY_SIZE(a) (sizeof(a) / sizeof((a)[0]))
#define DIV_ROUND_CLOSEST(n,d) (((n) + (d) / 2) / (d))
#define container_of(p,t,m) ((t *)((char *)(p) - offsetof(t,m)))
#define DECL_ENUMERATION_RANGE(...)
#define DECL_COMMAND(...)
#define DECL_TASK(...)
#define DECL_SHUTDOWN(...)
#define sendf review_sendf
void review_sendf(const char *, ...);
void *command_decode_ptr(uint32_t);
_Noreturn void shutdown(const char *);
'''.lstrip())
    (shim / "basecmd.h").write_text('''
#include <stdint.h>
void *oid_alloc(uint8_t, void *, uint16_t);
void *oid_lookup(uint8_t, void *);
void *oid_next(uint8_t *, void *);
#define foreach_oid(pos, obj, type) \\
    for ((pos) = UINT8_MAX; ((obj) = oid_next(&(pos), (type))) != 0; )
'''.lstrip())
    (shim / "sched.h").write_text('''
#include <stdint.h>
struct timer {
    struct timer *next;
    uint_fast8_t (*func)(struct timer *);
    uint32_t waketime;
};
struct task_wake { uint8_t wake; };
enum { SF_DONE = 0, SF_RESCHEDULE = 1 };
#define DECL_TASK(...)
#define DECL_SHUTDOWN(...)
void sched_add_timer(struct timer *);
void sched_del_timer(struct timer *);
void sched_wake_task(struct task_wake *);
uint8_t sched_check_wake(struct task_wake *);
uint8_t sched_is_shutdown(void);
'''.lstrip())


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--package", type=Path,
                        default=Path(__file__).resolve().parents[2],
                        help="product package root (defaults to this checkout)")
    parser.add_argument("--upstream", type=Path, required=True,
                        help="installed pinned Klipper source checkout")
    parser.add_argument("--results", type=Path, required=True,
                        help="new directory for build log and results.json")
    args = parser.parse_args()
    package, upstream, results = (args.package.resolve(), args.upstream.resolve(),
                                  args.results.resolve())
    results.mkdir(parents=True, exist_ok=False)
    manifest = json.loads((package / "upstream/source-hashes.json").read_text())
    report = {"status": "failed", "hardware_accessed": False,
              "upstream": str(upstream), "sources": {}, "cases": {},
              "failed_cases": []}
    try:
        hashes = {}
        for rel in SOURCES:
            path = upstream / rel
            if not path.is_file():
                raise RuntimeError(f"installed source is missing: {rel}")
            actual = sha256(path)
            entry = manifest.get(rel, {})
            expected = entry.get("after", entry.get("before"))
            hashes[rel] = {"sha256": actual, "expected_sha256": expected}
            if expected and actual != expected:
                raise RuntimeError(f"installed pinned source hash mismatch: {rel}")
        report["sources"] = hashes
        with tempfile.TemporaryDirectory(prefix="km-f407-tmcuart-") as temp:
            work = Path(temp)
            write_shims(work)
            driver = work / "driver"
            stm32 = work / "src/stm32"
            driver.mkdir()
            stm32.mkdir(parents=True)
            for rel in SOURCES:
                source = upstream / rel
                dest = (driver / Path(rel).name if rel.startswith("src/tmcuart")
                        else stm32 / Path(rel).name)
                shutil.copy2(source, dest)
            binary = work / "f407-ownership"
            command = ["gcc", "-std=gnu11", "-Wall", "-Wextra", "-Werror",
                       "-Wno-sign-compare",
                       "-O1", "-g", "-fno-pie", "-no-pie",
                       "-fsanitize=address,undefined", "-fno-sanitize-recover=all"]
            for include in (work / "shim", work / "src", stm32, driver):
                command += ["-I", str(include)]
            command += [str(driver / "tmcuart.c"), str(stm32 / "gpio.c"),
                        str(stm32 / "gpioperiph.c"),
                        str(Path(__file__).resolve().with_name("f407_ownership.c")),
                        "-o", str(binary)]
            with (results / "build.log").open("w") as log:
                subprocess.run(command, stdout=log, stderr=subprocess.STDOUT,
                               timeout=30, check=True)
            for case in CASES:
                completed = subprocess.run(
                    [str(binary), case], capture_output=True, text=True, timeout=5,
                    env={"ASAN_OPTIONS": "detect_leaks=0:halt_on_error=1",
                         "UBSAN_OPTIONS": "halt_on_error=1"})
                report["cases"][case] = {
                    "returncode": completed.returncode,
                    "stdout": completed.stdout,
                    "stderr": completed.stderr,
                    "passed": completed.returncode == 0,
                }
            report["failed_cases"] = [name for name, case in report["cases"].items()
                                      if not case["passed"]]
            if report["failed_cases"]:
                raise RuntimeError("failed cases: " + ", ".join(report["failed_cases"]))
        report["status"] = "passed"
    except Exception as error:
        report["error"] = repr(error)
    finally:
        (results / "results.json").write_text(json.dumps(report, indent=2) + "\n")
        print(json.dumps({"status": report["status"], "cases": report["cases"],
                          "error": report.get("error")}, indent=2))
    if report["status"] != "passed":
        raise SystemExit(1)


if __name__ == "__main__":
    main()
