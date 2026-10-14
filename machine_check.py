"""Run Atelier's existing constitution checker and Python syntax checks.

Usage: .venv/bin/python machine_check.py /path/to/checkout/src
Requires the project's atelier-core package. No network, tokens, state or merges.
"""
from pathlib import Path
import ast
import subprocess
import sys


def main():
    if len(sys.argv) != 2:
        sys.stderr.write('Usage: machine_check.py SOURCE_DIRECTORY\n')
        return 2
    root = Path(sys.argv[1]).resolve(strict=True)
    if not root.is_dir():
        raise ValueError('Source must be a directory')
    files = sorted(root.rglob('*.py'))
    if not files:
        raise ValueError('No Python source files found; refusing an empty check')
    errors = []
    for path in files:
        try:
            ast.parse(path.read_bytes(), filename=str(path))
        except (SyntaxError, ValueError) as exc:
            errors.append(f'{path}: {exc}')
    if errors:
        sys.stderr.write('\n'.join(errors) + '\n')
        return 1
    # Use the real kernel checker; never maintain a second constitution here.
    result = subprocess.run([sys.executable, '-I', '-m', 'atelier_core.discipline', str(root)],
                            check=False)
    return result.returncode


if __name__ == '__main__':
    raise SystemExit(main())
