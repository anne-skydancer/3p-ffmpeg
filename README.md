# 3p-ffmpeg

Autobuild packaging of a decoder-only, LGPL build of [FFmpeg](https://ffmpeg.org/) for
Vulkanstorm. The viewer's Rust audio engine decodes HE-AAC (v1 and v2) and Opus parcel
streams in-process through these libraries; MP3, AAC-LC, Vorbis and FLAC are decoded in
pure Rust. Demuxing (ADTS, Ogg) is done by the viewer, so no FFmpeg demuxer or parser is
built.

The viewer loads the shared libraries at runtime (it does not link them) and calls the
libavcodec decode API.

## Source

`ffmpeg/` is a git submodule of `https://github.com/FFmpeg/FFmpeg.git` pinned to release tag
**n9.0.2** (commit `946fcce07b6dcd0331c8cc609192aeff5e1924f8`, 2026-09-18). It was the newest
stable release when this package was created. It builds unmodified with MSVC 2022 on Windows
and with GCC 11 (Ubuntu 22.04) through GCC 15 on Linux. No patches are applied.

The ABI the viewer binds to is libavcodec 63, libavutil 61 and libswresample 7.
`verify_package.py` lists the struct offsets the viewer relies on. Update them, and the
viewer bindings, before moving to a release with a new major version.

## Licence

FFmpeg is LGPL 2.1 or later as configured here: there is no `--enable-gpl`,
`--enable-version3` or `--enable-nonfree`, and no external library. The libraries confirm
this at runtime (`avcodec_license()` returns `LGPL version 2.1 or later`).
`LICENSES/ffmpeg.txt` is FFmpeg's `COPYING.LGPLv2.1` followed by its `LICENSE.md`. The
libraries are shared (not static), so LGPL relinking terms are met by replacing the DLLs or
`.so` files. Corresponding source is the pinned submodule commit (recorded in
`SOURCE_REVISION.txt`) plus `build-cmd.sh` and `build-ffmpeg.sh`.

## Configuration

`build-ffmpeg.sh` runs this configure line on every platform:

```
--disable-everything --disable-autodetect --disable-programs --disable-doc
--disable-network --disable-avformat --disable-avdevice --disable-avfilter
--disable-swscale --enable-swresample --enable-decoder=aac --enable-decoder=opus
--enable-shared --disable-static
```

Platform additions from `build-cmd.sh`:

- **Windows x64:** `--toolchain=msvc --target-os=win64 --arch=x86_64 --enable-w32threads --extra-cflags=-MD`
- **Linux x86-64:** `--arch=x86_64 --enable-pthreads --extra-ldsoflags=-Wl,@build/rpath-origin.opt`
  The response file holds `-rpath=$ORIGIN`.

The configure line was chosen as follows:

- `opus_decoder_deps="swresample"` in FFmpeg's configure, so libswresample is the only
  library besides libavcodec and libavutil. The `aac` decoder covers AAC-LC, HE-AAC (SBR)
  and HE-AAC v2 (PS), with explicit or implicit signalling.
- `--disable-autodetect` also disables the autodetected thread libraries. Threads are
  therefore enabled explicitly; without them FFmpeg's one-time table initialisation is not
  thread-safe. The build stops unless `config.h` has `HAVE_THREADS 1`, `CONFIG_GPL 0`,
  `CONFIG_VERSION3 0` and `CONFIG_NONFREE 0`.
- FFmpeg's x86 assembly is built with NASM. It is selected by runtime CPU detection, so the
  baseline stays plain x86-64 and AVX2 or AVX-512 paths are used only where the CPU has them.
- Windows uses the MSVC toolchain and the dynamic CRT (`/MD`). The DLLs import only
  `KERNEL32`, `bcrypt` (avutil), `VCRUNTIME140` and the UCRT `api-ms-win-crt-*` sets, the
  same runtime as the viewer. MSYS2 provides only `make`, `nasm` and the shell that runs
  `configure`.
- On Linux each library has `RUNPATH $ORIGIN`, so it finds its siblings wherever the viewer
  stages them. The libraries depend only on `libc` and `libm`.

The exact string the libraries were configured with is shipped as `CONFIGURATION.txt`. It is
identical to `avcodec_configuration()`.

## Package contents

| | Windows (`windows64`) | Linux (`linux64`) |
|---|---|---|
| libavcodec | `bin/release/avcodec-63.dll`, `lib/release/avcodec.lib` | `lib/release/libavcodec.so.63.1.102`, symlinks `libavcodec.so.63`, `libavcodec.so` |
| libavutil | `bin/release/avutil-61.dll`, `lib/release/avutil.lib` | `lib/release/libavutil.so.61.1.102`, symlinks `libavutil.so.61`, `libavutil.so` |
| libswresample | `bin/release/swresample-7.dll`, `lib/release/swresample.lib` | `lib/release/libswresample.so.7.1.102`, symlinks `libswresample.so.7`, `libswresample.so` |

Every archive also contains:

- `include/libavcodec`, `include/libavutil` and `include/libswresample`;
- `LICENSES/ffmpeg.txt`;
- `SOURCE_REVISION.txt`, the FFmpeg commit;
- `CONFIGURATION.txt`.

The SONAMEs are `libavcodec.so.63`, `libavutil.so.61` and `libswresample.so.7`.

Loading notes for the viewer:

- **Windows:** load `avcodec-63.dll` by its fully qualified path, using backslashes, with
  `LOAD_LIBRARY_SEARCH_DLL_LOAD_DIR | LOAD_LIBRARY_SEARCH_DEFAULT_DIRS`. Its dependencies
  then resolve from the same directory. Alternatively, load `avutil-61.dll` and
  `swresample-7.dll` first.
- **Linux:** `dlopen("…/libavcodec.so.63")` is enough, because RUNPATH `$ORIGIN` finds the
  other two libraries.

## Building

Requirements:

- Python 3, autobuild 3.9.3 and a checkout of
  [secondlife/build-variables](https://github.com/secondlife/build-variables), with
  `AUTOBUILD_VARIABLES_FILE` pointing at its `variables` file.
- **Windows:** Visual Studio 2022 C++ tools (`AUTOBUILD_VSVER=170`) and MSYS2 with
  `make nasm diffutils`. MSYS2 is expected at `C:\msys64`; set `MSYS2_ROOT` for another
  location. `build-cmd.sh` runs under Git Bash, as autobuild starts it.
- **Linux:** GCC, make and nasm. Build on Ubuntu 22.04 (glibc 2.35) or in an equivalent
  chroot. A newer build host raises the glibc symbol versions the libraries require; for
  example, Ubuntu 26.04 produces `atan2f@GLIBC_2.43`. `verify_package.py` rejects anything
  newer than GLIBC 2.35 by default.

Clone with submodules on Windows without CRLF conversion, because FFmpeg's `configure`
must keep LF line endings:

```sh
git -c core.autocrlf=false clone --recurse-submodules https://github.com/anne-skydancer/3p-ffmpeg.git
```

Then build, package and verify:

```sh
autobuild build -A 64 -c Release --no-configure --id BUILD_ID
autobuild package -A 64 --archive-format tzst \
    --archive-name "ffmpeg-$(cat stage/VERSION.txt)-PLATFORM-BUILD_ID"
python verify_package.py ffmpeg-*-PLATFORM-BUILD_ID.tar.zst
```

`PLATFORM` is `windows64` or `linux64`. `VERSION.txt` is `<FFmpeg release>.<build id>`,
for example `9.0.2.BUILD_ID`.

## Verification

`verify_package.py` (Python 3.14, or the `zstandard` module) checks an archive by
extracting it and loading the shipped libraries. It checks:

- **Metadata:** name, licence, version and build id, and that the archive is not marked
  dirty.
- **File list:** the expected files, the Linux SONAME symlinks, and that no other FFmpeg
  library is packaged.
- **Licence text.**
- **Linux glibc ceiling.**
- **Runtime identity:** the library majors match the headers, the reported licence and
  configuration are LGPL, and the only codecs are the `aac` and `opus` decoders.
- **Exports:** every entry point the viewer uses is exported.
- **Decoding:** it decodes the fixtures in `tests/media` with the struct offsets the viewer
  uses:
  - **`he-aac-v1.aac`:** HE-AAC v1 in ADTS, 48 kbit/s. The 22.05 kHz stereo core must
    decode to 44.1 kHz.
  - **`he-aac-v2.aac`:** HE-AAC v2 in ADTS, 32 kbit/s. The 22.05 kHz mono core must decode
    to 44.1 kHz stereo through parametric stereo.
  - **`opus-stereo.opus`:** Ogg Opus at 48 kHz, stereo, 64 kbit/s, decoded with `OpusHead`
    as extradata.

The fixtures are one second long, with a 440 Hz tone on the left and 9 kHz on the right, both
at -6 dBFS. The right channel lies above the SBR crossover. They were generated with sox
14.7, libfdk-aac v3.0.0 `aac-enc` (`-t 5 -r 48000` and `-t 29 -r 32000`), and opus-tools
0.2 with libopus 1.6.1 (`opusenc --bitrate 64 --comp 10`).

The GitHub workflow builds both platforms on `windows-2022` and `ubuntu-22.04` with
autobuild 3.9.3, then verifies and uploads the `.tar.zst` archives. Those archives become
the GitHub release assets.
