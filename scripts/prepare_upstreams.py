"""Fetch locked upstream revisions, then apply project-specific changes once."""
import json
import subprocess
from pathlib import Path

root = Path(__file__).resolve().parents[1]
for name, source in json.loads((root / 'upstream-lock.json').read_text()).items():
    target = root / name
    if target.exists():
        raise SystemExit(f'{target} exists. Refusing to overwrite an existing checkout.')
    subprocess.run(['git', 'clone', '--no-checkout', source['url'], str(target)], check=True)
    subprocess.run(['git', '-C', str(target), 'checkout', '--detach', source['revision']], check=True)
    subprocess.run(['git', '-C', str(target), 'apply', '--check', str(root / 'patches' / f'{name}.patch')], check=True)
    subprocess.run(['git', '-C', str(target), 'apply', str(root / 'patches' / f'{name}.patch')], check=True)
    print(f'Prepared {name}: {source["revision"]}')
