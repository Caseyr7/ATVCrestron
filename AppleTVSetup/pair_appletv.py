"""
pair_appletv.py  –  Interactive Apple TV pairing helper
Usage: python pair_appletv.py [apple_tv_ip]
Default Apple TV IP: 10.100.51.132

This script:
  1. SSH-connects to the Crestron processor
  2. Injects INIT + PAIR_START via test_cmd.txt
  3. Polls bridge_diag.log every 1 second
  4. The moment PAIR_WAITING_PIN appears, immediately prompts you locally for the PIN
  5. Injects PAIR_PIN:<digits> instantly — no round-trip through Claude
  6. Repeats for each protocol that needs a PIN (Companion, then AirPlay)
  7. Runs a final INIT to reconnect with saved credentials and reports success
"""

import paramiko
import sys
import time
import re

# ── Config ────────────────────────────────────────────────────────────────────
CRESTRON_HOST = '10.100.51.24'
CRESTRON_PORT = 22
CRESTRON_USER = 'datavox'
CRESTRON_PASS = '@rgu$2436'
LOG_FILE      = '/program01/bridge_diag.log'
CMD_FILE      = '/program01/test_cmd.txt'
ATV_IP        = sys.argv[1] if len(sys.argv) > 1 else '10.100.51.132'
PIN_TIMEOUT   = 60   # seconds to wait for user to type PIN
POLL_INTERVAL = 1.0  # seconds between log polls

# ── Helpers ───────────────────────────────────────────────────────────────────

def ssh_connect():
    ssh = paramiko.SSHClient()
    ssh.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    ssh.connect(CRESTRON_HOST, port=CRESTRON_PORT,
                username=CRESTRON_USER, password=CRESTRON_PASS,
                timeout=10, allow_agent=False, look_for_keys=False)
    return ssh


def read_log(sftp) -> list[str]:
    with sftp.open(LOG_FILE, 'r') as f:
        return f.readlines()


def inject_cmd(sftp, cmd: str):
    with sftp.open(CMD_FILE, 'w') as f:
        f.write(cmd + '\n')
    print(f"  [inject] {cmd}")


def tail_new(lines_before: list[str], lines_after: list[str]) -> list[str]:
    """Return only the lines added since the last read."""
    return lines_after[len(lines_before):]


def log_line_has(lines: list[str], token: str) -> bool:
    return any(token in l for l in lines)


def find_line_with(lines: list[str], token: str) -> str | None:
    for l in lines:
        if token in l:
            return l.rstrip()
    return None

# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    print(f"\n=== Apple TV Pairing Helper ===")
    print(f"  Processor : {CRESTRON_HOST}")
    print(f"  Apple TV  : {ATV_IP}")
    print()

    ssh  = ssh_connect()
    sftp = ssh.open_sftp()

    # ── Step 1: snapshot current log length ──────────────────────────────────
    log_base = read_log(sftp)
    print(f"[*] Log baseline: {len(log_base)} lines")

    # ── Step 2: inject INIT + PAIR_START ─────────────────────────────────────
    print(f"[*] Sending INIT:{ATV_IP} ...")
    inject_cmd(sftp, f'INIT:{ATV_IP}')
    time.sleep(2)

    print(f"[*] Sending PAIR_START:{ATV_IP} ...")
    log_base = read_log(sftp)   # re-baseline after INIT activity
    inject_cmd(sftp, f'PAIR_START:{ATV_IP}')

    # ── Step 3: wait for pairing to complete (may loop for multiple protocols) ─
    pair_round   = 0
    wait_start   = time.time()
    log_snapshot = log_base[:]
    error_count  = 0

    while True:
        time.sleep(POLL_INTERVAL)
        current_log = read_log(sftp)
        new_lines   = tail_new(log_snapshot, current_log)
        log_snapshot = current_log

        if not new_lines:
            # Check for overall timeout (3 minutes max for whole pairing flow)
            if time.time() - wait_start > 180:
                print("\n[!] Timed out waiting for pairing response. Exiting.")
                break
            continue

        for l in new_lines:
            print(f"  LOG: {l.rstrip()}")

        # ── PAIR_WAITING_PIN ─────────────────────────────────────────────────
        if log_line_has(new_lines, 'PAIR_WAITING_PIN'):
            pair_round += 1
            print(f"\n*** PIN IS NOW SHOWING ON YOUR APPLE TV (round {pair_round}) ***")
            print("    Type the 4-digit PIN displayed and press ENTER:")
            print("    (You have ~30 seconds)\n")

            pin_input = ''
            pin_start = time.time()

            # Use a simple blocking input — the script is running locally so
            # there's no round-trip delay.
            try:
                pin_input = input("    PIN > ").strip()
            except EOFError:
                print("[!] No input received (stdin closed).")
                break

            if not pin_input.isdigit() or len(pin_input) != 4:
                print(f"[!] Invalid PIN '{pin_input}'. Aborting.")
                break

            elapsed = time.time() - pin_start
            print(f"  [*] PIN received in {elapsed:.1f}s — injecting PAIR_PIN:{pin_input}")
            inject_cmd(sftp, f'PAIR_PIN:{pin_input}')
            wait_start = time.time()   # reset timeout for this round

        # ── PAIR_OK ──────────────────────────────────────────────────────────
        elif log_line_has(new_lines, 'PAIR_OK'):
            print(f"\n[OK] Protocol paired successfully!")
            # Bridge will automatically move to the next protocol (AirPlay).
            # Keep looping — another PAIR_WAITING_PIN may appear shortly.

        # ── PAIR_ERROR ───────────────────────────────────────────────────────
        elif log_line_has(new_lines, 'PAIR_ERROR'):
            error_line = find_line_with(new_lines, 'PAIR_ERROR')
            error_count += 1
            print(f"\n[!] Pairing error: {error_line}")
            if error_count >= 3:
                print("[!] Too many errors. Giving up.")
                break
            print("    Retrying PAIR_START in 3 seconds...")
            time.sleep(3)
            log_snapshot = read_log(sftp)   # re-baseline
            inject_cmd(sftp, f'PAIR_START:{ATV_IP}')
            wait_start = time.time()

        # ── PAIR_COMPLETE (all protocols done) ───────────────────────────────
        elif log_line_has(new_lines, 'PAIR_COMPLETE'):
            print(f"\n[OK] All protocols paired! Credentials saved.")
            break

        # ── CONNECTED (bridge reconnected with credentials) ──────────────────
        elif log_line_has(new_lines, 'SEND>CONNECTED:') and 'MRP' in ''.join(new_lines):
            print(f"\n[OK] Bridge reconnected with MRP/Companion — full control active!")
            break

    # ── Step 4: re-INIT to load fresh credentials ─────────────────────────────
    print(f"\n[*] Sending final INIT:{ATV_IP} to reconnect with saved credentials...")
    time.sleep(1)
    log_snapshot = read_log(sftp)
    inject_cmd(sftp, f'INIT:{ATV_IP}')

    # Wait up to 10 seconds for CONNECTED
    for _ in range(10):
        time.sleep(1)
        current_log  = read_log(sftp)
        new_lines    = tail_new(log_snapshot, current_log)
        log_snapshot = current_log
        for l in new_lines:
            print(f"  LOG: {l.rstrip()}")
        if log_line_has(new_lines, 'SEND>CONNECTED:'):
            # Check which interfaces we got
            iface_line = find_line_with(new_lines + current_log[-20:], 'Interfaces:')
            if iface_line:
                print(f"\n  Interfaces: {iface_line}")
                if 'MRP' in iface_line:
                    print("  [OK] remote_control(Protocol.MRP) active — full remote control works!")
                else:
                    print("  [!] MRP not in interfaces — check credentials file")
                if 'Companion' in iface_line:
                    print("  [OK] apps(Protocol.Companion) active — app launching works!")
                else:
                    print("  [!] Companion not in interfaces")
            break

    sftp.close()
    ssh.close()
    print("\nDone.")


if __name__ == '__main__':
    main()
