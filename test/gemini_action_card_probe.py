#!/usr/bin/env python3
"""Live probe: what makes the server answer with an ActionCard instead of dispatching?

Answer (measured 2026-10-23, six runs): the trigger is the *advertised MCP tool
annotation*. A call through `execute_mcp` to an MCP tool declared
`toolAnnotations.destructiveHint == true` gets a "Run <tool>?" GUI consent card, whatever
the command is; the very same tool dispatches normally once that flag is false, and the
native tools (`run_command`, `write_to_file`, ...) never card at all.

    A2  execute_mcp -> bash, write command, bash destructive      -> CARD
    B   execute_mcp -> bash, read-only command, bash destructive  -> CARD
    C   native run_command, write command                         -> DISPATCHED
    D   execute_mcp -> write, write destructive                   -> CARD
    E   execute_mcp -> write, destructiveHint flipped to false    -> DISPATCHED
    F   execute_mcp -> bash, bash advertised but not destructive  -> DISPATCHED

F rules out a shell-specific rule, so the relay only has to declare nothing destructive
(see the self-test case and `gemini_action_card_harness.py` P5). Re-running this probe
after that change is a regression check, not an experiment:

    # no channel may come back with a card any more
    .venv/bin/python test/gemini_action_card_probe.py --label bash --tools bash
    .venv/bin/python test/gemini_action_card_probe.py --label write --tools write

Verdicts
--------
    CARD        the answer text carried `action_card_content` -> the call was gated;
    DISPATCHED  a `tool_calls` delta for a real tool arrived -> the call reached us;
    LIST_ONLY   only the in-place `list_mcp` was answered, no real tool call;
    NO_ACTION   the model neither called a tool nor got a card.

Usage
-----
    .venv/bin/python test/gemini_action_card_probe.py --label A --tools read
    .venv/bin/python test/gemini_action_card_probe.py --label B --tools bash --prompt "..."
    .venv/bin/python test/gemini_action_card_probe.py --label E --tools write

Each run opens a brand-new cloud conversation, so every probe costs one light round trip.
State is redirected to a temp dir by the shared harness module; nothing is executed
locally (the probe stops as soon as a call is dispatched).
"""

import argparse
import asyncio
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import gemini_tool_flow_harness as harness

ve = harness.ve

CARD_MARKER = "googleusercontent.com/action_card_content"

WRITE_PROMPT = (
    "只做一步：用你的「运行命令」工具执行下面这条命令，然后把命令输出原样告诉我。"
    "不要读文件，不要搜索，不要解释。\n"
    "mkdir -p /tmp/ve_card_probe_a && echo ok > /tmp/ve_card_probe_a/a.txt"
)

READ_ONLY_BASH_PROMPT = (
    "只做一步：调用 execute_mcp 工具，ServerName 填 dsh，ToolName 填 bash，"
    'Arguments 填 {"command": "ls /tmp | head -3", "description": "list /tmp"}。'
    "拿到结果后原样告诉我就行，不要解释。"
)

TOOLS = {
    "read": {
        "type": "function",
        "function": {
            "name": "read",
            "description": "Read a file from the local filesystem.",
            "parameters": {
                "type": "object",
                "properties": {"file_path": {"type": "string"}},
                "required": ["file_path"],
            },
        },
    },
    "bash": {
        "type": "function",
        "function": {
            "name": "bash",
            "description": (
                "Run a shell command on the local machine through the dsh MCP server."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "command": {"type": "string"},
                    "description": {"type": "string"},
                },
                "required": ["command"],
            },
        },
    },
    "write": {
        "type": "function",
        "function": {
            "name": "write",
            "description": (
                "Write content to a file on the local machine through the dsh MCP server."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "file_path": {"type": "string"},
                    "content": {"type": "string"},
                },
                "required": ["file_path", "content"],
            },
        },
    },
}

WRITE_TOOL_PROMPT = (
    "只做一步：调用 execute_mcp，ServerName 填 dsh，ToolName 填 write，"
    'Arguments 填 {"file_path": "/tmp/ve_probe_w.txt", "content": "hi"}。'
    "拿到结果原样告诉我就行，不要解释。"
)


async def probe(label: str, prompt: str, model: str, tool_names: list[str]) -> int:
    spec = ve.GeminiModelSpec.from_public_id(model)
    credentials = ve._gemini_discover_credentials()
    if not credentials:
        print("no Gemini credentials available")
        return 2

    tools = [TOOLS[name] for name in tool_names if name in TOOLS]
    body: dict[str, object] = {"model": spec.public_id, "stream": True}
    if tools:
        body["tools"] = tools
    messages = [
        {
            "role": "system",
            "content": f"You are a helpful assistant. cwd={harness.ROOT}",
        },
        {"role": "user", "content": prompt},
    ]
    base = ve._gemini_base_payload(
        spec, body, messages, credential_id=credentials[0].credential_id
    )
    client = ve.GeminiHTTPClient(credentials)
    print(f"# probe {label}: model={spec.public_id} tools={tool_names}")

    try:
        result = await harness.run_turn(client, spec, base, messages, label, budget=150)
    finally:
        for key in list(ve._ROBIN_PARKED_STREAMS):
            ve._robin_release_parked_stream_sync(ve._robin_pop_parked_stream(key))
        await asyncio.sleep(0.2)

    content = result["content"]
    calls = result["calls"]
    real_calls = {
        idx: call
        for idx, call in calls.items()
        if call["name"] not in ("list_mcp", "client:list_mcp")
    }
    print(f"   finish={result['finish']}")
    for idx, call in sorted(calls.items()):
        print(f"   tool[{idx}] {call['name']} args={call['args'][:200]}")
    if CARD_MARKER in content:
        print(f"   VERDICT: CARD (gated, answer={content.strip()[:160]!r})")
        return 0
    if real_calls:
        print(f"   VERDICT: DISPATCHED ({[c['name'] for c in real_calls.values()]})")
        return 0
    if calls:
        print("   VERDICT: LIST_ONLY (only in-place list_mcp, no real tool call)")
        return 0
    print(f"   VERDICT: NO_ACTION (content={content[:200]!r})")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--label", default="probe")
    parser.add_argument("--model", default="gemini:gemini-4-argon-high")
    parser.add_argument("--prompt", default="")
    parser.add_argument(
        "--tools",
        default="read",
        help="comma separated subset of: read,bash,write (MCP tools declared to the model)",
    )
    args = parser.parse_args()
    if args.prompt:
        prompt = args.prompt
    elif "bash" in args.tools:
        prompt = READ_ONLY_BASH_PROMPT
    elif "write" in args.tools:
        prompt = WRITE_TOOL_PROMPT
    else:
        prompt = WRITE_PROMPT
    tool_names = [name.strip() for name in args.tools.split(",") if name.strip()]
    return asyncio.run(probe(args.label, prompt, args.model, tool_names))


if __name__ == "__main__":
    raise SystemExit(main())
