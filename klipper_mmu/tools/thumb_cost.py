#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
"""Instruction and estimated-cycle cost of portable firmware C on M0+/M7.

Compiles one ELF per target with arm-none-eabi-gcc and executes it in the
Unicorn CPU emulator, counting executed instructions with a code hook: the
total, a per-function split from the ELF symbol table, and the calls into
libgcc helpers (names starting with __aeabi or ending in di3). Cycles are
instructions times a stated CPI -- an estimate to compare alternatives, not a
cycle-accurate simulation of pipeline stalls, flash wait states or the M7's
dual issue.

The caller supplies a driver C file with `void bench(void)` that sets up its
inputs in static data and calls the code under test, plus the sources it needs:

    tools/thumb_cost.py --driver tests/foo/bench.c --src src/foo.c -I include \\
        --target m0plus --define COUNT=8

    m0plus: 192683 instructions, ~289025 cycles (CPI 1.5 assumption), ~4516.0 us at 64 MHz
      functions: compute_stopping_distance 33865, __aeabi_lmul 48339, ...
      helpers: __aeabi_lmul 21 calls/48339 instr

Use it to decide whether a periodic job fits its tick budget (for example the
B2 step segment generator at 1 ms on a G0B1), not to model a whole firmware.

Unicorn and pyelftools live in the tools interpreter below; when they are
missing this script re-executes itself with it. Point KLIPPER_MMU_TOOLS_PYTHON
at another interpreter to override.
"""
from __future__ import annotations

import argparse
import bisect
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile

try:
    from unicorn import UC_ARCH_ARM, UC_HOOK_CODE, UC_MODE_THUMB, Uc, UcError
    from unicorn.arm_const import UC_ARM_REG_MSP, UC_ARM_REG_SP
    import unicorn.arm_const as _arm_const
    from elftools.elf.elffile import ELFFile
except ImportError as exc:  # pragma: no cover - depends on the interpreter
    Uc = None
    _RUNTIME_ERROR = exc
else:
    _RUNTIME_ERROR = None

FLASH_BASE = 0x08000000
RAM_BASE = 0x20000000
RAM_SIZE = 128 * 1024
DEFAULT_LIMIT = 20_000_000
PAGE = 0x1000

# cpi is the average cycles per instruction assumed for the estimate, and mhz
# the clock the default microsecond figure is quoted at.
TARGETS = {
    "m0plus": {"cpu": "cortex-m0plus", "model": "UC_CPU_ARM_CORTEX_M0", "cpi": 1.5, "mhz": 64.0},
    "m7": {"cpu": "cortex-m7", "model": "UC_CPU_ARM_CORTEX_M7", "cpi": 1.0, "mhz": 520.0},
}

TOOLS_PYTHON = Path(os.environ.get(
    "KLIPPER_MMU_TOOLS_PYTHON",
    str(Path.home() / ".local/share/klipper-mmu-tools/bin/python")))

STARTUP_C = """\
extern void bench(void);
void _start(void) {
    bench();
    __asm__ volatile("bkpt #0");
    for (;;) {
    }
}
"""

LINKER_SCRIPT = """\
ENTRY(_start)
MEMORY {{
  FLASH (rx) : ORIGIN = 0x{flash:08x}, LENGTH = 1024K
  RAM (rwx)  : ORIGIN = 0x{ram:08x}, LENGTH = {ram_k}K
}}
SECTIONS {{
  .text : {{ *(.text*) *(.rodata*) }} > FLASH
  .data : {{ *(.data*) }} > RAM
  .bss (NOLOAD) : {{ *(.bss*) *(COMMON) }} > RAM
  __stack_top = ORIGIN(RAM) + LENGTH(RAM);
}}
"""


class BuildError(RuntimeError):
    """arm-none-eabi-gcc refused to produce the benchmark ELF."""


def _align_up(value):
    return (value + PAGE - 1) & ~(PAGE - 1)


def _is_helper(name):
    return name.startswith("__aeabi") or name.endswith("di3")


def _prefer(name):
    # libgcc aliases a callable (__aeabi_lmul) to an implementation (__muldi3)
    # at one address; keep the name the compiler actually emits.
    return (0 if name.startswith("__aeabi") else 1, name)


def _function_ranges(raw):
    best = {}
    for start, end, name in raw:
        cur = best.get(start)
        if cur is None or _prefer(name) < _prefer(cur[2]):
            best[start] = (start, end, name)
    return sorted(best.values())


def build(target, driver, sources, includes, defines, work, gcc="arm-none-eabi-gcc", timeout=300):
    """Compile the driver and sources into one freestanding ELF for target."""
    cfg = TARGETS[target]
    startup = work / f"startup-{target}.c"
    linker = work / f"link-{target}.ld"
    elf = work / f"bench-{target}.elf"
    startup.write_text(STARTUP_C)
    linker.write_text(LINKER_SCRIPT.format(flash=FLASH_BASE, ram=RAM_BASE, ram_k=RAM_SIZE // 1024))
    argv = [
        gcc, f"-mcpu={cfg['cpu']}", "-mthumb", "-O2", "-ffreestanding", "-nostartfiles",
        "-mfloat-abi=soft", "-ffunction-sections", "-fdata-sections", "-Wl,--gc-sections",
        f"-Wl,-T,{linker}", "-o", str(elf),
    ]
    argv += [f"-I{inc}" for inc in includes]
    argv += [f"-D{d}" for d in defines]
    argv += [str(startup), str(driver)] + [str(s) for s in sources]
    argv += ["-lgcc"]
    proc = subprocess.run(argv, capture_output=True, text=True, timeout=timeout)
    if proc.returncode != 0:
        raise BuildError(f"arm-none-eabi-gcc failed for {target}:\n{proc.stderr.strip()}")
    return elf


def _symbols(elf):
    funcs, stack_top = [], None
    symtab = elf.get_section_by_name(".symtab") or elf.get_section_by_name(".dynsym")
    for sym in symtab.iter_symbols():
        if sym.name == "__stack_top":
            stack_top = sym["st_value"]
        if not sym.name or sym["st_info"]["type"] != "STT_FUNC" or sym["st_size"] <= 0:
            continue
        if sym["st_shndx"] == "SHN_UNDEF":
            continue
        start = sym["st_value"] & ~1
        funcs.append((start, start + sym["st_size"], sym.name))
    return funcs, stack_top


def _find_bkpt(uc, start, end):
    blob = bytes(uc.mem_read(start, end - start))
    for off in range(0, len(blob) - 1, 2):
        if blob[off] == 0x00 and blob[off + 1] == 0xBE:  # bkpt #0, little endian
            return start + off
    return None


def emulate(elf_path, target, limit=DEFAULT_LIMIT, mhz=None):
    """Run the ELF from its entry to the stub's bkpt, counting instructions."""
    cfg = TARGETS[target]
    with open(elf_path, "rb") as fh:
        elf = ELFFile(fh)
        entry = elf.header.e_entry
        segments = [(seg["p_vaddr"], seg["p_filesz"], seg["p_memsz"], seg.data())
                    for seg in elf.iter_segments() if seg["p_type"] == "PT_LOAD"]
        raw_funcs, stack_top = _symbols(elf)

    funcs = _function_ranges(raw_funcs)
    starts = [f[0] for f in funcs]

    def lookup(addr):
        i = bisect.bisect_right(starts, addr) - 1
        for j in range(i, max(-1, i - 8), -1):
            start, end, name = funcs[j]
            if start <= addr < end:
                return name
        return "<unknown>"

    flash_end = max((v + m for v, _, m, _ in segments if v < RAM_BASE), default=FLASH_BASE + PAGE)
    ram_end = max([RAM_BASE + RAM_SIZE] +
                  [v + m for v, _, m, _ in segments if v >= RAM_BASE])
    uc = Uc(UC_ARCH_ARM, UC_MODE_THUMB)
    model = getattr(_arm_const, cfg["model"], None)
    if model is not None:
        try:
            uc.ctl_set_cpu_model(model)
        except UcError:  # pragma: no cover - older Unicorn without the model
            pass
    uc.mem_map(FLASH_BASE, _align_up(flash_end - FLASH_BASE), 7)
    uc.mem_map(RAM_BASE, _align_up(ram_end - RAM_BASE), 7)
    for vaddr, filesz, _, data in segments:
        if filesz:
            uc.mem_write(vaddr, data)

    start_sym = next((f for f in funcs if f[2] == "_start"), None)
    if start_sym is None:
        raise BuildError("the ELF has no _start symbol; --driver likely defines main()")
    bkpt = _find_bkpt(uc, start_sym[0], start_sym[1])
    if bkpt is None:
        raise BuildError("no bkpt in _start; the startup stub was not linked as built")
    uc.reg_write(UC_ARM_REG_SP, stack_top or (RAM_BASE + RAM_SIZE))
    uc.reg_write(UC_ARM_REG_MSP, stack_top or (RAM_BASE + RAM_SIZE))

    total = 0
    func_count = {}
    helper_instr = {}
    helper_calls = {}
    previous = [""]
    stopped = None

    def hook(_uc, addr, _size, _user):
        nonlocal total, stopped
        if addr == bkpt:
            stopped = "bkpt"
            _uc.emu_stop()
            return
        name = lookup(addr)
        total += 1
        func_count[name] = func_count.get(name, 0) + 1
        if _is_helper(name):
            helper_instr[name] = helper_instr.get(name, 0) + 1
            if previous[0] != name:
                helper_calls[name] = helper_calls.get(name, 0) + 1
        previous[0] = name

    uc.hook_add(UC_HOOK_CODE, hook)
    try:
        uc.emu_start(entry, 0, count=limit)
    except UcError as exc:
        stopped = f"fault: {exc}"
    if stopped is None:
        stopped = "limit"

    cycles = total * cfg["cpi"]
    mhz = cfg["mhz"] if mhz is None else mhz
    return {
        "target": target,
        "instructions": total,
        "cpi": cfg["cpi"],
        "cycles": cycles,
        "mhz": mhz,
        "microseconds": cycles / mhz,
        "stopped": stopped,
        "functions": func_count,
        "helpers": {name: {"calls": helper_calls.get(name, 0), "instructions": helper_instr[name]}
                    for name in helper_instr},
    }


def measure(target, driver, sources=(), includes=(), defines=(), limit=DEFAULT_LIMIT,
            mhz=None, gcc="arm-none-eabi-gcc", build_dir=None, timeout=300):
    """Build and emulate one target; returns the metrics dict emulate() builds."""
    if Uc is None:
        raise RuntimeError(str(_RUNTIME_ERROR))
    if build_dir is None:
        work = Path(tempfile.mkdtemp(prefix="thumb_cost-"))
        cleanup = True
    else:
        work = Path(build_dir)
        work.mkdir(parents=True, exist_ok=True)
        cleanup = False
    try:
        elf = build(target, driver, sources, includes, defines, work, gcc, timeout)
        return emulate(str(elf), target, limit, mhz)
    finally:
        if cleanup:
            shutil.rmtree(work, ignore_errors=True)


def format_report(res, top=6):
    mhz = f"{res['mhz']:g}"
    lines = [
        f"{res['target']}: {res['instructions']} instructions, "
        f"~{res['cycles']:.0f} cycles (CPI {res['cpi']} assumption), "
        f"~{res['microseconds']:.1f} us at {mhz} MHz"
    ]
    if res["stopped"] != "bkpt":
        lines.append(f"  (did not reach the breakpoint: {res['stopped']})")
    top_functions = sorted(res["functions"].items(), key=lambda kv: (-kv[1], kv[0]))[:top]
    lines.append("  functions: " + ", ".join(f"{n} {c}" for n, c in top_functions))
    helpers = sorted(res["helpers"].items(), key=lambda kv: (-kv[1]["calls"], kv[0]))
    if helpers:
        lines.append("  helpers: " + ", ".join(
            f"{n} {h['calls']} calls/{h['instructions']} instr" for n, h in helpers))
    else:
        lines.append("  helpers: none")
    return "\n".join(lines)


def _ensure_runtime():
    """Re-execute with the tools interpreter when unicorn/pyelftools are missing."""
    if Uc is not None:
        return
    # Compare the literal paths, not their realpaths: the tools interpreter is
    # a symlink to the system one, and only starting it through the venv path
    # puts the venv's site-packages on sys.path.
    if TOOLS_PYTHON.exists() and os.path.abspath(TOOLS_PYTHON) != os.path.abspath(sys.executable):
        os.execv(str(TOOLS_PYTHON), [str(TOOLS_PYTHON)] + sys.argv)
    sys.stderr.write(
        f"thumb_cost: {_RUNTIME_ERROR}; set KLIPPER_MMU_TOOLS_PYTHON to a Python "
        "with unicorn and pyelftools installed\n")
    raise SystemExit(2)


def main(argv=None):
    _ensure_runtime()
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--driver", required=True, help="C file defining void bench(void)")
    parser.add_argument("--src", action="append", default=[], metavar="FILE.c",
                        help="additional C source the driver needs (repeatable)")
    parser.add_argument("-I", "--include", action="append", default=[], metavar="DIR",
                        help="include directory (repeatable)")
    parser.add_argument("--target", choices=["m0plus", "m7", "both"], default="both")
    parser.add_argument("--define", action="append", default=[], metavar="NAME=VAL",
                        help="preprocessor definition (repeatable)")
    parser.add_argument("--limit", type=int, default=DEFAULT_LIMIT,
                        help=f"instruction limit per run (default {DEFAULT_LIMIT})")
    parser.add_argument("--mhz", type=float, default=None,
                        help="clock for the microsecond figure (default 64 m0plus, 520 m7)")
    parser.add_argument("--top", type=int, default=6, help="functions to list (default 6)")
    parser.add_argument("--json", action="store_true", help="emit the metrics as JSON")
    parser.add_argument("--build-dir", default=None,
                        help="keep the generated ELFs here (default a temporary directory)")
    parser.add_argument("--gcc", default="arm-none-eabi-gcc", help="compiler to use")
    args = parser.parse_args(argv)

    targets = list(TARGETS) if args.target == "both" else [args.target]
    results = []
    for target in targets:
        results.append(measure(target, args.driver, args.src, args.include, args.define,
                               args.limit, args.mhz, args.gcc, args.build_dir))

    if args.json:
        print(json.dumps(results, indent=2, sort_keys=True))
    else:
        for res in results:
            print(format_report(res, args.top))
        print("cycles = instructions x CPI: an estimate, not a cycle-accurate simulation")
    return 0 if all(res["stopped"] == "bkpt" for res in results) else 1


if __name__ == "__main__":
    sys.exit(main())
