#!/usr/bin/env python3
"""Report-only: newest GLIBC_ version needed by Linux libraries (job summary).

Shared objects: versions in `objdump -T` undefined entries.
Static archives: undefined symbols (`nm -u`) looked up as default-versioned
symbols in the runner's libc/libm/libdl/libpthread/librt (`objdump -T`).
Usage: glibc_report.py PATH [PATH...]   (files or directories)
"""
import os, re, subprocess, sys

def run(*a):
    return subprocess.run(a, capture_output=True, text=True, errors='replace').stdout

def ver(s):
    return tuple(int(x) for x in s.split('.'))

def libc_defaults():
    table = {}
    for lib in ('libc.so.6', 'libm.so.6', 'libdl.so.2', 'libpthread.so.0', 'librt.so.1'):
        path = run('gcc', '-print-file-name=' + lib).strip()
        if not os.path.isabs(path):
            path = '/lib/x86_64-linux-gnu/' + lib
        for line in run('objdump', '-T', path).splitlines():
            m = re.search(r'\sGLIBC_(\d+(?:\.\d+)+)\s+(\S+)\s*$', line)  # default (not parenthesised)
            if m and '*UND*' not in line:
                table[m.group(2)] = ver(m.group(1))
    return table

def files(paths):
    for p in paths:
        if os.path.isdir(p):
            for r, _, fs in os.walk(p):
                for f in fs:
                    yield os.path.join(r, f)
        else:
            yield p

out = ['### Linux glibc requirements', '', 'Runner libc: ' + run('ldd', '--version').splitlines()[0], '']
table = None
overall = (0,)
for f in sorted(files(sys.argv[1:])):
    if os.path.islink(f):
        continue
    if re.search(r'\.so(\.\d+)*$', f):
        vs = [ver(m) for m in re.findall(r'GLIBC_(\d+(?:\.\d+)+)', run('objdump', '-T', f))]
        newest, why = (max(vs) if vs else (0,)), 'objdump -T'
    elif f.endswith('.a'):
        if table is None:
            table = libc_defaults()
        syms = {l.split()[-1].split('@')[0] for l in run('nm', '-u', '-A', f).splitlines() if l.strip()}
        hits = {s: table[s] for s in syms if s in table}
        newest, why = (max(hits.values()) if hits else (0,)), 'nm -u vs runner libc'
        if hits:
            top = sorted(hits, key=lambda s: hits[s])[-3:]
            why += ' (newest: ' + ', '.join('%s@%s' % (s, '.'.join(map(str, hits[s]))) for s in top) + ')'
    else:
        continue
    overall = max(overall, newest)
    out.append('- `%s`: GLIBC_%s via %s' % (f, '.'.join(map(str, newest)), why))
out += ['', '**Newest GLIBC_ needed overall: %s**' % '.'.join(map(str, overall))]
text = '\n'.join(out)
print(text)
if os.environ.get('GITHUB_STEP_SUMMARY'):
    with open(os.environ['GITHUB_STEP_SUMMARY'], 'a') as fh:
        fh.write(text + '\n')
