"""Index a configured Obsidian vault without changing the notes."""
from datetime import datetime, timezone
import re
from pathlib import Path


def schema(db):
    db.execute("""CREATE TABLE IF NOT EXISTS vault_notes (
        path TEXT PRIMARY KEY, title TEXT NOT NULL, text TEXT NOT NULL, scanned TEXT NOT NULL
    )""")


def scan(db, root, clean):
    root = root.resolve(strict=True)
    if not root.is_dir() or not (root / '.obsidian').is_dir():
        raise ValueError('Choose an Obsidian vault folder')
    now = datetime.now(timezone.utc).isoformat(timespec='seconds')
    scanned = skipped = 0
    with db:
        for file in sorted(root.rglob('*.md')):
            if file.is_symlink() or any(part.startswith('.') for part in file.relative_to(root).parts):
                continue
            try:
                if not file.resolve().is_relative_to(root) or file.stat().st_size > 1024 * 1024:
                    skipped += 1
                    continue
                text = clean(file.read_text(encoding='utf-8'))
            except (OSError, UnicodeError):
                skipped += 1
                continue
            heading = re.search(r'^#[ \t]+([^\n]+)', text, flags=re.M)
            title = heading[1].strip() if heading else file.stem
            db.execute("""INSERT INTO vault_notes VALUES(?,?,?,?) ON CONFLICT(path)
                       DO UPDATE SET title=excluded.title,text=excluded.text,scanned=excluded.scanned""",
                       (str(file), title[:200], text, now))
            scanned += 1
    return {'scanned': scanned, 'skipped': skipped}


def search(db, query, limit):
    q = query.casefold()
    for row in db.execute("""SELECT * FROM vault_notes
                             WHERE instr(lower(title),?) OR instr(lower(path),?) OR instr(lower(text),?)
                             ORDER BY instr(lower(title),?) DESC, title LIMIT ?""", (q, q, q, q, limit)):
        file = Path(row['path'])
        if not file.is_file() or file.is_symlink():
            continue
        pos = max(0, row['text'].casefold().find(q))
        yield {'source': 'obsidian', 'title': row['title'], 'path': row['path'],
               'text': row['text'][max(0, pos-30):pos+100]}


def project_inputs(db, folder):
    root = Path(folder)
    if not root.is_absolute():
        return []
    return [row for row in db.execute("SELECT path,title,scanned FROM vault_notes ORDER BY scanned DESC,path")
            if Path(row['path']).parent == root
            and Path(row['path']).is_file() and not Path(row['path']).is_symlink()][:5]
