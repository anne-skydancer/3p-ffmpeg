"""Verify a built 3p-ffmpeg autobuild archive (.tar.zst).

Checks:
  * autobuild metadata (name, version, licence, platform, build id, not dirty);
  * Linux: no glibc symbol version newer than --max-glibc (Ubuntu 22.04);
  * the archive's file list for the platform (headers, shared libraries and,
    on Linux, the SONAME symlinks), the licence text and CONFIGURATION.txt;
  * what the shipped libraries report about themselves when loaded:
    library majors match the headers, licence is LGPL 2.1+, the configuration
    has no GPL/nonfree/version3 switches, the only codecs are the AAC and Opus
    decoders, and every entry point the viewer resolves is exported;
  * real decoding of HE-AAC v1, HE-AAC v2 (ADTS) and Ogg Opus fixtures from
    tests/media through ctypes, using the struct offsets the viewer's Rust
    bindings use (OFFSETS below; regenerate them for a new FFmpeg major).

usage: python verify_package.py ARCHIVE [--allow-dirty] [--max-glibc 2.35]

Requires Python 3.14 (compression.zstd) or the 'zstandard' module; the
archive must be for the host platform (windows64 on Windows, linux64 on Linux).
"""
import argparse
import ctypes
import io
import math
import os
from pathlib import Path
import re
import sys
import tarfile
import tempfile
import xml.etree.ElementTree as ET

HERE = Path(__file__).resolve().parent
MEDIA = HERE / 'tests' / 'media'

# Struct offsets (bytes) for libavcodec 63 / libavutil 61 on x86-64 Windows
# and Linux, measured with offsetof() against the packaged headers. Majors
# are checked below; a new major means re-measuring these.
ABI = {'avcodec': 63, 'avutil': 61, 'swresample': 7}
OFFSETS = {
    'AVFrame.extended_data': 96,
    'AVFrame.nb_samples': 112,
    'AVFrame.format': 116,
    'AVFrame.sample_rate': 180,
    'AVFrame.ch_layout': 384,
    'AVChannelLayout.order': 0,
    'AVChannelLayout.nb_channels': 4,
    'AVChannelLayout.u.mask': 8,
    'AVCodecContext.extradata': 72,
    'AVCodecContext.extradata_size': 80,
    'AVPacket.data': 24,
    'AVPacket.size': 32,
    'AVCodec.name': 0,
}
AV_SAMPLE_FMT_FLTP = 8
AV_CODEC_ID_AAC = 86018
AV_CODEC_ID_OPUS = 86076
AVERROR_EAGAIN = -11
AVERROR_EOF = -0x20464F45  # -MKTAG('E','O','F',' ')
AV_INPUT_BUFFER_PADDING_SIZE = 64
AV_CH_LAYOUT_STEREO = 0x3
AV_CHANNEL_ORDER_NATIVE = 1

# Entry points the viewer's Rust engine resolves at runtime.
REQUIRED_SYMBOLS = {
    'avcodec': [
        'avcodec_version', 'avcodec_license', 'avcodec_configuration',
        'avcodec_find_decoder', 'avcodec_find_decoder_by_name',
        'avcodec_alloc_context3', 'avcodec_open2', 'avcodec_free_context',
        'avcodec_flush_buffers', 'avcodec_send_packet', 'avcodec_receive_frame',
        'av_packet_alloc', 'av_packet_free', 'av_new_packet', 'av_packet_unref',
        'av_codec_iterate', 'av_codec_is_decoder', 'av_codec_is_encoder',
    ],
    'avutil': [
        'avutil_version', 'av_frame_alloc', 'av_frame_free', 'av_frame_unref',
        'av_malloc', 'av_mallocz', 'av_free', 'av_strerror', 'av_log_set_level',
        'av_log_set_callback', 'av_channel_layout_uninit',
        'av_get_bytes_per_sample', 'av_sample_fmt_is_planar',
    ],
    'swresample': ['swresample_version'],
}


def fail(message):
    raise SystemExit('FAIL: ' + message)


def check(condition, message):
    if not condition:
        fail(message)


# ---------------------------------------------------------------- archive

def read_tar_zst(path):
    data = path.read_bytes()
    try:
        from compression import zstd  # Python 3.14+
        raw = zstd.decompress(data)
    except ImportError:
        import zstandard  # type: ignore[import-not-found]
        raw = zstandard.ZstdDecompressor().stream_reader(io.BytesIO(data)).read()
    return tarfile.open(fileobj=io.BytesIO(raw), mode='r:')


def llsd_value(node):
    tag = node.tag
    if tag == 'map':
        children = list(node)
        return {children[i].text or '': llsd_value(children[i + 1]) for i in range(0, len(children), 2)}
    if tag == 'array':
        return [llsd_value(child) for child in node]
    if tag == 'boolean':
        return (node.text or '').strip().lower() in ('1', 'true')
    if tag == 'integer':
        return int(node.text or 0)
    if tag == 'undef':
        return None
    return node.text or ''


def header_major(root, lib, name):
    for header in ('version_major.h', 'version.h'):
        path = root / 'include' / lib / header
        if path.exists():
            match = re.search(r'#define\s+%s\s+(\d+)' % name, path.read_text())
            if match:
                return int(match.group(1))
    fail('no %s in %s headers' % (name, lib))


# ---------------------------------------------------------------- libraries

def load_libraries(root, platform):
    libdir = root / ('bin/release' if platform == 'windows64' else 'lib/release')
    names = {
        'windows64': {lib: '%s-%d.dll' % (lib, major) for lib, major in ABI.items()},
        'linux64': {lib: 'lib%s.so.%d' % (lib, major) for lib, major in ABI.items()},
    }[platform]
    libs = {}
    # avcodec first: its dependencies must resolve from its own directory
    # (LOAD_LIBRARY_SEARCH_DLL_LOAD_DIR on Windows, RUNPATH $ORIGIN on Linux).
    for lib in ('avcodec', 'avutil', 'swresample'):
        path = str((libdir / names[lib]).resolve())
        if platform == 'windows64':
            libs[lib] = ctypes.CDLL(path, winmode=0x100 | 0x1000)
        else:
            libs[lib] = ctypes.CDLL(path, mode=os.RTLD_NOW | os.RTLD_LOCAL)
    for lib, symbols in REQUIRED_SYMBOLS.items():
        for symbol in symbols:
            check(hasattr(libs[lib], symbol), '%s does not export %s' % (names[lib], symbol))
    return libs


def setup_prototypes(av):
    c, u = av['avcodec'], av['avutil']
    vp, ci, cu = ctypes.c_void_p, ctypes.c_int, ctypes.c_uint
    for lib, name, res, args in (
        (c, 'avcodec_version', cu, []), (u, 'avutil_version', cu, []),
        (av['swresample'], 'swresample_version', cu, []),
        (c, 'avcodec_license', ctypes.c_char_p, []), (c, 'avcodec_configuration', ctypes.c_char_p, []),
        (c, 'av_codec_iterate', vp, [ctypes.POINTER(vp)]),
        (c, 'av_codec_is_decoder', ci, [vp]), (c, 'av_codec_is_encoder', ci, [vp]),
        (c, 'avcodec_find_decoder', vp, [ci]), (c, 'avcodec_find_decoder_by_name', vp, [ctypes.c_char_p]),
        (c, 'avcodec_alloc_context3', vp, [vp]), (c, 'avcodec_open2', ci, [vp, vp, vp]),
        (c, 'avcodec_free_context', None, [ctypes.POINTER(vp)]),
        (c, 'avcodec_send_packet', ci, [vp, vp]), (c, 'avcodec_receive_frame', ci, [vp, vp]),
        (c, 'av_packet_alloc', vp, []), (c, 'av_packet_free', None, [ctypes.POINTER(vp)]),
        (c, 'av_new_packet', ci, [vp, ci]), (c, 'av_packet_unref', None, [vp]),
        (u, 'av_frame_alloc', vp, []), (u, 'av_frame_free', None, [ctypes.POINTER(vp)]),
        (u, 'av_mallocz', vp, [ctypes.c_size_t]), (u, 'av_log_set_level', None, [ci]),
    ):
        fn = getattr(lib, name)
        fn.restype, fn.argtypes = res, args


def read(ptr, offset, ctype):
    return ctype.from_address(ptr + offset).value


def write(ptr, offset, ctype, value):
    ctype.from_address(ptr + offset).value = value


def decode(av, codec_id, packets, extradata=b''):
    c, u = av['avcodec'], av['avutil']
    codec = c.avcodec_find_decoder(codec_id)
    check(codec, 'decoder %d missing' % codec_id)
    ctx = c.avcodec_alloc_context3(codec)
    check(ctx, 'avcodec_alloc_context3 failed')
    if extradata:
        buf = u.av_mallocz(len(extradata) + AV_INPUT_BUFFER_PADDING_SIZE)
        ctypes.memmove(buf, extradata, len(extradata))
        write(ctx, OFFSETS['AVCodecContext.extradata'], ctypes.c_void_p, buf)
        write(ctx, OFFSETS['AVCodecContext.extradata_size'], ctypes.c_int, len(extradata))
    check(c.avcodec_open2(ctx, codec, None) == 0, 'avcodec_open2 failed')
    pkt, frame = c.av_packet_alloc(), u.av_frame_alloc()
    out = {'frames': 0, 'samples': 0, 'pcm': [[], []]}

    def drain():
        while True:
            ret = c.avcodec_receive_frame(ctx, frame)
            if ret in (AVERROR_EAGAIN, AVERROR_EOF):
                return
            check(ret == 0, 'avcodec_receive_frame returned %d' % ret)
            n = read(frame, OFFSETS['AVFrame.nb_samples'], ctypes.c_int)
            layout = frame + OFFSETS['AVFrame.ch_layout']
            info = (read(frame, OFFSETS['AVFrame.sample_rate'], ctypes.c_int),
                    read(layout, OFFSETS['AVChannelLayout.nb_channels'], ctypes.c_int),
                    read(layout, OFFSETS['AVChannelLayout.order'], ctypes.c_int),
                    read(layout, OFFSETS['AVChannelLayout.u.mask'], ctypes.c_uint64),
                    read(frame, OFFSETS['AVFrame.format'], ctypes.c_int))
            out.setdefault('info', info)
            check(info == out['info'], 'stream parameters changed mid-stream: %r' % (info,))
            planes = read(frame, OFFSETS['AVFrame.extended_data'], ctypes.c_void_p)
            for ch in range(min(info[1], 2)):
                plane = ctypes.c_void_p.from_address(planes + ch * ctypes.sizeof(ctypes.c_void_p)).value
                out['pcm'][ch].extend((ctypes.c_float * n).from_address(plane))
            out['frames'] += 1
            out['samples'] += n

    for payload in packets:
        check(c.av_new_packet(pkt, len(payload)) == 0, 'av_new_packet failed')
        ctypes.memmove(read(pkt, OFFSETS['AVPacket.data'], ctypes.c_void_p), payload, len(payload))
        check(read(pkt, OFFSETS['AVPacket.size'], ctypes.c_int) == len(payload), 'AVPacket.size offset')
        ret = c.avcodec_send_packet(ctx, pkt)
        check(ret == 0, 'avcodec_send_packet returned %d' % ret)
        c.av_packet_unref(pkt)
        drain()
    check(c.avcodec_send_packet(ctx, None) == 0, 'flush failed')
    drain()
    for handle, free in ((pkt, c.av_packet_free), (frame, u.av_frame_free), (ctx, c.avcodec_free_context)):
        free(ctypes.byref(ctypes.c_void_p(handle)))
    return out


def rms(samples):
    tail = samples[len(samples) // 4:]  # skip codec start-up
    return math.sqrt(sum(x * x for x in tail) / len(tail))


def adts_frames(data):
    pos, frames = 0, []
    while pos + 7 <= len(data):
        check(data[pos] == 0xFF and data[pos + 1] & 0xF6 == 0xF0, 'bad ADTS sync at %d' % pos)
        length = ((data[pos + 3] & 3) << 11) | (data[pos + 4] << 3) | (data[pos + 5] >> 5)
        check(length >= 7 and pos + length <= len(data), 'truncated ADTS frame')
        frames.append(data[pos:pos + length])
        pos += length
    return frames


def ogg_packets(data):
    pos, packets, current = 0, [], b''
    while pos < len(data):
        check(data[pos:pos + 4] == b'OggS', 'bad Ogg page at %d' % pos)
        segments = data[pos + 26]
        lacing = data[pos + 27:pos + 27 + segments]
        body = pos + 27 + segments
        for size in lacing:
            current += data[body:body + size]
            body += size
            if size < 255:
                packets.append(current)
                current = b''
        pos = body
    return packets


def verify_decoding(av):
    av['avutil'].av_log_set_level(16)  # AV_LOG_ERROR
    # (fixture, codec, packets, extradata, rate, channels, samples per frame)
    v1 = adts_frames((MEDIA / 'he-aac-v1.aac').read_bytes())
    v2 = adts_frames((MEDIA / 'he-aac-v2.aac').read_bytes())
    opus = ogg_packets((MEDIA / 'opus-stereo.opus').read_bytes())
    check(opus[0].startswith(b'OpusHead') and opus[1].startswith(b'OpusTags'), 'Opus headers')
    pre_skip = int.from_bytes(opus[0][10:12], 'little')
    cases = (
        ('HE-AAC v1 (ADTS 22.05 kHz stereo core + SBR)', AV_CODEC_ID_AAC, v1, b'', 44100, 2048),
        ('HE-AAC v2 (ADTS 22.05 kHz mono core + SBR + PS)', AV_CODEC_ID_AAC, v2, b'', 44100, 2048),
        ('Opus (Ogg, OpusHead extradata)', AV_CODEC_ID_OPUS, opus[2:], opus[0], 48000, 960),
    )
    for label, codec_id, packets, extradata, rate, frame_size in cases:
        out = decode(av, codec_id, packets, extradata)
        got_rate, channels, order, mask, fmt = out['info']
        check(got_rate == rate, '%s: sample rate %d, expected %d (SBR not applied?)' % (label, got_rate, rate))
        check(channels == 2 and order == AV_CHANNEL_ORDER_NATIVE and mask == AV_CH_LAYOUT_STEREO,
              '%s: layout %d ch order %d mask %#x, expected native stereo' % (label, channels, order, mask))
        check(fmt == AV_SAMPLE_FMT_FLTP, '%s: sample format %d, expected FLTP' % (label, fmt))
        expected = len(packets) * frame_size - (pre_skip if codec_id == AV_CODEC_ID_OPUS else 0)
        check(out['samples'] == expected, '%s: %d samples, expected %d' % (label, out['samples'], expected))
        # Fixtures: left 440 Hz, right 9 kHz (carried by SBR in HE-AAC), both -6 dBFS.
        left, right = rms(out['pcm'][0]), rms(out['pcm'][1])
        check(0.25 < left < 0.45, '%s: left RMS %.4f' % (label, left))
        check(right > 0.02, '%s: right (high band) RMS %.4f, SBR/PS output missing' % (label, right))
        print('decoded %s: %d Hz, %d ch, FLTP, %d frames, %d samples/ch, RMS L %.3f R %.3f'
              % (label, got_rate, channels, out['frames'], out['samples'], left, right))


def verify_library_identity(av, root, configuration):
    c = av['avcodec']
    for lib, macro in (('avcodec', 'LIBAVCODEC_VERSION_MAJOR'), ('avutil', 'LIBAVUTIL_VERSION_MAJOR'),
                       ('swresample', 'LIBSWRESAMPLE_VERSION_MAJOR')):
        runtime = getattr(av[lib], lib + '_version')() >> 16
        headers = header_major(root, 'lib' + lib, macro)
        check(runtime == headers == ABI[lib], '%s major: runtime %d, headers %d, expected %d'
              % (lib, runtime, headers, ABI[lib]))
    license_text = c.avcodec_license().decode()
    check(license_text == 'LGPL version 2.1 or later', 'avcodec_license() = %r' % license_text)
    reported = c.avcodec_configuration().decode()
    check(reported == configuration, 'avcodec_configuration() differs from CONFIGURATION.txt')
    for forbidden in ('--enable-gpl', '--enable-nonfree', '--enable-version3', '--enable-libfdk'):
        check(forbidden not in reported, 'configuration contains ' + forbidden)
    for required in ('--disable-everything', '--enable-decoder=aac', '--enable-decoder=opus', '--disable-autodetect'):
        check(required in reported, 'configuration lacks ' + required)
    codecs, opaque = [], ctypes.c_void_p(None)
    while True:
        codec = c.av_codec_iterate(ctypes.byref(opaque))
        if not codec:
            break
        name = ctypes.c_char_p.from_address(codec + OFFSETS['AVCodec.name']).value.decode()
        codecs.append((name, bool(c.av_codec_is_decoder(codec)), bool(c.av_codec_is_encoder(codec))))
    check(sorted(codecs) == [('aac', True, False), ('opus', True, False)], 'codecs: %r' % codecs)
    print('libraries: avcodec %d, avutil %d, swresample %d; %s; codecs %s'
          % (ABI['avcodec'], ABI['avutil'], ABI['swresample'], license_text, ', '.join(n for n, _, _ in codecs)))


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('archive', type=Path)
    parser.add_argument('--allow-dirty', action='store_true', help='accept a package marked dirty')
    parser.add_argument('--max-glibc', default='2.35', help='newest glibc symbol version allowed (Linux)')
    args = parser.parse_args()

    platform = {'win32': 'windows64', 'linux': 'linux64'}.get(sys.platform)
    check(platform, 'unsupported host platform ' + sys.platform)
    # ignore_cleanup_errors: Windows cannot delete DLLs that are still loaded.
    with read_tar_zst(args.archive) as tar, tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
        members = {m.name: m for m in tar.getmembers()}
        tar.extractall(tmp, filter='data')
        root = Path(tmp)

        meta = llsd_value(ET.fromstring((root / 'autobuild-package.xml').read_bytes()).find('map'))
        desc = meta['package_description']
        version = (root / 'VERSION.txt').read_text().strip() if (root / 'VERSION.txt').exists() else desc['version']
        check(meta.get('type') == 'metadata', 'not an autobuild metadata file')
        check(meta['platform'] == platform, 'archive platform %s, host %s' % (meta['platform'], platform))
        check(desc['name'] == 'ffmpeg', 'package name %r' % desc['name'])
        check(desc['license'] == 'LGPL-2.1-or-later', 'license %r' % desc['license'])
        check(desc['license_file'] == 'LICENSES/ffmpeg.txt', 'license_file %r' % desc['license_file'])
        check(desc.get('copyright'), 'copyright missing')
        check(desc['canonical_repo'] == 'https://github.com/anne-skydancer/3p-ffmpeg', 'canonical_repo')
        check(re.fullmatch(r'\d+\.\d+(\.\d+)?\.\d+', desc['version']), 'version %r' % desc['version'])
        check(desc['version'] == version, 'metadata version %s != VERSION.txt %s' % (desc['version'], version))
        check(str(meta['build_id']) == desc['version'].rsplit('.', 1)[1],
              'build_id %s does not end the version %s' % (meta['build_id'], desc['version']))
        check(args.allow_dirty or not meta.get('dirty', False), 'package is marked dirty')

        names = set(members)
        expected = {'autobuild-package.xml', 'LICENSES/ffmpeg.txt', 'SOURCE_REVISION.txt', 'CONFIGURATION.txt',
                    'include/libavcodec/avcodec.h', 'include/libavcodec/version_major.h',
                    'include/libavutil/frame.h', 'include/libavutil/channel_layout.h', 'include/libavutil/avconfig.h',
                    'include/libswresample/swresample.h'}
        if platform == 'windows64':
            for lib, major in ABI.items():
                expected |= {'bin/release/%s-%d.dll' % (lib, major), 'lib/release/%s.lib' % lib}
            binaries = {n for n in names if n.startswith(('bin/', 'lib/'))}
        else:
            for lib, major in ABI.items():
                soname = 'lib/release/lib%s.so.%d' % (lib, major)
                expected |= {soname, 'lib/release/lib%s.so' % lib}
                check(members[soname].issym(), soname + ' is not a symlink')
                real = members[soname].linkname
                check(re.fullmatch(r'lib%s\.so\.%d\.\d+\.\d+' % (lib, major), real), soname + ' -> ' + real)
                check(members.get('lib/release/' + real) is not None and members['lib/release/' + real].isfile(),
                      'missing ' + real)
                check(members['lib/release/lib%s.so' % lib].linkname == real, 'lib%s.so link' % lib)
            binaries = {n for n in names if n.startswith('lib/')}
        missing = expected - names
        check(not missing, 'missing from archive: %s' % ', '.join(sorted(missing)))
        unexpected = {n for n in binaries if members[n].isfile() or members[n].issym()} - expected
        unexpected = {n for n in unexpected if not re.fullmatch(r'lib/release/lib\w+\.so\.\d+\.\d+\.\d+', n)}
        check(not unexpected, 'unexpected binaries: %s' % ', '.join(sorted(unexpected)))
        for forbidden in ('libavformat', 'libavfilter', 'libavdevice', 'libswscale', 'libpostproc'):
            check(not any(forbidden in n or forbidden[3:] + '-' in n for n in names), forbidden + ' packaged')
        print('archive: %s %s %s, build_id %s, %d files' % (desc['name'], desc['version'], meta['platform'],
                                                          meta['build_id'], len(names)))

        licence = (root / 'LICENSES/ffmpeg.txt').read_text(encoding='utf-8')
        check(licence.lstrip().startswith('GNU LESSER GENERAL PUBLIC LICENSE')
              and 'Version 2.1, February 1999' in licence, 'LGPL 2.1 text missing from licence file')
        check('# License' in licence and 'GNU Lesser General Public License' in licence,
              'FFmpeg LICENSE.md missing from licence file')
        configuration = (root / 'CONFIGURATION.txt').read_text().strip()

        if platform == 'linux64':
            # The libraries must load on the oldest supported distribution
            # (Ubuntu 22.04, glibc 2.35); a newer build host raises symbol
            # versions (e.g. atan2f@GLIBC_2.43 on Ubuntu 26.04).
            ceiling = tuple(int(x) for x in args.max_glibc.split('.'))
            for lib in ABI:
                data = (root / 'lib/release' / ('lib%s.so.%d' % (lib, ABI[lib]))).resolve().read_bytes()
                versions = {tuple(map(int, m)) for m in re.findall(rb'GLIBC_(\d+)\.(\d+)', data)}
                newest = max(versions)
                check(newest <= ceiling, 'lib%s needs GLIBC_%d.%d, newer than %s' % (lib, *newest, args.max_glibc))
                print('lib%s: newest glibc symbol version GLIBC_%d.%d' % (lib, *newest))

        av = load_libraries(root, platform)
        setup_prototypes(av)
        verify_library_identity(av, root, configuration)
        verify_decoding(av)
    print('PASS: %s is a decoder-only LGPL FFmpeg package for %s' % (args.archive.name, platform))


if __name__ == '__main__':
    main()
