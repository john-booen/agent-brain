"""Audit tracked Git snapshots for private path and credential signatures."""
import argparse
import json
from pathlib import Path
import re
import subprocess


PATTERNS = {
    'local-user-path': re.compile(rb'/(?:Users|home)/[A-Za-z0-9._-]{2,}/'),
    'access-secret': re.compile(rb'\b(?:sk-(?:proj-|ant-api\d+-)?[A-Za-z0-9_-]{24,}|gh[pousr]_[A-Za-z0-9]{24,}|AKIA[A-Z0-9]{16}|AIza[0-9A-Za-z_-]{35})\b'),
    'private-data-file': re.compile(rb'(?:^|/)(?:brain\.sqlite(?:-wal|-shm)?|usage\.jsonl|conversations\.json|eval/gold)(?:$|/)'),
}


def git(*args):
    return subprocess.check_output(['git', *args])


def audit(private_terms):
    commits = git('rev-list', '--all').split()
    objects = {}
    for commit in commits:
        for row in git('ls-tree', '-r', '-z', commit.decode()).split(b'\0'):
            if not row:
                continue
            metadata, path = row.split(b'\t', 1)
            _, kind, object_id = metadata.split(b' ')
            if kind == b'blob':
                objects.setdefault(object_id, set()).add(path)
    hits = []
    for object_id, paths in objects.items():
        content = git('cat-file', 'blob', object_id.decode())
        checks = []
        for name, pattern in PATTERNS.items():
            if (any(pattern.search(path) for path in paths) if name == 'private-data-file' else pattern.search(content)):
                checks.append(name)
        if any(term in content or any(term in path for path in paths) for term in private_terms):
            checks.append('private-term')
        if checks:
            hits.append({'blob': object_id.decode(), 'labels': checks})
    if commits and not objects:
        raise ValueError('Unable to enumerate tracked history')
    return {'commits': len(commits), 'blobs': len(objects), 'hits': hits}


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--private-terms', type=Path, help='Local UTF-8 text with one private search term per line')
    args = parser.parse_args()
    terms = args.private_terms.read_text().splitlines() if args.private_terms else []
    report = audit([term.strip().encode('utf-8') for term in terms if term.strip()])
    print(json.dumps(report, ensure_ascii=False))
    raise SystemExit(int(bool(report['hits'])))
