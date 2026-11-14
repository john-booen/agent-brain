import fs from 'node:fs/promises';
import { createReadStream, createWriteStream } from 'node:fs';
import { spawn } from 'node:child_process';
import { homedir } from 'node:os';
import { dirname, join, relative, resolve, sep } from 'node:path';
import { pipeline } from 'node:stream/promises';
import { StringDecoder } from 'node:string_decoder';
import { fileURLToPath } from 'node:url';

const data = resolve(process.env.AGENT_BRAIN_DATA || join(homedir(), 'SharedBrain'));
const program = process.env.AGENTSVIEW_BIN || 'agentsview';
const zstdProgram = process.env.ZSTD_BIN || 'zstd';
const useDaemon = process.env.AGENT_BRAIN_SYNC_DAEMON === '1';
const packetPython = process.env.AGENT_BRAIN_PACKET_PYTHON || (useDaemon && process.platform === 'darwin' ? '/opt/homebrew/bin/python3.12' : '');
const coveragePath = join(data, 'coverage.json');
const todoPath = process.env.AGENT_BRAIN_USER_TODO || join(data, 'USER_TODO.md');
const refreshAlert = '<!-- agent-brain-packet-refresh -->';
const stagingVersion = 3;
const roots = {
  codex: ['.codex/sessions', '.codex/archived_sessions'],
  cowork: ['Library/Application Support/Claude/local-agent-mode-sessions', 'Library/Application Support/Claude-3p/local-agent-mode-sessions'],
  'deepseek-harness': ['.dsh/sessions'],
  'kimi-work': ['Library/Application Support/kimi-desktop/daimon-share/daimon/runtime/kimi-code/home/sessions'],
};
const filenames = {
  codex: /^rollout-.*\.jsonl(?:\.zst)?$/,
  cowork: /^(?:.*\.jsonl|local_.*\.json)$/,
  'deepseek-harness': /^session(?:\.v\d+)?\.jsonl(?:\.zstd)?$/,
  'kimi-work': /^(?:wire\.jsonl|metadata\.json|session\.json|state\.json)$/,
};
const selected = process.argv.slice(2);
if (selected.some(agent => !Object.hasOwn(roots, agent))) {
  throw new Error('Pass source names: ' + Object.keys(roots).join(', '));
}
const sensitiveKey = /(?:apikey|password|passwd|clientsecret|accesstoken|refreshtoken|authtoken|sessionkey|authorization|privatekey|cookie)$/i;
const redactRules = [
  [/-----BEGIN [A-Z ]*PRIVATE KEY-----[\s\S]*?-----END [A-Z ]*PRIVATE KEY-----/g, '[REDACTED]'],
  [/\bBearer\s+[A-Za-z0-9._~+/-]{10,}={0,2}/gi, 'Bearer [REDACTED]'],
  [/(\bhttps?:\/\/[^\s:/@]+:)[^\s/@]+@/gi, '$1[REDACTED]@'],
  [/((?:\b[\w.-]{0,64}(?:api[_ -]?key|client[_ -]?secret|access[_ -]?token|refresh[_ -]?token|auth[_ -]?token)|\b(?:password|passwd|pwd)|密码)[ \t]*["']?[ \t]*[:=][ \t]*)(["'])([^\r\n]*?)\2/giu, '$1$2[REDACTED]$2'],
  [/((?:\b[\w.-]{0,64}(?:api[_ -]?key|client[_ -]?secret|access[_ -]?token|refresh[_ -]?token|auth[_ -]?token)|\b(?:password|passwd|pwd)|密码)[ \t]*[:=][ \t]*)(?!["'\[])([^\s"',;}{<>]{4,})/giu, '$1[REDACTED]'],
  [/\b(?:sk-(?:proj-|ant-api\d+-|svcacct-)?[A-Za-z0-9_-]{15,}|gh[pousr]_[A-Za-z0-9]{20,}|github_pat_[A-Za-z0-9_]{20,}|xox[baprs]-[A-Za-z0-9-]{15,}|AKIA[A-Z0-9]{16}|AIza[0-9A-Za-z_-]{35}|eyJ[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+)\b/g, '[REDACTED]'],
];

function scrub(value) {
  if (typeof value === 'string') return redactRules.reduce((s, [re, replacement]) => s.replace(re, replacement), value);
  if (Array.isArray(value)) return value.map(scrub);
  if (!value || typeof value !== 'object') return value;
  for (const key of Object.keys(value)) {
    if (value[key] && sensitiveKey.test(key.replace(/[-_]/g, ''))) value[key] = '[REDACTED]';
    else value[key] = scrub(value[key]);
  }
  return value;
}

function launch(command, args, options) {
  const child = spawn(command, args, options);
  const exit = new Promise((resolve, reject) => {
    child.once('error', reject);
    child.once('close', resolve);
  });
  return { child, exit };
}

async function* lfLines(input) {
  const decoder = new StringDecoder('utf8');
  let remainder = '';
  for await (const chunk of input) {
    const parts = (remainder + decoder.write(chunk)).split('\n');
    remainder = parts.pop();
    yield* parts;
  }
  const last = remainder + decoder.end();
  if (last) yield last;
}

async function* lines(input, stats) {
  for await (const line of lfLines(input)) {
    if (!line.trim()) continue;
    let row;
    try { row = JSON.parse(line); } catch { stats.skippedLines++; continue; }
    yield JSON.stringify(scrub(row)) + '\n';
  }
}

async function copyTranscript(source, target, stats) {
  const compressed = /\.zst(?:d)?$/.test(source);
  const decoder = compressed && launch(zstdProgram, ['-dc', '--', source], { stdio: ['ignore', 'pipe', 'ignore'] });
  const input = decoder ? decoder.child.stdout : createReadStream(source);
  if (target.endsWith('.zst')) {
    const encoder = launch(zstdProgram, ['-q', '-1', '-c'], { stdio: ['pipe', 'pipe', 'ignore'] });
    const tasks = [
      pipeline(lines(input, stats), encoder.child.stdin),
      pipeline(encoder.child.stdout, createWriteStream(target + '.tmp', { mode: 0o600 })),
      encoder.exit.then(code => { if (code) throw new Error('encoding failed'); }),
    ];
    if (decoder) tasks.push(decoder.exit.then(code => { if (code) throw new Error('decoding failed'); }));
    await Promise.all(tasks);
  } else {
    const tasks = [pipeline(lines(input, stats), createWriteStream(target + '.tmp', { mode: 0o600 }))];
    if (decoder) tasks.push(decoder.exit.then(code => { if (code) throw new Error('decoding failed'); }));
    await Promise.all(tasks);
  }
}

async function writeAtomic(path, value) {
  await fs.writeFile(path + '.tmp', value, { mode: 0o600 });
  await fs.rename(path + '.tmp', path);
}

async function updateRefreshAlert(refresh) {
  const current = await fs.readFile(todoPath, 'utf8').catch(error => {
    if (error.code !== 'ENOENT') throw error;
    return '';
  });
  const content = current.startsWith(refreshAlert + '\n') ? current.split(refreshAlert + '\n', 3).at(-1) : current;
  const warning = refresh.consecutiveFailures >= 3 ?
    `${refreshAlert}\n- **资料袋刷新连续失败 ${refresh.consecutiveFailures} 次。** 项目资料袋可能过期。见本机健康日志。\n${refreshAlert}\n` : '';
  if (warning + content !== current) await writeAtomic(todoPath, warning + content);
}

async function reportRefresh(report, previous, status, error) {
  const refresh = { status, at: new Date().toISOString(),
    consecutiveFailures: status === 'failed' ? (previous?.consecutiveFailures || 0) + 1 : 0 };
  if (error) refresh.error = error;
  report.packetRefresh = refresh;
  await writeAtomic(coveragePath, JSON.stringify(report, null, 2));
  await fs.appendFile(join(data, 'health.log'), JSON.stringify({ event: 'packet_refresh', ...refresh }) + '\n', { mode: 0o600 });
  await updateRefreshAlert(refresh);
}

async function localTask(args) {
  const job = launch(packetPython, ['-B', fileURLToPath(new URL('brain.py', import.meta.url)), ...args],
    { env: { ...process.env, AGENT_BRAIN_DATA: data }, stdio: ['ignore', 'ignore', 'pipe'] });
  let diagnostics = '';
  job.child.stderr.setEncoding('utf8');
  job.child.stderr.on('data', chunk => { diagnostics = (diagnostics + chunk).slice(-4096); });
  const code = await job.exit;
  const reason = diagnostics.match(/\b(?:KeyError|OperationalError|PermissionError|ValueError)(?=:)/)?.[0];
  return code === 0 ? '' : reason || (code === null ? 'interrupted' : 'exit ' + code);
}

async function main() {
  process.umask(0o077);
  await fs.mkdir(data, { recursive: true, mode: 0o700 });
  const repo = new URL('./', import.meta.url);
  const repoPath = await fs.realpath(repo);
  const actualData = await fs.realpath(data);
  const relation = relative(repoPath, actualData);
  if (!relation || (relation !== '..' && !relation.startsWith('..' + sep) && !relation.startsWith(sep))) {
    throw new Error('AGENT_BRAIN_DATA must be outside the code repository');
  }
  const lockPath = join(data, '.collect.lock');
  const lock = await fs.open(lockPath, 'wx', 0o600).catch(async error => {
    if (error.code !== 'EEXIST') throw error;
    const old = await fs.stat(lockPath);
    if (Date.now() - old.mtimeMs < 30 * 60 * 1000) throw error;
    const pid = Number(await fs.readFile(lockPath, 'utf8'));
    if (Number.isSafeInteger(pid) && pid > 0) {
      try { process.kill(pid, 0); throw error; }
      catch (check) { if (check.code !== 'ESRCH') throw check; }
    }
    if ((await fs.stat(lockPath)).ino !== old.ino) throw error;
    await fs.unlink(lockPath);
    return fs.open(lockPath, 'wx', 0o600);
  });
  await lock.writeFile(String(process.pid));
  try { await collect(); }
  catch (error) {
    const prior = await fs.readFile(coveragePath, 'utf8').then(JSON.parse).catch(() => ({}));
    prior.exit = 1;
    prior.completedAt = new Date().toISOString();
    delete prior.updatingSince;
    await writeAtomic(coveragePath, JSON.stringify(prior, null, 2));
    await fs.appendFile(join(data, 'health.log'), JSON.stringify({ event: 'collector', status: 'failed',
      at: prior.completedAt, reason: error.code || error.message }) + '\n', { mode: 0o600 });
    throw error;
  }
  finally { await lock.close(); await fs.unlink(lockPath); }
}

async function collect() {
  const archive = join(data, 'agentsview');
  const manifestPath = join(data, 'sources.json');
  const configPath = join(archive, 'config.toml');
  const sources = {};
  const startedAt = new Date().toISOString();
  const priorReport = await fs.readFile(coveragePath, 'utf8').then(JSON.parse).catch(error => {
    if (error.code !== 'ENOENT') throw error;
    return {};
  });
  const report = { startedAt, sources: { ...priorReport.sources }, packetRefresh: priorReport.packetRefresh,
    weeklyGeneratedOn: priorReport.weeklyGeneratedOn, lastHealthCheck: priorReport.lastHealthCheck };
  const previous = await fs.readFile(manifestPath, 'utf8').then(JSON.parse).catch(error => {
    if (error.code !== 'ENOENT') throw error;
    return {};
  });
  await fs.mkdir(archive, { recursive: true, mode: 0o700 });
  const jobs = [];
  for (const [agent, dirs] of Object.entries(roots)) {
    const active = !selected.length || selected.includes(agent);
    const stats = active ? report.sources[agent] = { found: 0, prepared: 0, unchanged: 0, failed: 0, missing: 0, skippedLines: 0 } : null;
    sources[agent] = [];
    for (const [index, rel] of dirs.entries()) {
      const root = join(homedir(), rel);
      const dest = join(data, 'sources', agent, String(index));
      let entries;
      try {
        if (active) entries = await fs.readdir(root, { recursive: true, withFileTypes: true });
        else await fs.access(dest);
      } catch (error) { if (error.code === 'ENOENT') { if (stats) stats.missing++; continue; } throw error; }
      sources[agent].push(dest);
      if (!active) continue;
      await fs.mkdir(dest, { recursive: true, mode: 0o700 });
      const indexedFiles = agent === 'cowork' && new Set(entries.filter(entry => entry.isFile())
        .map(entry => join(entry.parentPath, entry.name)));
      const indexedDirs = agent === 'cowork' && new Set(entries.filter(entry => entry.isDirectory())
        .map(entry => join(entry.parentPath, entry.name)));
      for (const entry of entries) {
        if (!entry.isFile() || !filenames[agent].test(entry.name)) continue;
        stats.found++;
        const source = join(entry.parentPath, entry.name);
        const filename = relative(root, source).replace(/\.zstd$/, '');
        jobs.push({ source, target: join(dest, filename), stats });
        // Newer Cowork builds store transcripts under a short session folder.
        // AgentsView v0.47 discovers them under the full local session ID.
        const meta = agent === 'cowork' && /^local_([a-f0-9]{8})(?:-[a-f0-9]{4}){3}-[a-f0-9]{12}\.json$/.exec(entry.name);
        if (!meta) continue;
        const fullId = entry.name.slice(0, -5);
        const transcriptDir = join(entry.parentPath, meta[1], '.claude', 'projects', 'session');
        if (!indexedDirs.has(transcriptDir)) continue;
        const { cliSessionId } = await fs.readFile(source, 'utf8').then(JSON.parse).catch(() => ({}));
        if (typeof cliSessionId !== 'string' || !/^[a-f0-9]{8}(?:-[a-f0-9]{4}){3}-[a-f0-9]{12}$/.test(cliSessionId)) continue;
        const transcript = join(transcriptDir, cliSessionId + '.jsonl');
        if (!indexedFiles.has(transcript)) continue;
        const legacyProjects = join(entry.parentPath, fullId, '.claude', 'projects') + sep;
        if ([...indexedFiles].some(path => path.startsWith(legacyProjects) && path.endsWith(sep + cliSessionId + '.jsonl'))) continue;
        const alias = join(dest, relative(root, entry.parentPath), fullId, '.claude', 'projects', 'session', cliSessionId + '.jsonl');
        jobs.push({ source: transcript, target: alias, stats });
      }
    }
  }

  const manifest = { ...previous };
  let position = 0;
  let checkpoint = Promise.resolve();
  async function worker() {
    while (position < jobs.length) {
      const index = position++;
      const { source, target, stats } = jobs[index];
      const key = relative(data, target);
      try {
        const before = await fs.stat(source);
        const fingerprint = [stagingVersion, before.size, before.mtimeMs, before.ino].join(':');
        if (previous[key]?.fingerprint === fingerprint && await fs.stat(target).catch(() => null)) {
          manifest[key] = previous[key];
          stats.unchanged++;
          stats.skippedLines += previous[key].skippedLines || 0;
          continue;
        }
        const fileStats = { skippedLines: 0 };
        await fs.mkdir(dirname(target), { recursive: true, mode: 0o700 });
        if (source.endsWith('.json')) {
          await fs.writeFile(target + '.tmp', JSON.stringify(scrub(JSON.parse(await fs.readFile(source, 'utf8')))), { mode: 0o600 });
        } else {
          await copyTranscript(source, target, fileStats);
        }
        const after = await fs.stat(source);
        if (before.ino !== after.ino || before.size !== after.size || before.mtimeMs !== after.mtimeMs) {
          throw new Error('source changed during collection');
        }
        await fs.rename(target + '.tmp', target);
        manifest[key] = { source, fingerprint, skippedLines: fileStats.skippedLines };
        stats.skippedLines += fileStats.skippedLines;
        stats.prepared++;
      } catch {
        if (previous[key]) manifest[key] = previous[key];
        await fs.rm(target + '.tmp', { force: true });
        stats.failed++;
      }
      if (index % 100 === 0) {
        checkpoint = checkpoint.then(() => writeAtomic(manifestPath, JSON.stringify(manifest)));
        await checkpoint;
      }
    }
  }
  await Promise.all(Array.from({ length: 4 }, worker));
  await writeAtomic(manifestPath, JSON.stringify(manifest));
  const env = { ...process.env, AGENTSVIEW_DATA_DIR: archive, AGENTSVIEW_TELEMETRY_ENABLED: '0', AGENTSVIEW_NO_DAEMON: '1' };
  for (const name of ['AGENTSVIEW_AUTH_TOKEN', 'AGENTSVIEW_AUTH_TOKEN_FILE', 'AGENTSVIEW_PUBLIC_ORIGIN',
    'AGENTSVIEW_MODE', 'AGENTSVIEW_HOST', 'CODEX_SESSIONS_DIR', 'COWORK_DIR', 'KIMI_WORK_DIR',
    'DEEPSEEK_HARNESS_SESSIONS_DIR', 'DSH_HOME']) delete env[name];
  const catalog = launch(program, ['source', 'list', '--json'], { env, stdio: ['ignore', 'pipe', 'inherit'] });
  let output = '';
  for await (const part of catalog.child.stdout) output += part;
  if (await catalog.exit) throw new Error('AgentsView source list failed');
  const disabled = JSON.parse(output).agents.filter(item => !item.import_only && !roots[item.type]).map(item => item.type);
  const previousConfig = await fs.readFile(configPath, 'utf8').catch(error => {
    if (error.code !== 'ENOENT') throw error;
    return '';
  });
  const authConfig = previousConfig.split(/^\s*\[/m, 1)[0].split('\n')
    .filter(line => /^(?:auth_token|cursor_secret)\s*=/.test(line));
  const config = [
    'archive_content = "transcripts"',
    'disable_update_check = true',
    'tool_result_images = "drop"',
    'disabled_agents = ' + JSON.stringify(disabled),
    ...authConfig,
  ];
  for (const [agent, dirs] of Object.entries(sources)) {
    config.push('', '[agents.' + agent + ']', 'dirs = ' + JSON.stringify(dirs));
  }
  const nextConfig = config.join('\n') + '\n';
  if (previousConfig !== nextConfig) await writeAtomic(configPath, nextConfig);
  if (useDaemon) {
    const start = launch(program, ['serve', '--background', '--no-browser', '--no-sync', '--no-update-check',
      '--require-auth', '--host', '127.0.0.1', '--port', '0'], { env, stdio: ['ignore', 'ignore', 'ignore'] });
    if (await start.exit) throw new Error('AgentsView local query service failed to start');
  }
  await writeAtomic(coveragePath, JSON.stringify({ ...priorReport, updatingSince: startedAt }, null, 2));
  const sync = launch(program, ['source', 'sync'], { env, stdio: ['ignore', 'inherit', 'inherit'] });
  report.exit = await sync.exit;
  report.completedAt = new Date().toISOString();
  for (const [agent, item] of Object.entries(report.sources)) {
    if (!selected.length || selected.includes(agent)) item.indexedAt = report.completedAt;
    else item.indexedAt ||= priorReport.completedAt;
  }
  for (const [source, item] of Object.entries(report.sources)) {
    if (item.failed) await fs.appendFile(join(data, 'health.log'), JSON.stringify({ event: 'source_collection',
      source, at: report.completedAt, status: 'partial', failed: item.failed }) + '\n', { mode: 0o600 });
  }
  if (report.exit) {
    for (const item of Object.values(report.sources)) item.syncIncomplete = true;
  }
  await writeAtomic(coveragePath, JSON.stringify(report, null, 2));
  if (report.exit) {
    process.exitCode = 1;
    await fs.appendFile(join(data, 'health.log'), JSON.stringify({ event: 'collector', status: 'failed',
      at: report.completedAt, exit: report.exit }) + '\n', { mode: 0o600 });
  } else if (packetPython) {
    let error;
    try {
      const failure = await localTask(['build-packets', '--files']);
      if (failure) error = `Project packet refresh failed (${failure})`;
    } catch (failure) {
      error = 'Project packet refresh failed to start (' + (failure.code || 'spawn error') + ')';
    }
    if (error) process.exitCode = 1;
    await reportRefresh(report, priorReport.packetRefresh, error ? 'failed' : 'ok', error);
    const day = new Intl.DateTimeFormat('en-CA', { timeZone: 'Asia/Shanghai', dateStyle: 'short' }).format(new Date());
    if (process.env.AGENT_BRAIN_DAILY_HEALTH === '1' && priorReport.lastHealthCheck !== day) {
      let failure;
      try { failure = await localTask(['health', '--backup']); }
      catch (error) { failure = error.code || 'spawn error'; }
      if (!failure) report.lastHealthCheck = day;
      else process.exitCode = 1;
      await fs.appendFile(join(data, 'health.log'), JSON.stringify({ event: 'daily_health', day, status: failure ? 'failed' : 'ok' }) + '\n');
      await writeAtomic(coveragePath, JSON.stringify(report, null, 2));
    }
    if (process.env.AGENT_BRAIN_WEEKLY === '1' && priorReport.weeklyGeneratedOn !== day &&
        new Intl.DateTimeFormat('en', { timeZone: 'Asia/Shanghai', weekday: 'short' }).format(new Date()) === 'Mon') {
      let failure;
      try { failure = await localTask(['weekly-report']); }
      catch (error) { failure = error.code || 'spawn error'; }
      if (!failure) report.weeklyGeneratedOn = day;
      else process.exitCode = 1;
      await fs.appendFile(join(data, 'health.log'), JSON.stringify({ event: 'weekly_report', day, status: failure ? 'failed' : 'ok' }) + '\n');
      await writeAtomic(coveragePath, JSON.stringify(report, null, 2));
    }
  }
  console.log(JSON.stringify(report, null, 2));
}

main().catch(error => { console.error(error.message); process.exitCode = 1; });
