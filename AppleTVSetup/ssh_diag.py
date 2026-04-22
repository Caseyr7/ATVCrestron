import paramiko
import sys
import time

ssh = paramiko.SSHClient()
ssh.set_missing_host_key_policy(paramiko.AutoAddPolicy())

try:
    ssh.connect('10.100.51.24', port=22, username='datavox',
                password='@rgu$2436', timeout=10,
                allow_agent=False, look_for_keys=False)
except Exception as e:
    print(f"Connect error: {e}")
    sys.exit(1)

# Crestron console commands
commands = [
    'help pip3',
    'pip3 --version',
    'pip3 list',
    'help python',
    'python3 --version',
    'ver',
    'showhw',
    'showlicense',
    'progcomments:1',
    'listdirectory \\user\\program01',
    'listdirectory \\simpl\\app01',
    'listdirectory \\tmp',
    'help taskrun',
    'taskrun "pip3 --version"',
    'taskrun "pip3 install debugpy --target /user/appletv/deps"',
]

for cmd in commands:
    print(f"\n=== {cmd} ===")
    stdin, stdout, stderr = ssh.exec_command(cmd)
    time.sleep(2)
    out = stdout.read().decode()
    err = stderr.read().decode()
    if out: print(out.strip())
    if err: print(f"STDERR: {err.strip()}")

ssh.close()
