#!/usr/bin/env python3
"""Report (and optionally enforce) the newest GLIBC_ version Linux libraries need.

Shared objects: versions in `objdump -T` output.
Static archives: every undefined, non-weak symbol (`nm -A`) must be defined
inside the scanned package or in the system libraries (libc, libm, libdl,
libpthread, librt, libstdc++, libgcc_s); the newest default-versioned glibc
symbol among the ones resolved in glibc counts as the requirement.

usage: glibc_report.py [--max X.Y] PATH [PATH...]
  --max  exit 1 if the requirement exceeds X.Y or a static-archive symbol is unresolved
Writes Markdown to stdout and $GITHUB_STEP_SUMMARY.
"""
import argparse, os, re, subprocess, sys

def run(*a):
    return subprocess.run(a, capture_output=True, text=True, errors='replace').stdout

def ver(s):
    return tuple(int(x) for x in s.split('.'))

def vs(v):
    return '.'.join(map(str, v))

def system_libs():
    glibc, others = {}, set()
    for lib in ('libc.so.6', 'libm.so.6', 'libdl.so.2', 'libpthread.so.0', 'librt.so.1',
                'libstdc++.so.6', 'libgcc_s.so.1'):
        path = run('gcc', '-print-file-name=' + lib).strip()
        if not os.path.isabs(path):
            path = '/lib/x86_64-linux-gnu/' + lib
        if not os.path.exists(path):
            continue
        for line in run('objdump', '-T', path).splitlines():
            if '*UND*' in line:
                continue
            m = re.search(r'\sGLIBC_(\d+(?:\.\d+)+)\s+(\S+)\s*$', line)  # default (not parenthesised)
            if m:
                glibc[m.group(2)] = ver(m.group(1))
                continue
            m = re.search(r'\s(?:\(?[A-Z_0-9.]+\)?)\s+(\S+)\s*$', line)
            if m:
                others.add(m.group(1))
    return glibc, others

def files(paths):
    for p in paths:
        if os.path.isdir(p):
            for r, _, fs in os.walk(p):
                for f in fs:
                    yield os.path.join(r, f)
        else:
            yield p

NM = re.compile(r'^.*?:\s*(?:[0-9a-fA-F]+\s+)?([A-Za-z])\s+(\S+)$')

def nm_syms(f):
    und, dfn = set(), set()
    for l in run('nm', '-A', '--no-demangle', f).splitlines():
        m = NM.match(l.strip())
        if not m:
            continue
        t, s = m.groups()
        s = s.split('@')[0]
        if s == '_GLOBAL_OFFSET_TABLE_':
            continue  # linker-synthesised
        if t == 'U':
            und.add(s)
        elif t in 'wv':
            pass
        elif t != '-' and t != 'N':
            dfn.add(s)
    return und, dfn

ap = argparse.ArgumentParser()
ap.add_argument('--max')
ap.add_argument('--json', help='write {"newest_glibc": "X.Y"} to this file')
ap.add_argument('paths', nargs='+')
args = ap.parse_args()
limit = ver(args.max) if args.max else None

flist = [f for f in sorted(files(args.paths)) if not os.path.islink(f)]
out = ['### Linux glibc requirements', '', 'Runner libc: ' + run('ldd', '--version').splitlines()[0], '']
overall, bad = (0,), []
archives = [f for f in flist if f.endswith('.a')]
glibc = others = package_defs = None
if archives:
    glibc, others = system_libs()
    package_defs = set()
    for f in flist:
        if f.endswith('.a') or re.search(r'\.so(\.\d+)*$', f):
            package_defs |= nm_syms(f)[1] if f.endswith('.a') else {l.split()[-1].split('@')[0] for l in run('objdump', '-T', f).splitlines() if '*UND*' not in l and l.strip()}
for f in flist:
    if re.search(r'\.so(\.\d+)*$', f):
        v = [ver(m) for m in re.findall(r'GLIBC_(\d+(?:\.\d+)+)', run('objdump', '-T', f))]
        newest = max(v) if v else (0,)
        out.append('- `%s`: GLIBC_%s (objdump -T)' % (f, vs(newest)))
    elif f.endswith('.a'):
        und, _ = nm_syms(f)
        hits = {s: glibc[s] for s in und if s in glibc and s not in package_defs}
        unresolved = sorted(s for s in und if s not in package_defs and s not in glibc and s not in others)
        newest = max(hits.values()) if hits else (0,)
        top = sorted(hits, key=lambda s: hits[s])[-3:]
        out.append('- `%s`: GLIBC_%s (newest: %s)%s' % (f, vs(newest),
                   ', '.join('%s@%s' % (s, vs(hits[s])) for s in top),
                   '; UNRESOLVED: ' + ', '.join(unresolved[:20]) if unresolved else ''))
        if unresolved:
            bad.append('%s: unresolved symbols %s' % (f, unresolved[:20]))
    else:
        continue
    overall = max(overall, newest)
    if limit and newest > limit:
        bad.append('%s needs GLIBC_%s > %s' % (f, vs(newest), args.max))
out += ['', '**Newest GLIBC_ needed overall: %s**' % vs(overall)]
if limit:
    out.append('Limit %s: %s' % (args.max, 'FAIL - ' + '; '.join(bad) if bad else 'ok'))
text = '\n'.join(out)
print(text)
if os.environ.get('GITHUB_STEP_SUMMARY'):
    with open(os.environ['GITHUB_STEP_SUMMARY'], 'a') as fh:
        fh.write(text + '\n')
sys.exit(1 if (limit and bad) else 0)
