#!/usr/bin/env bash
# Build the vendored SpinCore SpinAPI driver for THIS machine's architecture and
# help spinapi.py find it
#
# The Linux driver is not a portable binary, so this script picks the
# tarball that matches `uname -m`, so the same command works on an x86_64 control
# node and on a Raspberry Pi:
#
#     cd third_party && ./build_spinapi.sh
#
# It is idempotent: re-running rebuilds in place. The extracted tree and build/
# output are gitignored; only the .tar.gz files are tracked.
set -eu

here="$(cd "$(dirname "$0")" && pwd)"
arch="$(uname -m)"

case "$arch" in
    x86_64|amd64)
        tarball="$(ls "$here"/SpinAPI_linux-*-x86_64.tar.gz 2>/dev/null | head -n1)"
        ;;
    armv7l|armv6l|arm)
        # SpinCore ships a 32-bit ARMv7 hard-float build. This is what a Raspberry
        # Pi reports under 32-bit Raspberry Pi OS.
        tarball="$(ls "$here"/SpinAPI_linux-*_ARMv7hf.tar.gz 2>/dev/null | head -n1)"
        ;;
    aarch64|arm64)
        cat >&2 <<'EOF'
ERROR: This is a 64-bit ARM (aarch64) userland, but SpinCore only ships a 32-bit
ARMv7hf SpinAPI build -- there is no aarch64 driver. A 32-bit .so cannot load into
a 64-bit Python process.

To drive a (USB) PulseBlaster from a Raspberry Pi, flash 32-bit Raspberry Pi OS so
`uname -m` reports armv7l and the process is 32-bit. On 64-bit OS the card is not
usable and the app will run in dry-run mode.
EOF
        exit 1
        ;;
    *)
        echo "ERROR: no vendored SpinAPI tarball for architecture '$arch'." >&2
        echo "Vendored tarballs:" >&2
        ls "$here"/SpinAPI_linux-*.tar.gz >&2 || true
        exit 1
        ;;
esac

if [ -z "${tarball:-}" ] || [ ! -f "$tarball" ]; then
    echo "ERROR: no vendored SpinAPI tarball found for '$arch' in $here." >&2
    exit 1
fi

echo "Architecture: $arch"
echo "Using tarball: $(basename "$tarball")"

srcdir="${tarball%.tar.gz}"
tar xzf "$tarball" -C "$here"

builddir="$srcdir/build"
mkdir -p "$builddir"
( cd "$builddir" && cmake .. && make )

lib="$(ls "$builddir"/src/libspinapi.so 2>/dev/null | head -n1)"
if [ -z "$lib" ]; then
    echo "Build finished but libspinapi.so was not found under $builddir/src." >&2
    exit 1
fi
libdir="$(cd "$(dirname "$lib")" && pwd)"

cat <<EOF

Built: $lib

Point the driver at it (bash/zsh):
    export LD_LIBRARY_PATH="$libdir:\$LD_LIBRARY_PATH"

To make it permanent, add that line to ~/.bashrc or the IOC's systemd unit
(Environment=LD_LIBRARY_PATH=$libdir). Then verify the board is seen:
    lsusb | grep -i spincore
    python -c "import spinapi; print(spinapi.pb_count_boards())"

Non-root USB access also needs SpinCore's udev rule + a 'spincore' group; see
https://spincore.com/support/spinapi/Linux_Help.shtml
EOF
