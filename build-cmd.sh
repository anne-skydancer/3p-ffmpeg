#!/usr/bin/env bash

cd "$(dirname "$0")"

# turn on verbose debugging output for parabuild logs.
exec 4>&1; export BASH_XTRACEFD=4; set -x
# make errors fatal
set -e
# complain about unset env variables
set -u

FFMPEG_SOURCE_DIR="ffmpeg"
FFMPEG_VERSION="$(tr -d '\r\n' < "$FFMPEG_SOURCE_DIR/RELEASE")"

if [ -z "$AUTOBUILD" ] ; then
    exit 1
fi

if [[ "$OSTYPE" == "cygwin" || "$OSTYPE" == "msys" ]] ; then
    autobuild="$(cygpath -u "$AUTOBUILD")"
else
    autobuild="$AUTOBUILD"
fi

top="$(pwd)"
stage="$top/stage"

# load autobuild provided shell functions and variables
source_environment_tempfile="$stage/source_environment.sh"
"$autobuild" source_environment > "$source_environment_tempfile"
. "$source_environment_tempfile"

build=${AUTOBUILD_BUILD_ID:=0}
echo "${FFMPEG_VERSION}.${build}" > "${stage}/VERSION.txt"

if [[ "$AUTOBUILD_ADDRSIZE" != 64 ]]; then
    echo "3p-ffmpeg packages x86-64 only" >&2
    exit 1
fi

# Out-of-tree build keeps the source submodule clean.
build_dir="$top/build/ffmpeg"
install_dir="$top/build/install"

case "$AUTOBUILD_PLATFORM" in
    windows*)
        # MSVC toolchain, so the DLLs use the same dynamic CRT (/MD) as the
        # viewer and need no MinGW runtime. configure/make run under MSYS2
        # (preinstalled on GitHub's windows-2022 image at C:\msys64; packages
        # make, nasm and diffutils are required). MSYS2_ROOT overrides it.
        load_vsvars
        msys2_root="$(cygpath -u "${MSYS2_ROOT:-C:/msys64}")"
        # Start MSYS2's bash through a native process (Python, which
        # autobuild already requires). Spawned directly from Git for
        # Windows' bash, a second MSYS runtime receives no environment, so
        # the MSVC INCLUDE/LIB/PATH set by load_vsvars would be lost.
        MSYSTEM=MSYS CHERE_INVOKING=1 \
            "${PYTHON:-python}" -c 'import subprocess, sys; sys.exit(subprocess.call(sys.argv[1:]))' \
            "$(cygpath -w "$msys2_root/usr/bin/bash.exe")" --noprofile --norc \
            "$(cygpath -m "$top/build-ffmpeg.sh")" \
            "$(cygpath -m "$top/$FFMPEG_SOURCE_DIR")" \
            "$(cygpath -m "$build_dir")" \
            "$(cygpath -m "$install_dir")" \
            --toolchain=msvc --target-os=win64 --arch=x86_64 \
            --enable-w32threads --extra-cflags=-MD

        mkdir -p "$stage/bin/release" "$stage/lib/release"
        cp "$install_dir"/bin/avcodec-*.dll "$install_dir"/bin/avutil-*.dll \
           "$install_dir"/bin/swresample-*.dll "$stage/bin/release/"
        # Import libraries (FFmpeg installs them beside the DLLs). The viewer
        # loads the DLLs at runtime; the .lib files allow ordinary linking.
        cp "$install_dir"/bin/avcodec.lib "$install_dir"/bin/avutil.lib \
           "$install_dir"/bin/swresample.lib "$stage/lib/release/"
    ;;

    linux*)
        # RUNPATH $ORIGIN: each library finds its siblings next to itself,
        # whichever directory the viewer loads them from. The option goes
        # through a linker response file because a literal '$' does not
        # survive configure's eval plus the Makefiles' double expansion.
        rpath_file="$top/build/rpath-origin.opt"
        mkdir -p "$top/build"
        printf '%s\n' '-rpath=$ORIGIN' > "$rpath_file"
        bash "$top/build-ffmpeg.sh" \
            "$top/$FFMPEG_SOURCE_DIR" "$build_dir" "$install_dir" \
            --arch=x86_64 --enable-pthreads \
            "--extra-ldsoflags=-Wl,@$rpath_file"

        mkdir -p "$stage/lib/release"
        # Real files plus the SONAME and development symlinks.
        cp -P "$install_dir"/lib/libavcodec.so* "$install_dir"/lib/libavutil.so* \
              "$install_dir"/lib/libswresample.so* "$stage/lib/release/"
    ;;

    *)
        echo "3p-ffmpeg packages windows64 and linux64 only" >&2
        exit 1
    ;;
esac

mkdir -p "$stage/include"
for lib in libavcodec libavutil libswresample; do
    rm -rf "$stage/include/$lib"
    cp -R "$install_dir/include/$lib" "$stage/include/"
done

# LGPL text followed by FFmpeg's own licence statement.
mkdir -p "$stage/LICENSES"
cat "$FFMPEG_SOURCE_DIR/COPYING.LGPLv2.1" "$FFMPEG_SOURCE_DIR/LICENSE.md" > "$stage/LICENSES/ffmpeg.txt"

git -C "$FFMPEG_SOURCE_DIR" rev-parse HEAD > "$stage/SOURCE_REVISION.txt"
# The exact configure invocation, as recorded by configure itself; the
# libraries report the same string through avcodec_configuration().
sed -n -E 's/^#define FFMPEG_CONFIGURATION "(.*)"$/\1/p' "$build_dir/config.h" > "$stage/CONFIGURATION.txt"
test -s "$stage/CONFIGURATION.txt"
