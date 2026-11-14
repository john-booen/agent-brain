"""Score project-packet predictions against locally curated message anchors."""
import argparse
from collections import defaultdict
import json
from pathlib import Path
from tempfile import TemporaryDirectory

import brain


FIELDS = ('inputs', 'deliverables', 'rules', 'todos', 'attribution')


def anchor(item):
    path = item.get('path')
    return (item['session'], item['ordinal'], Path(path).name if path else None)


def prediction(db, case):
    project = case['id']
    data, _ = brain.packet.load(db, brain.DATA, project, brain.visible, brain.clean, lazy=True)
    result = {}
    for field in FIELDS[:4]:
        key = 'model_todos' if field == 'todos' else field
        items = data.get(key, [])
        if field == 'deliverables':
            items = [item for item in items if item.get('pinned') or item.get('final')]
        result[field] = [{'session': item['ref']['session_id'], 'ordinal': item['ref']['ordinal'],
                          'path': item.get('original_path', item.get('path'))} for item in items]
    result['attribution'] = [{'session': item['ref']['session_id'], 'ordinal': item['ref']['ordinal']}
                             for item in data.get('model_outcomes', [])]
    with_model = set(data.get('model_sessions', ()))
    for row in brain.packet.work_rows(db, project):
        sid = row['session_id']
        ordinal = json.loads(row['data']).get('ordinal')
        if sid is not None and sid not in with_model and ordinal is not None:
            result['attribution'].append({'session': sid, 'ordinal': ordinal})
    return result


def score(cases, predict, measure):
    totals = defaultdict(lambda: [0, 0, 0, 0])
    rows = []
    for case in cases:
        if case.get('status') not in {'codex_reviewed', 'user_verified', 'fictional'}:
            continue
        predicted = predict(case)
        row = {'id': case['id'], 'status': case['status'], **measure(case), 'counts': {}}
        windows = set(case.get('sessions', ()))
        for key in FIELDS:
            expected = {anchor(item) for item in case[key]}
            actual = {anchor(item) for item in predicted.get(key, ()) if not windows or item['session'] in windows}
            if key == 'attribution':
                # Attribution is a session-level decision. The ordinal is retained for manual review.
                expected = {item[0] for item in expected}
                actual = {item[0] for item in actual}
            counts = [len(expected & actual), len(actual - expected), len(expected - actual), int(expected == actual)]
            row['counts'][key] = counts
            totals[key] = [a + b for a, b in zip(totals[key], counts)]
        rows.append(row)
    report = {'scored_projects': len(rows), 'sample_projects': len(cases), 'user_verified_projects':
              sum(row['status'] == 'user_verified' for row in rows), 'metrics': {}, 'projects': rows}
    for key in FIELDS:
        tp, fp, fn, exact = totals[key]
        report['metrics'][key] = {'precision': tp / (tp + fp) if tp + fp else None,
                                  'recall': tp / (tp + fn) if tp + fn else None,
                                  'f1': 2 * tp / (2 * tp + fp + fn) if tp + fp + fn else None,
                                  'exact_project_accuracy': exact / len(rows) if rows else None,
                                  'tp': tp, 'fp': fp, 'fn': fn}
    return report


def measure_card(db, case, tokenizer=None):
    card = brain.json_text(brain.get_project(db, case['id']))
    return {'utf8_bytes': len(card.encode()), 'tokens': tokenizer(card) if tokenizer else None}


def fictional(fixture):
    with TemporaryDirectory(prefix='agent-brain-eval-') as temporary:
        brain.DATA = Path(temporary).resolve() / 'data'
        with brain.connect() as db:
            path = str(Path(temporary).resolve() / 'fictional-deliverable.md')
            case = fixture['case']
            sid = case['sessions'][0]
            project = brain.log_work(db, **fixture['log'], create_project=True, session_id=sid,
                                     deliverables=[{'task': 'Checklist', 'path': path, 'ordinal': 2, 'final': True}])
            case['id'] = project['project_id']
            case['deliverables'][0]['path'] = path
            report = score([case], lambda item: prediction(db, item), lambda item: measure_card(db, item))
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('gold', type=Path, nargs='?')
    parser.add_argument('--fictional', action='store_true')
    parser.add_argument('--tokenizer', metavar='ENCODING', help='Use an installed and locally cached tiktoken encoding')
    parser.add_argument('--output', type=Path)
    parser.add_argument('--markdown', type=Path)
    args = parser.parse_args()
    if args.fictional:
        fixture = json.loads((brain.REPO / 'examples/eval.fictional.json').read_text())
        report = fictional(fixture)
        if report['metrics']['deliverables']['exact_project_accuracy'] != 1:
            raise SystemExit('Fictional evaluation failed')
    else:
        if not args.gold:
            parser.error('provide the private gold file or --fictional')
        cases = json.loads(args.gold.read_text())['projects']
        tokenizer = None
        if args.tokenizer:
            if args.tokenizer == 'o200k_base':
                tokenizer = brain.usage.token_counter(brain.DATA)
            if not tokenizer:
                parser.error('install the local tokenizer cache before requesting token counts')
        with brain.connect() as db:
            report = score(cases, lambda item: prediction(db, item), lambda item: measure_card(db, item, tokenizer))
    body = json.dumps(report, ensure_ascii=False, indent=2)
    if args.output:
        args.output.write_text(body + '\n')
    else:
        print(body)
    if args.markdown:
        lines = ['# Project packet evaluation', '',
                 f"Scored {report['scored_projects']} of {report['sample_projects']} projects. "
                 f"User-verified projects: {report['user_verified_projects']}.", '',
                 '| Field | Precision | Recall | F1 | Exact project accuracy |',
                 '|---|---:|---:|---:|---:|']
        for key, measured in report['metrics'].items():
            values = [measured[name] for name in ('precision', 'recall', 'f1', 'exact_project_accuracy')]
            display = ['—' if value is None else f'{value:.1%}' for value in values]
            lines.append('| ' + ' | '.join([key, *display]) + ' |')
        lines.extend(['', '| Project ID | Status | Tokens | UTF-8 bytes |', '|---|---|---:|---:|'])
        for row in report['projects']:
            tokens = row['tokens'] if row['tokens'] is not None else '—'
            lines.append(f"| {row['id']} | {row['status']} | {tokens} | {row['utf8_bytes']} |")
        args.markdown.write_text('\n'.join(lines) + '\n')


if __name__ == '__main__':
    main()
