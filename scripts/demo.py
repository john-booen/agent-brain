"""Run a fictional Codex-to-Cowork handoff over the real local MCP protocol."""
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

root = Path(__file__).resolve().parents[1]


def call(child, method, params):
    call.counter += 1
    child.stdin.write(json.dumps({"jsonrpc": "2.0", "id": call.counter, "method": method, "params": params}) + "\n")
    child.stdin.flush()
    reply = json.loads(child.stdout.readline())
    if "error" in reply or reply.get("result", {}).get("isError"):
        raise RuntimeError(reply)
    return reply["result"]


call.counter = 0


def tool(child, name, arguments):
    return json.loads(call(child, "tools/call", {"name": name, "arguments": arguments})["content"][0]["text"])


def client(data):
    env = {**os.environ, 'AGENT_BRAIN_DATA': data, 'AGENT_BRAIN_USAGE_ORIGIN': 'test'}
    env.pop('AGENT_BRAIN_SOURCE', None)
    return subprocess.Popen([sys.executable, str(root / "brain.py"), "mcp"],
                            env=env,
                            stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True)


def run(data, name, arguments):
    with client(data) as child:
        call(child, "initialize", {"protocolVersion": "2025-11-25", "capabilities": {},
                                   "clientInfo": {"name": "fictional-demo", "version": "1"}})
        result = tool(child, name, arguments)
        child.stdin.close()
        if child.wait(timeout=5):
            raise RuntimeError("MCP client failed")
        return result


with tempfile.TemporaryDirectory(prefix="agent-brain-demo-") as data:
    fixture = json.loads((root / "examples" / "demo.json").read_text())
    assert run(data, "log_work", {**fixture, "create_project": True})["written"]
    assert run(data, 'log_work', {'project': fixture['project'], 'source': 'kimi-work',
                                'summary': 'Prepared the handoff notes for the stage rehearsal.',
                                'inputs': [{'text': 'Stage rehearsal starts Friday.'}]})['written']
    print("Codex → log_work")
    print("Kimi demo client → log_work")
    print("Cowork demo client → get_project")
    result = run(data, "get_project", {"project": fixture["project"]})
    assert result.get('records') == 2
    print(result["card"])
