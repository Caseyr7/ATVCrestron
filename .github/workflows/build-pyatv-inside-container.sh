#!/bin/bash
# Runs INSIDE the python:3.8-bullseye arm/v7 Docker container under QEMU emulation.
# Produces /deps/* ready to be uploaded as a GitHub Actions artifact.
set -euxo pipefail

# Build toolchain - needed for aiohttp + zeroconf C extensions.
# Cryptography 46.x has a pre-built cp38-abi3 armv7l wheel so no Rust needed.
export DEBIAN_FRONTEND=noninteractive
apt-get update
apt-get install -y --no-install-recommends \
    gcc g++ make \
    libffi-dev libssl-dev \
    pkg-config
rm -rf /var/lib/apt/lists/*

# Upgrade pip to a recent version that handles modern wheel tags.
pip install --no-cache-dir --upgrade 'pip<25' 'setuptools<70' 'wheel<1'

# Install everything into /deps as a flat import root.
# --no-compile skips .pyc generation (the installer will regenerate on the target).
pip install --no-cache-dir --no-compile \
    --target /deps \
    -r /req.txt

# Rename ABI suffix: Debian bullseye arm/v7 produces gnueabihf.so
# but Crestron Python 3.8.13 expects gnueabi.so (no hf). The binary
# is identical - only the filename tag differs.
cd /deps
find . -name '*.cpython-38-arm-linux-gnueabihf.so' -print0 | while IFS= read -r -d '' f; do
    new="${f%gnueabihf.so}gnueabi.so"
    mv -v "$f" "$new"
done

# Trim metadata and tests - each MB matters over SFTP to a CP4.
find . -type d -name '__pycache__' -exec rm -rf {} + 2>/dev/null || true
find . -type d -name '*.dist-info' -exec rm -rf {} + 2>/dev/null || true
find . -type d -name '*.egg-info' -exec rm -rf {} + 2>/dev/null || true
find . -type d -name 'tests' -exec rm -rf {} + 2>/dev/null || true
find . -type d -name 'test' -exec rm -rf {} + 2>/dev/null || true
find . -name '*.pyc' -delete 2>/dev/null || true
find . -name '*.pyo' -delete 2>/dev/null || true

# Remove miniaudio if pip pulled it in - the Crestron installer stubs it.
rm -rf miniaudio miniaudio-*.dist-info 2>/dev/null || true

# Sanity: verify the critical .so files have the right ABI name now.
echo '=== .so files in /deps (should all be gnueabi, NOT gnueabihf) ==='
find . -name '*.so' | sort
echo '=== total size ==='
du -sh /deps

# Files are root-owned inside the container; make them world-readable
# so the GitHub runner user can upload them as an artifact.
chmod -R u+rwX,go+rX /deps
