"""The real entry point over stdio (milestone 5, sections 3 and 10).

The other protocol tests use the in-memory client, which never touches the
transport. These spawn `python -m easygoing_mcp.server` as a subprocess, exactly
as Claude Code and Claude Desktop will, and check two things the in-memory
client cannot: that the module entry point serves the protocol, and that
nothing but JSON-RPC reaches stdout (section 3: logging goes to stderr, never
`print()`).
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import threading
from pathlib import Path

import anyio
import pytest
from mcp import Client
from mcp.client.stdio import StdioServerParameters
from mcp_types import LATEST_PROTOCOL_VERSION

REPO = Path(__file__).resolve().parents[1]
DATA = REPO / "data"

pytestmark = pytest.mark.skipif(
    not (DATA / "canonical" / "MB20261028.canonical.json").exists(),
    reason="canonical files not built; run scripts/ingest.py",
)


def params(advice_risk: bool) -> StdioServerParameters:
    return StdioServerParameters(
        command=sys.executable,
        args=["-m", "easygoing_mcp.server"],
        env={
            "EASYGOING_DATA_DIR": str(DATA),
            "EASYGOING_ENABLE_ADVICE_RISK": "true" if advice_risk else "false",
        },
        cwd=str(REPO),
    )


def test_entry_point_serves_every_primitive_over_stdio():
    async def go():
        async with Client(params(advice_risk=True)) as client:
            tools = {t.name for t in (await client.list_tools()).tools}
            prompts = {p.name for p in (await client.list_prompts()).prompts}
            lives = await client.read_resource("easygoing://lives")
            hit = await client.call_tool("find_mentions", {"subject": "Stellantis", "limit": 1})
            prompt = await client.get_prompt(
                "summarize_live_portion", {"live_id": "MB20261028", "chapter": "1"}
            )
            return tools, prompts, lives, hit, prompt

    tools, prompts, lives, hit, prompt = anyio.run(go)
    assert {
        "find_mentions", "get_speaker_coverage", "rank_subjects",
        "get_subject_discussion", "list_rubric_occurrences",
    } <= tools
    assert prompts == {"summarize_live_portion", "compare_views"}
    assert [l["live_id"] for l in json.loads(lives.contents[0].text)["lives"]] == [
        "MB20261029", "MB20261028"
    ]
    assert hit.is_error is False and hit.structured_content["returned"] == 1
    assert "Davide Biocchi" in prompt.messages[0].content.text


def test_flag_is_read_from_the_environment():
    async def go():
        async with Client(params(advice_risk=False)) as client:
            return {t.name for t in (await client.list_tools()).tools}

    assert "get_subject_discussion" not in anyio.run(go)


def test_stdout_carries_only_json_rpc():
    """Drive the server by hand and require every stdout line to be a JSON-RPC message.

    The SDK client would skip or choke on a stray line without telling us which;
    reading the raw stream makes a leaked log line or print() an explicit failure.
    """
    messages = [
        {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {
            "protocolVersion": LATEST_PROTOCOL_VERSION,
            "capabilities": {},
            "clientInfo": {"name": "stdout-purity-test", "version": "0"},
        }},
        {"jsonrpc": "2.0", "method": "notifications/initialized"},
        {"jsonrpc": "2.0", "id": 2, "method": "tools/list"},
        {"jsonrpc": "2.0", "id": 3, "method": "resources/read",
         "params": {"uri": "easygoing://lives/NOPE/chapters"}},
    ]
    p = params(advice_risk=True)
    proc = subprocess.Popen(
        [p.command, *p.args],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        cwd=p.cwd,
        env={**os.environ, **(p.env or {})},
    )
    # Keep stdin open until every reply is in, as a real host does: closing it
    # early shuts the server down with requests still in flight.
    watchdog = threading.Timer(60, proc.kill)
    watchdog.start()
    parsed: list[dict] = []
    try:
        for message in messages:
            proc.stdin.write(json.dumps(message) + "\n")
        proc.stdin.flush()
        pending = {m["id"] for m in messages if "id" in m}
        while pending:
            line = proc.stdout.readline()
            if not line:
                break
            if not line.strip():
                continue
            try:
                reply = json.loads(line)
            except json.JSONDecodeError:
                pytest.fail(f"non-JSON line on stdout: {line[:200]!r}")
            parsed.append(reply)
            pending.discard(reply.get("id"))
        proc.stdin.close()
        stderr = proc.stderr.read()
        proc.wait(timeout=30)
    finally:
        watchdog.cancel()
        if proc.poll() is None:
            proc.kill()

    assert parsed, f"no output; stderr:\n{stderr}"
    assert not pending, f"no reply to {pending}; stderr:\n{stderr}"
    # Anything the server wrote after the replies must be protocol too.
    for line in proc.stdout.read().splitlines():
        if line.strip():
            json.loads(line)
    assert all(m.get("jsonrpc") == "2.0" for m in parsed)
    by_id = {m["id"]: m for m in parsed if "id" in m}
    assert "result" in by_id[1] and "result" in by_id[2]
    # The error path must not leak a traceback to stdout either, and keeps its message.
    assert "MB20261028" in by_id[3]["error"]["message"]
    # The startup log line exists, and it went to stderr.
    assert "advice-risk primitives registered" in stderr
