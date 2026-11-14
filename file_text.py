"""Read local project documents without opening the original in an editor."""
import io
import os
import re
import shutil
import stat
import subprocess
from pathlib import Path
from xml.etree import ElementTree as ET
from zipfile import BadZipFile, ZipFile


SUPPORTED = {".docx", ".pdf", ".xlsx", ".md", ".markdown", ".txt", ".csv", ".tsv"}
MAX_FILE = 40 * 1024 * 1024
MAX_XML = 16 * 1024 * 1024
DOCUMENT_KINDS = {
    '.docx': 'Word 文档', '.pdf': 'PDF 文档', '.xlsx': 'Excel 工作簿',
    '.md': 'Markdown 文档', '.markdown': 'Markdown 文档', '.txt': '文本',
    '.csv': '表格数据', '.tsv': '表格数据',
}
W = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"
S = "{http://schemas.openxmlformats.org/spreadsheetml/2006/main}"
R = "{http://schemas.openxmlformats.org/officeDocument/2006/relationships}"
SENSITIVE = re.compile(r"(?:^|[._ -])(?:secrets?|credentials?|passwords?|api[-_ ]?keys?|private[-_ ]?keys?|cookies|tokens?)(?:$|[._ -])", re.I)


def document_path(value):
    path = Path(value)
    if not path.is_absolute() or ".." in path.parts or path.suffix.lower() not in SUPPORTED:
        raise ValueError("需要登记过的绝对文件路径和支持的文件格式")
    canonical = path.resolve(strict=True)
    if canonical != path and not (path.is_relative_to('/var') and canonical == Path('/private') / path.relative_to('/')):
        raise ValueError("登记路径经过符号链接或已改指向")
    # Keep app output and workspaces accessible without exposing their private state.
    blocked = {'.ssh', '.aws', '.gnupg', '.git', '.env', '.config', '.cache', 'keychains', 'login data',
               'profiles', 'local storage', 'session storage', 'credential store', 'local-agent-mode-sessions',
               'archived_sessions', 'sessions', 'logs', 'storages'}
    # Cowork outputs are published into their originating session's output directory.
    app_output = 'local-agent-mode-sessions' in canonical.parts and 'outputs' in canonical.parts
    if app_output:
        blocked.remove('local-agent-mode-sessions')
    if any(part.casefold() in blocked or SENSITIVE.search(part) for part in canonical.parts):
        raise ValueError("该路径属于配置或凭据目录")
    if canonical.is_relative_to(Path.home() / 'SharedBrain'):
        raise ValueError("共享大脑的内部数据文件不能作为交付文件读取")
    return canonical


def read_bytes(path):
    flags = os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK
    directory = os.open(path.anchor, flags | os.O_DIRECTORY)
    try:
        for part in path.parts[1:-1]:
            next_dir = os.open(part, flags | os.O_DIRECTORY, dir_fd=directory)
            os.close(directory)
            directory = next_dir
        fd = os.open(path.name, flags, dir_fd=directory)
    finally:
        os.close(directory)
    with os.fdopen(fd, 'rb') as handle:
        st = os.fstat(handle.fileno())
        if not stat.S_ISREG(st.st_mode) or st.st_size > MAX_FILE:
            raise ValueError("文件不可读取或超过 40 MiB")
        data = handle.read(MAX_FILE + 1)
        after = os.fstat(handle.fileno())
    if len(data) > MAX_FILE or (st.st_size, st.st_mtime_ns) != (after.st_size, after.st_mtime_ns):
        raise ValueError("文件读取期间发生变化，请重新查询")
    return data


def xml(book, name):
    info = book.getinfo(name)
    if info.file_size > MAX_XML:
        raise ValueError("文件内部文档块过大")
    data = book.read(info)
    if re.search(br'<!\s*(?:DOCTYPE|ENTITY)', data, re.I):
        raise ValueError("不支持含 DTD 或实体定义的文档")
    return ET.fromstring(data)


def paragraph_text(p):
    return ''.join(e.text or '' if e.tag == W + 't' else '\t' if e.tag == W + 'tab' else '\n'
                   for e in p.iter() if e.tag in {W + 't', W + 'tab', W + 'br'})


def word_parts(data, page=None):
    with ZipFile(io.BytesIO(data)) as book:
        root = xml(book, 'word/document.xml')
        styles = {}
        if 'word/styles.xml' in book.namelist():
            for style in xml(book, 'word/styles.xml').iter(W + 'style'):
                label = style.find(W + 'name')
                if label is not None:
                    styles[style.get(W + 'styleId')] = label.get(W + 'val', '')
    groups = [('正文', [])]
    for p in root.iter(W + 'p'):
        if page is not None and p.find(f'{W}pPr/{W}pageBreakBefore') is not None:
            groups.append((f'第 {len(groups) + 1} 页', []))
        if page is not None:
            for node in p.iter():
                if (node.tag == W + 'br' and node.get(W + 'type') == 'page') or node.tag == W + 'lastRenderedPageBreak':
                    groups.append((f'第 {len(groups) + 1} 页', []))
                elif node.tag == W + 't' and node.text:
                    groups[-1][1].append(node.text)
            groups[-1][1].append('\n')
            continue
        text = paragraph_text(p).strip()
        if not text:
            continue
        style = p.find(f'{W}pPr/{W}pStyle')
        style_id = style.get(W + 'val', '') if style is not None else ''
        if re.match(r'heading|标题|title', styles.get(style_id, style_id), re.I) or p.find(f'{W}pPr/{W}outlineLvl') is not None:
            groups.append((text, []))
        else:
            groups[-1][1].append(text)
    if page is not None:
        if page > len(groups):
            raise ValueError("Word 文件无可验证的该页分页标记，可按章节读取")
        return [(groups[page - 1][0], ''.join(groups[page - 1][1]).strip())]
    return [(title, '\n'.join(lines)) for title, lines in groups if lines or title != '正文']


def pdf_parts(data, page=None, preview=False):
    program = shutil.which('pdftotext') or next((p for p in ('/opt/homebrew/bin/pdftotext', '/usr/local/bin/pdftotext') if Path(p).is_file()), None)
    if not program:
        raise ValueError("PDF 提取需要本机 pdftotext")
    # Limit background extraction to representative opening pages. Explicit pages are exact.
    first = page or 1
    selection = ['-f', str(first), '-l', str(page or 8)] if page is not None or preview else []
    run = subprocess.run([program, *selection, '-layout', '-enc', 'UTF-8', '-', '-'],
                         input=data, capture_output=True, timeout=15, check=False)
    if run.returncode:
        raise ValueError("PDF 无法提取，可能加密、损坏或超出页数")
    parts = [(f'第 {n} 页', text.strip()) for n, text in enumerate(run.stdout.decode('utf-8', 'replace').split('\f'), first) if text.strip()]
    if parts or not (page is not None or preview):
        return parts
    renderer = shutil.which('pdftoppm') or '/opt/homebrew/bin/pdftoppm'
    ocr = shutil.which('tesseract') or '/opt/homebrew/bin/tesseract'
    if not Path(renderer).is_file() or not Path(ocr).is_file():
        return []
    raster = subprocess.run([renderer, '-f', str(first), '-l', str(first), '-scale-to', '1600', '-singlefile', '-png', '-'],
                            input=data, capture_output=True, timeout=15, check=False)
    if raster.returncode or not raster.stdout.startswith(b'\x89PNG'):
        return []
    result = subprocess.run([ocr, 'stdin', 'stdout', '-l', 'chi_sim+eng'], input=raster.stdout,
                            capture_output=True, timeout=15, check=False)
    text = result.stdout.decode('utf-8', 'replace').strip()
    return [(f'第 {first} 页（OCR）', text)] if result.returncode == 0 and text else []


def sheet_parts(data, section=None, preview=False):
    with ZipFile(io.BytesIO(data)) as book:
        wb = xml(book, 'xl/workbook.xml')
        links = {e.get('Id'): e.get('Target', '') for e in xml(book, 'xl/_rels/workbook.xml.rels')
                 if e.get('TargetMode') != 'External'}
        shared = []
        if 'xl/sharedStrings.xml' in book.namelist():
            shared = [''.join(t.text or '' for t in si.iter(S + 't')) for si in xml(book, 'xl/sharedStrings.xml')]
        sheets = list(wb.iter(S + 'sheet'))
        if section:
            matches = [s for s in sheets if section.casefold() in s.get('name', '').casefold()]
            if not matches:
                raise ValueError('没有匹配的工作表')
            sheets = matches
        for sheet in sheets[:8] if preview else sheets:
            location = links.get(sheet.get(R + 'id'), '')
            name = location.lstrip('/') if location.startswith('/') else 'xl/' + location
            if not name.startswith('xl/worksheets/') or '..' in Path(name).parts:
                continue
            lines = []
            for row in xml(book, name).iter(S + 'row'):
                cells = []
                for cell in row.iter(S + 'c'):
                    value = cell.findtext(S + 'v', '')
                    formula = cell.findtext(S + 'f', '')
                    if cell.get('t') == 's' and value.isdigit() and int(value) < len(shared):
                        value = shared[int(value)]
                    elif cell.get('t') == 'inlineStr':
                        value = ''.join(t.text or '' for t in cell.iter(S + 't'))
                    if value or formula:
                        cells.append(f"{cell.get('r', '')} {value}" + (f' [公式 ={formula}]' if formula else ''))
                if cells:
                    lines.append(' | '.join(cells))
                    if preview and len(lines) >= 4:
                        break
            yield sheet.get('name', '工作表'), '\n'.join(lines)


def text_parts(data):
    try:
        text = data.decode('utf-8-sig')
    except UnicodeDecodeError:
        text = data.decode('gb18030')
    groups = [('正文', [])]
    for line in text.splitlines():
        heading = re.match(r'^#{1,6}\s+(.+?)\s*#*$', line)
        if heading:
            groups.append((heading[1], []))
        else:
            groups[-1][1].append(line)
    return [(title, '\n'.join(lines).strip()) for title, lines in groups if lines or title != '正文']


def extract(path, data, section=None, page=None, preview=False):
    try:
        suffix = path.suffix.lower()
        if suffix == '.docx':
            parts = word_parts(data, page)
        elif suffix == '.pdf':
            parts = pdf_parts(data, page, preview=preview)
        elif suffix == '.xlsx':
            if page is not None:
                raise ValueError('Excel 按工作表读取，请使用 section')
            parts = list(sheet_parts(data, section, preview))
        else:
            if page is not None:
                raise ValueError('文本文件按章节读取，请使用 section')
            parts = text_parts(data)
    except (BadZipFile, ET.ParseError, KeyError, UnicodeError, subprocess.TimeoutExpired) as exc:
        raise ValueError('文件格式不完整、编码未知或提取超时') from exc
    if section and suffix != '.xlsx':
        def score(part):
            query = re.sub(r'\s+', '', section).casefold()
            title = re.sub(r'\s+', '', part[0]).casefold()
            body = re.sub(r'\s+', '', part[1]).casefold()
            return 3 if query == title else 2 if query in title else 1 if query in body else 0
        best = max(map(score, parts), default=0)
        if not best:
            raise ValueError('没有匹配的章节，试用文件里的标题或更短的关键词')
        selected = [part for part in parts if score(part) == best]
        if best == 1:
            # A plain document or PDF page may have no marked heading. Start at the matching text.
            words = re.compile(r'\s*'.join(map(re.escape, re.sub(r'\s+', '', section))), re.I)
            narrowed = []
            for title, body in selected:
                match = words.search(body)
                at = max(0, match.start() - 150) if match else 0
                narrowed.append((title, body[at:at + 5000]))
            selected = narrowed
        parts = selected
    return parts


def summary_from_parts(path, parts):
    if not parts or not any(title != '正文' or text.strip() for title, text in parts):
        return ''
    suffix = path.suffix.lower()
    kind = DOCUMENT_KINDS[suffix]
    labels = []
    for title, _ in parts:
        title = ' '.join(title.split()).strip()
        if title and title not in labels and title != '正文' and not re.fullmatch(r'第 \d+ 页(?:（OCR）)?', title):
            labels.append(title)
    entries = []
    for title, text in parts:
        if suffix == '.xlsx' or not re.search(r'摘要|结论|要点|概要|\b(?:abstract|summary|conclusions?)\b', title, re.I):
            continue
        sentences = re.split(r'(?<=[。！？])\s*|(?<=[.!?])\s+|\r?\n', text)
        entries.extend(' '.join(sentence.split()).strip('|#- ') for sentence in sentences)
    finding = next((entry for entry in entries if 10 <= len(entry) <= 85), '')
    brief = kind + ('，要点：' + finding if finding else '')
    prefix = '，工作表：' if suffix == '.xlsx' else '，章节：'
    headings = []
    for label in labels:
        if len(brief + prefix + '、'.join([*headings, label])) <= 180:
            headings.append(label)
        if len(headings) >= 4:
            break
    if headings:
        return brief + prefix + '、'.join(headings)
    pages = [title for title, text in parts if text.strip() and re.fullmatch(r'第 \d+ 页(?:（OCR）)?', title)]
    if pages:
        return brief + '，可按页核对 ' + '、'.join(pages[:4])
    return brief
