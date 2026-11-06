#!/usr/bin/env python3
"""Write LICENSES/<name>-buildinfo.json into the stage directory before packaging.

usage: buildinfo.py --stage stage --name NAME --flavour windows64|linux64|linux64-lts
         --upstream-version V --upstream-source URL --upstream-hash H
         --build-type T [--glibc-json FILE] [--option K=V ...]
Environment (GitHub Actions): GITHUB_SHA, GITHUB_RUN_ID, GITHUB_REPOSITORY,
GITHUB_SERVER_URL, ImageOS, ImageVersion.
"""
import argparse, datetime, json, os, platform, re, subprocess

def run(*a):
    try:
        r = subprocess.run(a, capture_output=True, text=True, errors='replace', timeout=60)
        return (r.stdout + r.stderr).strip()
    except Exception:
        return ''

ap = argparse.ArgumentParser()
ap.add_argument('--stage', default='stage')
ap.add_argument('--name', required=True)
ap.add_argument('--flavour', required=True)
ap.add_argument('--upstream-version', required=True)
ap.add_argument('--upstream-source', required=True)
ap.add_argument('--upstream-hash', required=True)
ap.add_argument('--build-type', required=True)
ap.add_argument('--glibc-json')
ap.add_argument('--option', action='append', default=[])
a = ap.parse_args()
e = os.environ.get
linux = platform.system() == 'Linux'
run_id = e('GITHUB_RUN_ID', '')
repo = e('GITHUB_REPOSITORY', '')
info = {
    'package': a.name,
    'package_version': open(os.path.join(a.stage, 'VERSION.txt')).read().strip(),
    'flavour': a.flavour,
    'upstream_source_version': a.upstream_version,
    'upstream_source': a.upstream_source,
    'upstream_source_hash': a.upstream_hash,
    'recipe_repository': (e('GITHUB_SERVER_URL', 'https://github.com') + '/' + repo) if repo else '',
    'recipe_commit': e('GITHUB_SHA', ''),
    'ci_run_id': run_id,
    'ci_run_url': (e('GITHUB_SERVER_URL', 'https://github.com') + '/' + repo + '/actions/runs/' + run_id) if repo and run_id else '',
    'build_type': a.build_type,
    'configure_options': a.option,
    'build_date_utc': datetime.datetime.now(datetime.timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ'),
}
if linux:
    osr = {}
    for l in open('/etc/os-release'):
        if '=' in l:
            k, v = l.strip().split('=', 1); osr[k] = v.strip('"')
    in_container = a.flavour.endswith('lts')
    info['build_os'] = osr.get('PRETTY_NAME', '')
    info['build_environment'] = ('ubuntu:24.04 container on ubuntu-latest runner' if in_container
                                 else 'ubuntu-latest = %s (image %s)' % (osr.get('PRETTY_NAME', ''), e('ImageVersion', 'unknown')))
    info['build_glibc'] = (run('ldd', '--version').splitlines() or [''])[0]
    info['compiler'] = (run('gcc', '--version').splitlines() or [''])[0]
    g = '(not measured)'
    if a.glibc_json and os.path.exists(a.glibc_json):
        g = json.load(open(a.glibc_json))['newest_glibc']
    info['min_glibc_required'] = g
else:
    vw = os.path.join(e('ProgramFiles(x86)', r'C:\Program Files (x86)'), 'Microsoft Visual Studio', 'Installer', 'vswhere.exe')
    vsv = run(vw, '-latest', '-products', '*', '-property', 'catalog_productDisplayVersion') if os.path.exists(vw) else ''
    info['build_os'] = platform.platform()
    info['build_environment'] = 'windows-2022 (image %s)' % e('ImageVersion', 'unknown')
    info['build_glibc'] = 'n/a'
    info['compiler'] = 'MSVC (Visual Studio 2022 %s)' % (vsv or 'version unknown')
    info['min_glibc_required'] = 'n/a'
d = os.path.join(a.stage, 'LICENSES')
os.makedirs(d, exist_ok=True)
out = os.path.join(d, a.name + '-buildinfo.json')
with open(out, 'w', newline='\n') as fh:
    json.dump(info, fh, indent=2); fh.write('\n')
print(open(out).read())
