# Vendored third-party sources

## SpinAPI : SpinCore PulseBlaster driver (Linux source)

`src/spinapi.py` is SpinCore's `ctypes` wrapper; on Linux it loads `libspinapi.so`
found via `LD_LIBRARY_PATH`. That `.so` is **not** a portable binary, as one compiled
on a given arch/distro/glibc won't load elsewhere, so instead of committing a `.so`
we vendor SpinCore's **source tarballs** here and build them per-machine.

Vendoring the source (rather than just linking to spincore.com) means the driver can
always be rebuilt.

### Vendored tarballs

| Architecture (`uname -m`) | Tarball | Version | Typical host |
|---|---|---|---|
| `x86_64` | `SpinAPI_linux-20250210-x86_64.tar.gz` | 20250210 | Linux control node / workstation |
| `armv7l` | `SpinAPI_linux-20230223_ARMv7hf.tar.gz` | 20230223 | **Raspberry Pi (32-bit Raspberry Pi OS)** |

- **Upstream:** https://spincore.com/CD/Setup/linux/ (both files live here)
- Both tarballs have the same layout (CMake source + `examples/`), so the build steps
  below are identical on either arch.

> **Raspberry Pi note.** SpinCore's only ARM build is **32-bit ARMv7hf**, and there is
> **no aarch64/64-bit** driver. A 32-bit `.so` cannot load into a 64-bit Python, so a
> Pi that drives a card must run **32-bit Raspberry Pi OS** (then `uname -m` is
> `armv7l`). This also means **USB PulseBlasters only**, as the Pi has no PCI slot.
> On 64-bit Pi OS, the card is unusable and the app will just be in dry-run mode.

### Build it (produces `libspinapi.so`)

The easy way, auto-detects arch, picks the right tarball, builds, and prints the
`LD_LIBRARY_PATH` line to use:

```bash
cd third_party
./build_spinapi.sh
```

The manual equivalent (substitute the tarball for your arch):

```bash
cd third_party
tar xzf SpinAPI_linux-*_ARMv7hf.tar.gz         # or ...-x86_64.tar.gz
cd SpinAPI_linux-*_ARMv7hf
mkdir build && cd build
cmake .. && make                                # build/src/libspinapi.so
export LD_LIBRARY_PATH="$PWD/src:$LD_LIBRARY_PATH"
```

Non-root USB/device access on Linux also needs a one-time udev rule plus a `spincore`
group (root); see SpinCore's
[Linux instructions](https://spincore.com/support/spinapi/Linux_Help.shtml).

The extracted folder and `build/` output are gitignored; only the `.tar.gz` files and
`build_spinapi.sh` are tracked.

### Updating

Download a newer tarball for the relevant arch from SpinCore, drop it here (remove the
old one for that arch), and bump the version in the table above. Keep the filename
pattern (`...-x86_64.tar.gz` / `..._ARMv7hf.tar.gz`) so `build_spinapi.sh` still
matches it.
