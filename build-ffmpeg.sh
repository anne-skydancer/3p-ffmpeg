#!/usr/bin/env bash
# Configure, compile and install the decoder-only LGPL FFmpeg subset.
#
# Usage: build-ffmpeg.sh SOURCE_DIR BUILD_DIR INSTALL_DIR [extra configure args...]
#
# build-cmd.sh calls this script on every platform: directly on Linux and
# through MSYS2's bash on Windows (FFmpeg's configure and Makefiles need GNU
# make and a POSIX userland, which Git for Windows does not provide). Keeping
# the configure line here means both platforms build exactly the same subset.
# Paths may be given in Windows (C:/...) or POSIX form.

set -eux

if [[ $# -lt 3 ]]; then
    echo "usage: $0 SOURCE_DIR BUILD_DIR INSTALL_DIR [configure args...]" >&2
    exit 2
fi

to_posix() {
    if command -v cygpath >/dev/null 2>&1; then cygpath -u "$1"; else printf '%s\n' "$1"; fi
}

# Inherited from build-cmd.sh's xtrace setup; fd 4 does not exist here.
unset BASH_XTRACEFD

if [[ "${OSTYPE:-}" == msys || "${OSTYPE:-}" == cygwin ]]; then
    # MSYS2 tools first. FFmpeg's compat/windows/mslink resolves MSVC's
    # link.exe even though coreutils' /usr/bin/link precedes it.
    export PATH="/usr/bin:$PATH"
    # The CI workflow disables MSYS argument conversion for Git Bash; MSVC's
    # cl/rc need MSYS2 to convert /c/... paths, so re-enable it here.
    unset MSYS2_ARG_CONV_EXCL MSYS_NO_PATHCONV
fi

src="$(cd "$(to_posix "$1")" && pwd)"
build="$(to_posix "$2")"
prefix="$(to_posix "$3")"
shift 3

# Decoder-only, LGPL: --disable-everything turns off every component, then
# only the AAC (LC/HE-AAC v1/HE-AAC v2) and Opus decoders are enabled.
# opus_decoder_deps="swresample" in FFmpeg's configure, so libswresample is
# the one extra library that has to be built. Demuxing happens in the viewer.
# Threads are enabled explicitly: --disable-autodetect also disables the
# autodetected thread libraries, and without them ff_thread_once (one-time
# table initialisation) is not thread-safe.
configure_args=(
    --disable-everything
    --disable-autodetect
    --disable-programs
    --disable-doc
    --disable-network
    --disable-avformat
    --disable-avdevice
    --disable-avfilter
    --disable-swscale
    --enable-swresample
    --enable-decoder=aac
    --enable-decoder=opus
    --enable-shared
    --disable-static
    "$@"
)

rm -rf "$build" "$prefix"
mkdir -p "$build"
cd "$build"
"$src/configure" --prefix="$prefix" "${configure_args[@]}"

# Refuse to continue if the effective licence is not LGPL (e.g. a stray
# --enable-gpl/--enable-nonfree among the extra arguments).
grep -q '^#define CONFIG_GPL 0$' config.h
grep -q '^#define CONFIG_VERSION3 0$' config.h
grep -q '^#define CONFIG_NONFREE 0$' config.h
grep -q '^#define HAVE_THREADS 1$' config.h

jobs="$(nproc 2>/dev/null || echo 4)"
make -j"$jobs"
make install
