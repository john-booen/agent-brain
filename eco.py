"""Shared credit reservations for the configured OpenDesign Eco route."""
from datetime import datetime, timedelta, timezone
from urllib.parse import urlsplit


MODEL = 'opendesign/deepseek-v4.1-flash-eco'
MAX_OUTPUT = 7500
RECALL_MAX_OUTPUT = 16000
DAY_LIMIT = 5000
WEEK_LIMIT = 30000


class BudgetUnavailable(RuntimeError):
    pass


class OutputTruncated(ValueError):
    pass


def complete_usage(bill):
    input_tokens = bill.get('prompt_tokens')
    output_tokens = bill.get('completion_tokens')
    return (type(input_tokens) is int and input_tokens > 0
            and type(output_tokens) is int and output_tokens >= 0)


def schema(db):
    db.executescript("""
        CREATE TABLE IF NOT EXISTS eco_requests(
          id INTEGER PRIMARY KEY, at TEXT NOT NULL, purpose TEXT NOT NULL,
          charge INTEGER NOT NULL, reserved INTEGER NOT NULL, status TEXT NOT NULL,
          input_tokens INTEGER, output_tokens INTEGER, reasoning_tokens INTEGER
        );
        CREATE INDEX IF NOT EXISTS eco_requests_at ON eco_requests(at);
    """)


def credit_bound(input_tokens, output_tokens):
    # 10,000 plan points per credit-dollar. Count every input token at $0.30/M
    # and every output token at $1.20/M, without applying Eco's 0.27 multiplier.
    return max(1, (3 * input_tokens + 12 * output_tokens + 999) // 1000)


def limits(settings):
    day = settings.get('daily_credits', DAY_LIMIT)
    week = settings.get('weekly_credits', WEEK_LIMIT)
    if type(day) is not int or type(week) is not int or day <= 0 or week <= 0:
        raise BudgetUnavailable('Eco 积分预算必须为正整数')
    return day, week


def snapshot(db, settings, instant=None):
    instant = instant or datetime.now(timezone.utc)
    day, week = limits(settings)
    windows = {}
    for label, duration, cap in (('24h', 1, day), ('7d', 7, week)):
        used, held = db.execute("""SELECT COALESCE(SUM(charge),0),
                   COALESCE(SUM(CASE WHEN input_tokens IS NULL OR input_tokens<=0 OR output_tokens IS NULL OR output_tokens<0
                                     THEN charge ELSE 0 END),0)
                   FROM eco_requests WHERE at>=?""", ((instant-timedelta(days=duration)).isoformat(),)).fetchone()
        windows[label] = {'used_or_reserved': used, 'unmeasured_or_held': held, 'limit': cap,
                          'warning': used * 5 >= cap * 4}
    return {'unit': '积分', 'meter': '公开价未折扣上界，非服务商实扣', **windows}


def reserve(db, settings, purpose, amount, *, enforce_budget=True):
    instant = datetime.now(timezone.utc)
    db.execute('BEGIN IMMEDIATE')
    try:
        if enforce_budget:
            state = snapshot(db, settings, instant)
            if any(state[w]['used_or_reserved'] + amount > state[w]['limit'] for w in ('24h', '7d')):
                raise BudgetUnavailable('Eco 积分预算不足')
        recent = db.execute("SELECT max(at) FROM eco_requests WHERE status='rate_limited'").fetchone()[0]
        if recent and instant - datetime.fromisoformat(recent) < timedelta(minutes=15):
            raise BudgetUnavailable('Eco 限流冷却中')
        cursor = db.execute("INSERT INTO eco_requests(at,purpose,charge,reserved,status) VALUES(?,?,?,?,?)",
                            (instant.isoformat(), purpose, amount, amount, 'started'))
        db.commit()
        return cursor.lastrowid
    except BaseException:
        db.rollback()
        raise


def call(db, settings, messages, purpose, send, encode, *, recall=False, enforce_budget=True):
    url = settings.get('url', '')
    parsed = urlsplit(url)
    if (settings.get('enabled') is not True or settings.get('name') != MODEL or parsed.scheme != 'http'
            or parsed.hostname != '127.0.0.1' or parsed.username or parsed.password or parsed.query
            or parsed.fragment or parsed.path.rstrip('/') != '/v1' or not parsed.port):
        raise BudgetUnavailable('Eco 路由未配置或未启用')
    # UTF-8 bytes cover byte-based tokenizer input plus conservative protocol overhead.
    output_limit = RECALL_MAX_OUTPUT if recall else MAX_OUTPUT
    held = credit_bound(len(encode(messages).encode()) + 1024, output_limit)
    request = reserve(db, settings, purpose, held, enforce_budget=enforce_budget)
    bill = {}
    status = 'failed'
    try:
        result = send(messages, settings={
            'url': url, 'name': MODEL, 'max_tokens': output_limit, 'timeout': 45,
            'headers': {'Authorization': 'Bearer magpie-agent-brain', 'User-Agent': 'agent-brain/1.0'},
            'extra_body': {'stream': True, 'stream_options': {'include_usage': True},
                           **({'reasoning_effort':'low'} if recall else {'thinking':{'type':'disabled'}}),
                           'response_format': {'type': 'json_object'}},
        }, local_only=True, on_usage=bill.update)
        status = 'ok'
        return result
    except RuntimeError as exc:
        if type(exc).__name__ == 'ModelQuotaError':
            status = 'rate_limited'
        raise
    except OutputTruncated:
        status = 'truncated'
        raise
    finally:
        input_tokens = bill.get('prompt_tokens')
        output_tokens = bill.get('completion_tokens')
        details = bill.get('completion_tokens_details') or {}
        reasoning = details.get('reasoning_tokens') if isinstance(details, dict) else None
        measured = complete_usage(bill)
        charge = credit_bound(input_tokens, output_tokens) if measured else held
        if reasoning is not None and (type(reasoning) is not int or reasoning < 0):
            reasoning = None
        with db:
            db.execute("""UPDATE eco_requests SET charge=?,status=?,input_tokens=?,output_tokens=?,reasoning_tokens=? WHERE id=?""",
                       (charge, status if measured or status == 'rate_limited' else 'unmeasured',
                        input_tokens if type(input_tokens) is int else None,
                        output_tokens if type(output_tokens) is int else None,
                        reasoning, request))
