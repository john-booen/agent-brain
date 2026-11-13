import fs from 'node:fs/promises';
import { createReadStream, createWriteStream } from 'node:fs';
import { spawn } from 'node:child_process';
import { homedir } from 'node:os';
import { dirname, join, relative, resolve, sep } from 'node:path';
import { pipeline } from 'node:stream/promises';
import { StringDecoder } from 'node:string_decoder';

const data = resolve(process.env.AGENT_BRAIN_DATA || join(homedir(), 'SharedBrain'));
const program = process.env.AGENTSVIEW_BIN || 'agentsview';
const zstdProgram = process.env.ZSTD_BIN || 'zstd';
const useDaemon = process.env.AGENT_BRAIN_SYNC_DAEMON === '1';
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
  const lock = await fs.open(join(data, '.collect.lock'), 'wx', 0o600);
  await lock.writeFile(String(process.pid));
  try { await collect(); }
  finally { await lock.close(); await fs.unlink(join(data, '.collect.lock')); }
}

async function collect() {
  const archive = join(data, 'agentsview');
  const manifestPath = join(data, 'sources.json');
  const configPath = join(archive, 'config.toml');
  const sources = {};
  const startedAt = new Date().toISOString();
  const priorReport = await fs.readFile(join(data, 'coverage.json'), 'utf8').then(JSON.parse).catch(error => {
    if (error.code !== 'ENOENT') throw error;
    return {};
  });
  const report = { startedAt, sources: { ...priorReport.sources } };
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
  await writeAtomic(join(data, 'coverage.json'), JSON.stringify({ ...priorReport, updatingSince: startedAt }, null, 2));
  const sync = launch(program, ['source', 'sync'], { env, stdio: ['ignore', 'inherit', 'inherit'] });
  report.exit = await sync.exit;
  report.completedAt = new Date().toISOString();
  for (const [agent, item] of Object.entries(report.sources)) {
    if (!selected.length || selected.includes(agent)) item.indexedAt = report.completedAt;
    else item.indexedAt ||= priorReport.completedAt;
  }
  if (report.exit) {
    for (const item of Object.values(report.sources)) item.syncIncomplete = true;
  }
  await writeAtomic(join(data, 'coverage.json'), JSON.stringify(report, null, 2));
  console.log(JSON.stringify(report, null, 2));
  if (report.exit) process.exitCode = 1;
}

main().catch(error => { console.error(error.message); process.exitCode = 1; });
