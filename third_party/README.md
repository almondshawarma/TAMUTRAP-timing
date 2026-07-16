# Vendored third-party sources

## SpinAPI — SpinCore PulseBlaster driver (Linux source)

**File:** `SpinAPI_linux-20250210-x86_64.tar.gz` *(add it here — see below)*

SpinCore's SpinAPI library is the driver `src/spinapi.py` binds to. On **Windows**
the compiled `spinapi64.dll` is vendored directly in `src/`. On **Linux** the driver
is **not** a portable binary — a `.so` compiled on one machine won't load on a
different distro/glibc — so instead of committing a `.so`, we vendor the **source
tarball** here and build it per-machine.

Vendoring the source (rather than just linking to spincore.com) means the Linux
driver can always be rebuilt even if SpinCore's site is down or bumps the version.
SpinAPI ships under a permissive zlib-style license that explicitly allows
redistribution, so keeping it in-repo is fine.

- **Upstream:** https://spincore.com/CD/Setup/linux/SpinAPI_linux-20250210-x86_64.tar.gz
- **Version:** 20250210 (x86_64)

### Build it (produces `libspinapi.so`)

```bash
cd third_party
tar xzf SpinAPI_linux-20250210-x86_64.tar.gz
cd SpinAPI_linux-20250210-x86_64
mkdir build && cd build
cmake .. && make
```

Then put the build's `src/` directory on `LD_LIBRARY_PATH` so `spinapi.py` can find
the library — see the main [README](../README.md#the-spincore-driver-windows-and-linux).
The extracted folder and `build/` output are gitignored; only the `.tar.gz` is tracked.

### Updating

Download a newer tarball from SpinCore, drop it here (remove the old one), and bump
the version in this file and the upstream link above.
