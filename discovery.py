"""Discover local working presentations inside explicitly configured folders."""
from collections import defaultdict
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
from time import monotonic

import file_text


def schema(db):
    db.executescript("""
        CREATE TABLE IF NOT EXISTS local_file_versions(
            path TEXT NOT NULL, sha256 TEXT NOT NULL, pages TEXT NOT NULL, observed TEXT NOT NULL,
            PRIMARY KEY(path,sha256)
        );
        CREATE TABLE IF NOT EXISTS local_file_observations(
            path TEXT PRIMARY KEY, fingerprint TEXT NOT NULL, sha256 TEXT,
            modified TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS local_file_candidates(
            path TEXT PRIMARY KEY, sha256 TEXT NOT NULL, project_id TEXT,
            evidence TEXT NOT NULL, matched_pages INTEGER NOT NULL
        );
    """)


def directories(data):
    path = data / 'config.json'
    settings = json.loads(path.read_text()) if path.is_file() else {}
    values = settings.get('file_discovery', {}).get('directories', [])
    if not isinstance(values, list) or any(not isinstance(value, str) or not Path(value).is_absolute()
                                           for value in values):
        raise ValueError('File discovery needs explicitly configured absolute directories')
    return [Path(value) for value in values]


def fingerprint(path):
    st = path.stat()
    if not file_text.local_file_available(st):
        raise ValueError('Local file is unavailable')
    return f'{st.st_dev}:{st.st_ino}:{st.st_size}:{st.st_mtime_ns}:{st.st_ctime_ns}', st.st_mtime


def features(db, path, observed, wait_for_save=True):
    before, modified = fingerprint(path)
    row = db.execute('SELECT fingerprint,sha256 FROM local_file_observations WHERE path=?',
                     (str(path),)).fetchone()
    if row and row[0] == before and row[1]:
        stored = db.execute('SELECT pages FROM local_file_versions WHERE path=? AND sha256=?',
                            (str(path), row[1])).fetchone()
        if stored:
            return row[1], json.loads(stored[0])
    # Wait for a second observation of the same saved file before opening it.
    if not row or row[0] != before:
        with db:
            db.execute('INSERT OR REPLACE INTO local_file_observations VALUES(?,?,NULL,?)',
                       (str(path), before, datetime.fromtimestamp(modified, timezone.utc).isoformat()))
        if wait_for_save:
            return None
    body = file_text.read_bytes(file_text.document_path(str(path)))
    pages = {hashlib.sha256(' '.join(text.split()).encode()).hexdigest()
             for _, text in file_text.extract(path, body) if len(''.join(text.split())) >= 80}
    after, _ = fingerprint(path)
    if before != after:
        return None
    digest = hashlib.sha256(body).hexdigest()
    with db:
        db.execute('INSERT OR IGNORE INTO local_file_versions VALUES(?,?,?,?)',
                   (str(path), digest, json.dumps(sorted(pages)), observed))
        db.execute('UPDATE local_file_observations SET sha256=? WHERE path=? AND fingerprint=?',
                   (digest, str(path), before))
    return digest, sorted(pages)


def scoped_path(path, roots, data):
    if path.is_symlink() or path.suffix.casefold() != '.pptx':
        return None
    resolved = path.resolve()
    if resolved.is_relative_to(data.resolve()) or resolved.is_relative_to(Path(__file__).resolve().parent):
        return None
    if any(resolved.is_relative_to(root.resolve()) for root in roots if root.is_dir() and not root.is_symlink()):
        return resolved
    return None


def refresh(db, data, sources, expires_at):
    roots = directories(data)
    report = {'directories': len(roots), 'observed': 0, 'matched': 0, 'ambiguous': 0,
              'waiting': 0, 'unreadable': 0, 'deferred': 0}
    if not roots:
        return report
    observed = datetime.now(timezone.utc).isoformat(timespec='seconds')
    source_pages = defaultdict(set)
    source_refs = defaultdict(list)
    registered = {str(Path(path).resolve()) for path in sources}
    complete = True
    for value, projects in sources.items():
        if monotonic() >= expires_at:
            complete = False
            break
        try:
            result = features(db, Path(value), observed, wait_for_save=False)
        except (OSError, ValueError):
            report['unreadable'] += 1
            continue
        if not result:
            complete = False
            continue
        digest, pages = result
        for page in pages:
            source_pages[page].update(projects)
            source_refs[page].append({'path': value, 'sha256': digest})
    if not complete:
        report['deferred'] += 1
        return report
    for root in roots:
        if not root.is_dir() or root.is_symlink():
            report['unreadable'] += 1
            continue
        for folder, children, names in os.walk(root, followlinks=False):
            if monotonic() >= expires_at:
                report['deferred'] += 1
                return report
            children[:] = sorted(name for name in children if not name.startswith('.')
                                  and not (Path(folder) / name).is_symlink()
                                  and not (Path(folder) / name).resolve().is_relative_to(data.resolve()))
            for name in sorted(names):
                if monotonic() >= expires_at:
                    report['deferred'] += 1
                    return report
                if name.startswith(('.', '~$')) or not name.casefold().endswith('.pptx'):
                    continue
                file = Path(folder) / name
                try:
                    path = scoped_path(file, roots, data)
                    if path is None or str(path) in registered:
                        continue
                    report['observed'] += 1
                    result = features(db, path, observed)
                    if not result:
                        report['waiting'] += 1
                        continue
                    digest, pages = result
                    matches = defaultdict(set)
                    for page in pages:
                        projects = source_pages.get(page, ())
                        if len(projects) == 1:
                            matches[next(iter(projects))].add(page)
                    project = None
                    references = {}
                    count = 0
                    if len(matches) == 1:
                        choice, found = next(iter(matches.items()))
                        if len(found) >= 2:
                            project, count = choice, len(found)
                            for page in found:
                                for ref in source_refs[page]:
                                    references[ref['path']] = ref
                    if project:
                        report['matched'] += 1
                    elif matches or any(page in source_pages for page in pages):
                        report['ambiguous'] += 1
                    with db:
                        db.execute('INSERT OR REPLACE INTO local_file_candidates VALUES(?,?,?,?,?)',
                                   (str(path), digest, project, json.dumps(list(references.values())), count))
                except (OSError, ValueError):
                    report['unreadable'] += 1
    return report


def project_entries(db, data, project):
    if not db.execute("SELECT 1 FROM sqlite_master WHERE name='local_file_candidates'").fetchone():
        return []
    roots = directories(data)
    result = []
    for value, digest, count, saved, expected in db.execute("""SELECT c.path,c.sha256,c.matched_pages,
            o.modified,o.fingerprint FROM local_file_candidates c
            JOIN local_file_observations o ON o.path=c.path AND o.sha256=c.sha256
            WHERE c.project_id=? ORDER BY o.modified DESC,c.path""", (project,)):
        try:
            path = scoped_path(Path(value), roots, data)
            if path is None or fingerprint(path)[0] != expected:
                continue
        except (OSError, ValueError):
            continue
        result.append({'path': value, 'ref': {'source': 'local-file', 'session_id': 'file:' + digest,
                       'ordinal': None, 'time': saved}, 'working_copy': True,
                       'description': f'与已登记源稿的 {count} 页一致，实际采用待核'})
    return result
