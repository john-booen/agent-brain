"""Install local MCP entries and skills without changing conversation archives."""
import argparse
from datetime import datetime
import json
from pathlib import Path
import shutil
import shlex
import subprocess
import sys
import tomllib


ROOT = Path(__file__).resolve().parent
COMMAND = str(Path(sys.executable).resolve())
ARGS = [str(ROOT / 'brain.py'), 'mcp']


def write(path, text):
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    if path.exists():
        if path.read_text() == text:
            return
        stamp = datetime.now().strftime('%Y%m%d-%H%M%S-%f')
        shutil.copy2(path, path.with_name(f'{path.name}.before-agent-brain-{stamp}'))
    temp = path.with_name(path.name + '.agent-brain.tmp')
    temp.write_text(text)
    temp.chmod(path.stat().st_mode & 0o777 if path.exists() else 0o600)
    temp.replace(path)


def install_json(path, source, data):
    current = json.loads(path.read_text()) if path.exists() else {}
    servers = current.setdefault('mcpServers', {})
    entry = servers.setdefault('agent-brain', {})
    entry.update(command=COMMAND, args=ARGS)
    entry.setdefault('env', {}).update(AGENT_BRAIN_SOURCE=source, AGENT_BRAIN_DATA=data)
    write(path, json.dumps(current, ensure_ascii=False, indent=2) + '\n')


def install_codex(path, data):
    text = path.read_text() if path.exists() else ''
    config = tomllib.loads(text)
    known = config.get('mcp_servers', {})
    name = 'agent_brain' if 'agent_brain' in known else 'agent-brain'
    key = f'mcp_servers.{name}'
    if name not in known:
        text += f'\n[{key}]\ncommand = {json.dumps(COMMAND)}\nargs = {json.dumps(ARGS, ensure_ascii=False)}\n'
    environment = known.get(name, {}).get('env', {})
    if (environment.get('AGENT_BRAIN_SOURCE'), environment.get('AGENT_BRAIN_DATA')) != ('codex', data):
        if environment:
            raise ValueError(f'Existing env in {path}. Add AGENT_BRAIN_SOURCE and AGENT_BRAIN_DATA under [{key}.env].')
        text += f'\n[{key}.env]\nAGENT_BRAIN_SOURCE = "codex"\nAGENT_BRAIN_DATA = {json.dumps(data, ensure_ascii=False)}\n'
    tomllib.loads(text)
    write(path, text)


def install_kimi(home, data, command):
    plugin = home / '.local/share/agent-brain/kimi-plugin'
    spec = {'$schema': 'https://www.kimi.com/schemas/plugin.json', 'name': 'agent-brain',
            'version': '0.1.0', 'description': 'Cross-client local project memory', 'author': 'agent-brain contributors',
            'interface': {'displayName': 'Agent Brain', 'shortDescription': 'Local shared project memory',
                          'longDescription': 'Read project packets and record handoffs.', 'developerName': 'agent-brain contributors',
                          'category': 'productivity', 'defaultLocale': 'en-US', 'hostKind': 'local',
                          'platforms': ['macos']},
            'skills': './skills/', 'mcpServers': {'agent-brain': {
                'command': 'sh', 'args': ['./bin/agent-brain-mcp'], 'cwd': './'}}}
    write(plugin / 'kimi.plugin.json', json.dumps(spec, ensure_ascii=False, indent=2) + '\n')
    write(plugin / 'bin/agent-brain-mcp', '#!/bin/sh\nexport AGENT_BRAIN_SOURCE=kimi-work\nexport AGENT_BRAIN_DATA='
          + shlex.quote(data) + '\nexec ' + shlex.join([command['command'], *command['args']]) + '\n')
    write(plugin / 'skills/agent-brain/SKILL.md', (ROOT / 'skills/agent-brain/SKILL.md').read_text())
    print(f'Kimi plugin folder: {plugin}')


def self_check(data):
    env = {'PATH': '/usr/local/bin:/usr/bin:/bin', 'AGENT_BRAIN_DATA': data, 'AGENT_BRAIN_USAGE_ORIGIN': 'test'}
    result = subprocess.run([COMMAND, '-B', *ARGS], text=True, input='{"jsonrpc":"2.0","id":1,"method":"tools/list"}\n',
                            env=env, stdout=subprocess.PIPE, check=True, timeout=20)
    from brain import TOOLS
    if {tool['name'] for tool in json.loads(result.stdout)['result']['tools']} != set(TOOLS):
        raise ValueError('The local MCP server did not advertise the expected tools')
    print('Local MCP self-check passed.')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--home', type=Path, default=Path.home())
    parser.add_argument('--data', type=Path)
    parser.add_argument('--client', action='append', choices=['codex','cowork','kimi-work','deepseek-harness','antigravity'])
    parser.add_argument('--demo', action='store_true')
    args = parser.parse_args()
    home = args.home.expanduser().resolve()
    data = str((args.data or home / 'SharedBrain').expanduser().resolve())
    if Path(data).is_relative_to(ROOT):
        parser.error('choose a data directory outside the code repository')
    claude = home / 'Library/Application Support'
    auto = {
        'codex': (home / '.codex').is_dir(),
        'cowork': any((claude / name).is_dir() for name in ('Claude-3p', 'Claude')),
        'kimi-work': (claude / 'kimi-desktop').is_dir(),
        'deepseek-harness': (home / '.dsh').is_dir(),
        'antigravity': (home / '.gemini').is_dir(),
    }
    skill_path = ROOT / 'skills/agent-brain/SKILL.md'
    for source in args.client or [key for key, found in auto.items() if found]:
        command = {'command': COMMAND, 'args': ARGS, 'env': {'AGENT_BRAIN_SOURCE': source, 'AGENT_BRAIN_DATA': data}}
        if source == 'codex':
            install_codex(home / '.codex/config.toml', data)
            write(home / '.codex/skills/agent-brain/SKILL.md', skill_path.read_text())
        elif source == 'cowork':
            folder = claude / ('Claude-3p' if (claude / 'Claude-3p').is_dir() else 'Claude')
            install_json(folder / 'claude_desktop_config.json', source, data)
        elif source == 'antigravity':
            install_json(home / '.gemini/config/mcp_config.json', source, data)
        elif source == 'deepseek-harness':
            out = home / '.dsh/agent-brain-mcp.json'
            write(out, json.dumps({'mcpServers': {'agent-brain': command}}, ensure_ascii=False, indent=2) + '\n')
            write(home / '.dsh/skills/agent-brain/SKILL.md', skill_path.read_text())
            print(f'Import in DeepSeek Harness: {out}')
        elif source == 'kimi-work':
            install_kimi(home, data, command)
        print(f'Staged {source} configuration')
    self_check(data)
    if args.demo:
        subprocess.run([COMMAND, '-B', str(ROOT / 'scripts/demo.py')], check=True)


if __name__ == '__main__':
    main()
