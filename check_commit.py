"""Assemble affected Atelier variants at an exact commit and run kernel checks."""
from __future__ import annotations
import argparse
import configparser
import json
import os
from pathlib import Path
import re
import subprocess
import sys
from urllib.parse import urljoin, urlparse

OWNER = 'NomadAnget'
VARIANTS = ('atelier-studio', 'atelier-reel-solo', 'atelier-reel-creator')
CORE = f'{OWNER}/atelier-core'


def command(args, cwd=None, env=None):
    result = subprocess.run([str(x) for x in args], cwd=cwd, env=env,
                            capture_output=True, text=True, timeout=900)
    if result.returncode:
        raise RuntimeError(f'{args[0]} failed ({result.returncode}): {result.stderr[-3000:]}')
    return result.stdout.strip()


def git(path, *args):
    return command(['git', '-C', path, *args])


def checkout(repository, destination, sha=None):
    print(f'Checking out {repository}@{sha or "default branch"}', flush=True)
    command(['git', 'clone', '--quiet', '--no-checkout',
             f'https://github.com/{repository}.git', destination])
    if sha:
        git(destination, 'fetch', '--quiet', '--no-tags', 'origin', sha)
        git(destination, 'checkout', '--quiet', '--detach', sha)
    else:
        git(destination, 'checkout', '--quiet', '--detach', 'origin/HEAD')


def modules(checkout_path, repository):
    config = configparser.ConfigParser(interpolation=None)
    config.read(checkout_path / '.gitmodules')
    entries = []
    for section in config.sections():
        path = config.get(section, 'path')
        target = (checkout_path / path).resolve()
        if checkout_path.resolve() not in target.parents:
            raise ValueError(f'Submodule path escapes checkout: {path}')
        url = config.get(section, 'url')
        resolved = urljoin(f'https://github.com/{repository}.git/', url)
        parsed = urlparse(resolved)
        if parsed.scheme != 'https' or parsed.netloc != 'github.com':
            raise ValueError('Only HTTPS GitHub submodules are allowed')
        name = parsed.path.strip('/').removesuffix('.git')
        if not (name.startswith(OWNER + '/') or name == 'Brainicism/bgutil-ytdlp-pot-provider'):
            raise ValueError(f'Unexpected submodule repository: {name}')
        entries.append((path, name))
    return entries


def revision(path):
    return {'sha': git(path, 'rev-parse', 'HEAD'),
            'commit': git(path, 'show', '-s', '--format=fuller', 'HEAD')}


def run_check(python, args, checkout_path, work, env):
    argv = [str(python), *args]
    result = subprocess.run(argv, cwd=checkout_path, env=env,
                            capture_output=True, text=True, timeout=900)
    output = result.stdout + result.stderr
    name = 'constitution' if 'atelier_core.discipline' in args else 'assembly'
    (work / f'{name}.log').write_text(output)
    print(f'[{checkout_path.name}/{name}] exit={result.returncode}', flush=True)
    print(output, flush=True)
    return {'command': argv, 'exit_code': result.returncode}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--repository', required=True, help='Changed owner/repository')
    parser.add_argument('--sha', required=True, help='Exact source commit, not a branch')
    parser.add_argument('--work', type=Path, required=True, help='New, empty work directory')
    parser.add_argument('--variant', choices=VARIANTS, action='append')
    parser.add_argument('--prepared-python', type=Path,
                        help='Local verification only: use an already prepared Python environment')
    args = parser.parse_args()
    if not re.fullmatch(r'NomadAnget/atelier-[A-Za-z0-9_.-]+', args.repository):
        parser.error('Only owner Atelier repositories are accepted')
    if not re.fullmatch(r'[0-9a-f]{40}', args.sha):
        parser.error('sha must be a full 40-character commit SHA')
    args.work = args.work.resolve()
    args.work.mkdir(parents=True, exist_ok=False)
    # Only Git transport sees the existing read credential. No tokens in URLs/config.
    if os.environ.get('GH_TOKEN'):
        os.environ.update(GIT_CONFIG_COUNT='1', GIT_CONFIG_KEY_0='credential.helper',
                          GIT_CONFIG_VALUE_0='!gh auth git-credential')
    os.environ['GIT_ALLOW_PROTOCOL'] = 'https'
    os.environ['GIT_TERMINAL_PROMPT'] = '0'
    report = {'repository': args.repository, 'source_sha': args.sha, 'variants': []}
    try:
        core = args.work / 'core'
        checkout(CORE, core, args.sha if args.repository == CORE else None)
        report['core'] = revision(core)
        prepared = args.prepared_python.resolve() if args.prepared_python else None
        for name in args.variant or VARIANTS:
            path = args.work / name
            item = {'repository': f'{OWNER}/{name}', 'checks': []}
            report['variants'].append(item)
            try:
                checkout(item['repository'], path,
                         args.sha if args.repository == item['repository'] else None)
                item['base'] = revision(path)
                entries = modules(path, item['repository'])
                targets = [p for p, repo in entries if repo == args.repository]
                if args.repository not in (item['repository'], CORE) and not targets:
                    item['skipped'] = 'This variant does not consume the changed repository'
                    continue
                print(f'[{name}] Fetching pinned recursive submodules', flush=True)
                git(path, 'submodule', 'update', '--init', '--recursive')
                for target in targets:
                    git(path / target, 'fetch', '--quiet', '--no-tags', 'origin', args.sha)
                    git(path / target, 'checkout', '--quiet', '--detach', args.sha)
                    git(path / target, 'submodule', 'update', '--init', '--recursive')
                    git(path, 'add', '--', target)
                if git(path, 'diff', '--cached', '--name-only'):
                    git(path, '-c', 'user.name=Atelier CI', '-c', 'user.email=ci@localhost',
                        'commit', '--quiet', '-m', f'CI assembly: {args.repository}@{args.sha}')
                item['assembled'] = revision(path)
                item['submodules'] = {p: revision(path / p) for p, _ in entries}
                if targets and any(item['submodules'][p]['sha'] != args.sha for p in targets):
                    raise RuntimeError('Source commit was not installed in the assembled variant')
                home = args.work / (name + '-runtime')
                home.mkdir()
                env = {'PATH': os.environ['PATH'], 'HOME': str(home), 'LANG': 'C.UTF-8',
                       'PYTHONNOUSERSITE': '1', 'GIT_CONFIG_GLOBAL': '/dev/null',
                       'GIT_CONFIG_NOSYSTEM': '1'}
                if prepared:
                    python = prepared
                    item['environment'] = 'existing local verification runtime'
                else:
                    print(f'[{name}] Creating fresh check environment', flush=True)
                    venv = home / '.venv'
                    command(['uv', 'venv', '--python', '3.11', venv], env=env)
                    python = venv / 'bin/python'
                    command(['uv', 'pip', 'install', '--python', python, '--no-cache',
                             core, 'ollama>=0.1.6'], env=env)
                    item['environment'] = 'fresh Python 3.11 + checked-out core + ollama'
                # No extra domain tests, model downloads, server startup or production data.
                for check in (['-I', '-m', 'atelier_core.discipline', str(path / 'src')],
                              ['-m', 'src.main', '--check']):
                    item['checks'].append(run_check(python, check, path, home, env))
                item['success'] = all(c['exit_code'] == 0 for c in item['checks'])
            except Exception as exc:
                item.update(success=False, error=str(exc))
        affected = [v for v in report['variants'] if 'skipped' not in v]
        report['success'] = bool(affected) and all(v.get('success') is True for v in affected)
        if not affected:
            report['error'] = 'No selected variant consumes this repository'
    except Exception as exc:
        report.update(success=False, error=str(exc))
    (args.work / 'result.json').write_text(json.dumps(report, indent=2) + '\n')
    print(json.dumps(report, indent=2), flush=True)
    return 0 if report['success'] else 1


if __name__ == '__main__':
    raise SystemExit(main())
