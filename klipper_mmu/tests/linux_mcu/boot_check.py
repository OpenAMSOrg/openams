#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
import os
import shutil
import subprocess
import sys
import tempfile
import time
import socket
import json

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from build_root import KLIPPY_PYTHON, skip_unless_available  # noqa: E402

def main():
    skip_unless_available('tests/linux_mcu/boot_check.py')
    mcu_elf = 'build/linux-mcu/klipper/out-sim/klipper.elf'
    pty_path = os.path.abspath('build/linux-mcu/mcu.pty')
    
    if os.path.exists(pty_path):
        os.unlink(pty_path)
    
    mcu_proc = subprocess.Popen([os.path.abspath(mcu_elf), '-I', pty_path], stdout=open('build/linux-mcu/mcu.log', 'w'), stderr=subprocess.STDOUT)
    
    try:
        for _ in range(50):
            if os.path.exists(pty_path): break
            time.sleep(0.1)
            
        subprocess.run(['python3', 'tools/install.py', 'install-host', 'build/linux-mcu/klipper'], check=True, timeout=60)
        
        cfg_path = os.path.abspath('build/linux-mcu/printer.cfg')
        with open(cfg_path, 'w') as f:
            f.write(f"""
[mcu]
serial: {pty_path}
[printer]
kinematics: none
max_velocity: 1
max_accel: 1
[klipper_mmu box]
mcu: mcu
diff_probe: True
""")
            
        # AF_UNIX socket paths are capped at ~108 bytes by the kernel, well
        # under what a worktree path under build/linux-mcu/ can run to,
        # so the API socket itself lives in a short-named tmpdir. Logs,
        # the pty and printer.cfg still land in build/linux-mcu/ per the
        # "keep it under build/" rule; this is transient IPC, not output.
        api_socket_dir = tempfile.mkdtemp(prefix='kmmu-sock-')
        api_socket = os.path.join(api_socket_dir, 'klippy.sock')

        klippy_log = os.path.abspath('build/linux-mcu/klippy.log')
        klippy_proc = subprocess.Popen(
            [str(KLIPPY_PYTHON),
             'build/linux-mcu/klipper/klippy/klippy.py',
             cfg_path, '-l', klippy_log, '-a', api_socket],
            stdout=open('build/linux-mcu/klippy_out.log', 'w'), stderr=subprocess.STDOUT
        )
        
        try:
            for _ in range(100):
                if os.path.exists(api_socket): break
                time.sleep(0.1)
                
            s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            s.connect(api_socket)
            s.settimeout(5.0)
            
            req_id = 1
            def req(method, params=None):
                nonlocal req_id
                s.sendall((json.dumps({"jsonrpc":"2.0","method":method,"params":params or {},"id":req_id}) + '\x03').encode())
                req_id += 1
                resp = b""
                while b'\x03' not in resp:
                    resp += s.recv(4096)
                return json.loads(resp.split(b'\x03')[0].decode())
                
            for _ in range(100):
                info = req("info")
                if info.get('result', {}).get('state') == 'ready':
                    print("Klippy is ready.")
                    break
                time.sleep(0.5)
            else:
                raise Exception("Klippy never became ready")
                
            gcode_resp = req("gcode/script", {"script": "KLIPPER_MMU_QUERY MMU=box"})
            print("G-Code response:", gcode_resp)
            if 'error' in gcode_resp:
                raise Exception("GCode failed: " + str(gcode_resp))
                
            print("Boot check passed.")
        finally:
            klippy_proc.terminate()
            klippy_proc.wait(timeout=10)
            shutil.rmtree(api_socket_dir, ignore_errors=True)
    finally:
        mcu_proc.terminate()
        mcu_proc.wait(timeout=10)

if __name__ == '__main__':
    main()
