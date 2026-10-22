#!/usr/bin/env python3
"""Live harness pinning the Gemini task-mode tool bridge (real network, real model).

It drives the production path in-process (`GeminiHTTPClient.chat_stream` ->
`_RobinRunActor` -> PQ/ERT drivers) with a byte tap on `_RobinHTTP2Mux` and asserts the
properties that the design depends on:

  P1  every RobinService RPC (RegisterDevice / ProcessQuery / ReportClientPresence /
      ExecuteRemoteTools) runs on the shared HTTP/2 mux, and the ERT response can push
      frames after connect (`send_grpc_frames` present);
  P2  the dispatched `list_mcp` frame decodes into a tool call named `list_mcp` with an
      empty-but-present id, and never into an "unknown confirmation op";
  P3  a tool dispatch reaches DSH as an OpenAI `tool_calls` delta with a usable id;
  P4  feeding the tool result back produces an ERT `FunctionResponse` that echoes the
      original server-side name and wire id (not the adapted DSH tool name);
  P5  the remote run survives the handoff (follow-up request is served by the parked
      actor, no `AttributeError`, no reconnect).

Usage:
    .venv/bin/python test/gemini_tool_flow_harness.py
    .venv/bin/python test/gemini_tool_flow_harness.py --prompt "..." --model gemini:gemini-4-argon

Exit code 0 means all pinned behaviours held. State is redirected to a temp dir, so the
developer's real conversation store is untouched. No repo file is modified.
"""

import argparse
import asyncio
import gzip
import json
import os
import sys
import tempfile
import time
import traceback

TMP = tempfile.mkdtemp(prefix="ve_tool_flow_harness_")
os.environ.setdefault("VE_GEMINI_LOG_DEBUG", "1")
os.environ["VE_GEMINI_STATE_DB"] = os.path.join(TMP, "state.sqlite3")
os.environ["VE_ROBIN_SESSION_CACHE_FILE"] = os.path.join(TMP, "robin_sessions.json")

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

import voice_edge as ve  # noqa: E402

EVENTS = []  # {"kind": open|out|in, "sid": int, "path": str, "data": bytes}


def grpc_frames(blob: bytes):
    """Split DATA bytes into gRPC frames, decompressing when the flag is set."""
    i = 0
    while i + 5 <= len(blob):
        flag = blob[i]
        length = int.from_bytes(blob[i + 1 : i + 5], "big")
        body = blob[i + 5 : i + 5 + length]
        if len(body) < length:
            return
        if flag & 1:
            try:
                body = gzip.decompress(body)
            except OSError:
                pass
        yield body
        i += 5 + length


def install_tap():
    Mux = ve._RobinHTTP2Mux
    orig_open = Mux.open_stream
    orig_send = Mux.send_data
    orig_dispatch = Mux._dispatch

    async def open_stream(self, path, headers, payload, *, end_stream=True):
        before = set(self.streams)
        resp = await orig_open(self, path, headers, payload, end_stream=end_stream)
        for sid in set(self.streams) - before:
            EVENTS.append(
                {
                    "kind": "open",
                    "sid": sid,
                    "path": path,
                    "mux": id(self),
                    "data": bytes(payload or b""),
                    "has_push": hasattr(resp, "send_grpc_frames"),
                }
            )
        return resp

    async def send_data(self, stream_id, payload, *, end_stream=False):
        EVENTS.append({"kind": "out", "sid": stream_id, "data": bytes(payload)})
        return await orig_send(self, stream_id, payload, end_stream=end_stream)

    def _dispatch(self, event):
        import h2.events as hev

        if isinstance(event, hev.DataReceived):
            EVENTS.append(
                {"kind": "in", "sid": event.stream_id, "data": bytes(event.data)}
            )
        return orig_dispatch(self, event)

    Mux.open_stream = open_stream
    Mux.send_data = send_data
    Mux._dispatch = _dispatch


def sse_events(chunk: bytes):
    for line in chunk.decode("utf-8", "replace").splitlines():
        line = line.strip()
        if not line.startswith("data:"):
            continue
        payload = line[5:].strip()
        if not payload:
            continue
        if payload == "[DONE]":
            yield {"done": True}
            continue
        try:
            yield json.loads(payload)
        except json.JSONDecodeError:
            pass


async def run_turn(client, spec, base, messages, label, budget=200):
    started = time.monotonic()
    gen, err = await asyncio.wait_for(client.chat_stream(spec, base, messages), 90)
    if gen is None:
        raise AssertionError(
            f"[{label}] chat_stream returned no stream: status={getattr(err, 'status', None)}"
        )
    content, reasoning, calls, finish = [], [], {}, None
    async for raw in gen:
        for event in sse_events(raw):
            if event.get("done"):
                continue
            for choice in event.get("choices") or []:
                delta = choice.get("delta") or {}
                if delta.get("content"):
                    content.append(delta["content"])
                if delta.get("reasoning_content"):
                    reasoning.append(delta["reasoning_content"])
                for tc in delta.get("tool_calls") or []:
                    slot = calls.setdefault(
                        tc.get("index", 0), {"id": "", "name": "", "args": ""}
                    )
                    if tc.get("id"):
                        slot["id"] = tc["id"]
                    fn = tc.get("function") or {}
                    slot["name"] += fn.get("name") or ""
                    slot["args"] += fn.get("arguments") or ""
                if choice.get("finish_reason"):
                    finish = choice["finish_reason"]
        if time.monotonic() - started > budget:
            raise AssertionError(f"[{label}] exceeded {budget}s budget")
    result = {
        "finish": finish,
        "content": "".join(content),
        "reasoning": "".join(reasoning),
        "calls": calls,
    }
    # P3b: a `tool_calls` finish must carry at least one call. DSH maps an empty
    # toolUse step to "turn completed" and silently drops the rest of the turn.
    assert not (finish == "tool_calls" and not calls), (
        f"[{label}] invalid toolUse: finish=tool_calls with zero tool_calls "
        "(handoff raced ahead of the delivered delta)"
    )
    print(f"\n== [{label}] finish={finish} in {time.monotonic() - started:.1f}s")
    print(f"   reasoning: {result['reasoning'][:160].replace(chr(10), ' / ')}")
    print(f"   content  : {result['content'][:160].replace(chr(10), ' / ')}")
    for index in sorted(calls):
        call = calls[index]
        print(
            f"   tool_call[{index}] id={call['id']!r} name={call['name']!r} "
            f"args={call['args'][:200]}"
        )
    return result


def ert_events(start):
    events = EVENTS[start:]
    opens = [e for e in events if e["kind"] == "open"]
    ert_sids = {
        e["sid"]
        for e in EVENTS
        if e["kind"] == "open" and e["path"].endswith("/ExecuteRemoteTools")
    }
    inbound = [
        frame
        for e in events
        if e["kind"] == "in" and e["sid"] in ert_sids
        for frame in grpc_frames(e["data"])
    ]
    outbound = [
        frame
        for e in events
        if e["kind"] == "out" and e["sid"] in ert_sids
        for frame in grpc_frames(e["data"])
    ]
    return opens, inbound, outbound


def assert_shared_mux(opens):
    """P1: all four RPCs share one HTTP/2 connection and ERT is push-capable."""
    names = {e["path"].rsplit("/", 1)[-1]: e for e in opens}
    required = {
        "RegisterDevice",
        "ProcessQuery",
        "ReportClientPresence",
        "ExecuteRemoteTools",
    }
    missing = required - set(names)
    assert not missing, f"P1: RPCs missing from the mux: {sorted(missing)}"
    assert names["ExecuteRemoteTools"]["has_push"], (
        "P1: ERT response cannot push frames (aiohttp fallback) - "
        "tool results would be dropped"
    )
    muxes = {e["mux"] for e in opens}
    assert len(muxes) == 1, f"P1: RPCs used {len(muxes)} connections, expected 1"
    print(
        "   P1 ok: one mux, ERT sid="
        f"{names['ExecuteRemoteTools']['sid']}, push-capable; "
        f"streams={[e['path'].rsplit('/', 1)[-1] for e in opens]}"
    )


def parse_dispatch(frame):
    return ve._robin_parse_execute_remote_tools_response(frame)


async def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--model", default="gemini:gemini-4-argon")
    parser.add_argument(
        "--prompt",
        default="请读取我工作区里的 README.md 的前 5 行并原样告诉我，必须用你可用的文件工具读取。",
    )
    args = parser.parse_args()

    install_tap()
    spec = ve.GeminiModelSpec.from_public_id(args.model)
    credentials = ve._gemini_discover_credentials()
    if not credentials:
        raise SystemExit("no Gemini credentials available")
    print(f"# model={spec.public_id} tmp={TMP}")

    tools = [
        {
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
        {
            "type": "function",
            "function": {
                "name": "ask_user_question",
                "description": "Ask the human operator a question.",
                "parameters": {
                    "type": "object",
                    "properties": {"question": {"type": "string"}},
                    "required": ["question"],
                },
            },
        },
    ]
    body = {"model": spec.public_id, "tools": tools, "stream": True}
    messages = [
        {
            "role": "system",
            "content": (
                f"You are a helpful assistant. Your working directory is {ROOT}."
            ),
        },
        {"role": "user", "content": args.prompt},
    ]
    base = ve._gemini_base_payload(
        spec, body, messages, credential_id=credentials[0].credential_id
    )
    client = ve.GeminiHTTPClient(credentials)

    first = await run_turn(client, spec, base, messages, "turn1")
    await asyncio.sleep(0.5)
    opens, inbound, outbound = ert_events(0)
    assert_shared_mux(opens)

    parsed_in = [parse_dispatch(frame) for frame in inbound]
    list_mcp = [
        call
        for parsed in parsed_in
        for call in parsed["tool_calls"]
        if call["name"] in ("list_mcp", "client:list_mcp")
    ]
    assert list_mcp, "P2: no list_mcp dispatch observed on ERT"
    assert list_mcp[0]["id"] == "" and list_mcp[0]["id_present"], (
        f"P2: list_mcp id shape changed: {list_mcp[0]!r}"
    )
    assert all(not parsed["unexpected_confirm_ops"] for parsed in parsed_in), (
        f"P2: dispatch misread as confirmation: {[p['unexpected_confirm_ops'] for p in parsed_in]}"
    )
    print(
        "   P2 ok: list_mcp dispatch decoded, "
        f"{len(list_mcp)} call(s), no confirmation fallback"
    )

    assert first["finish"] == "tool_calls" and first["calls"], (
        "P3: model did not produce DSH tool_calls "
        f"(finish={first['finish']}, content={first['content'][:120]!r})"
    )
    assert all(call["id"] for call in first["calls"].values()), (
        f"P3: tool_call id missing: {first['calls']}"
    )
    print(f"   P3 ok: {len(first['calls'])} tool_call(s) delivered to DSH")

    # Feed results back through the parked actor.
    assistant = {
        "role": "assistant",
        "content": first["content"],
        "tool_calls": [
            {
                "id": call["id"],
                "type": "function",
                "function": {
                    "name": call["name"],
                    "arguments": call["args"] or "{}",
                },
            }
            for _, call in sorted(first["calls"].items())
        ],
    }
    tool_messages = [
        {
            "role": "tool",
            "tool_call_id": call["id"],
            "name": call["name"],
            "content": "Voice Edge\n\n本地语音与 LLM 中继服务。\n\n## 简介\n这是一个把 macOS 语音、转录与多种 LLM 后端串起来的本地服务。",
        }
        for _, call in sorted(first["calls"].items())
    ]
    followup = [*messages, assistant, *tool_messages]
    base2 = ve._gemini_base_payload(
        spec, body, followup, credential_id=credentials[0].credential_id
    )
    mark = len(EVENTS)
    second = await run_turn(client, spec, base2, followup, "turn2")
    await asyncio.sleep(0.5)
    _, _, outbound2 = ert_events(mark)

    response_names = []
    for frame in outbound2:
        for fno, wtype, val in ve._pb_iter_fields(frame):
            if fno != 2 or wtype != 2:
                continue
            for inner_fno, inner_wtype, inner_val in ve._pb_iter_fields(bytes(val)):
                if inner_fno == 1 and inner_wtype == 2:
                    fields = dict(
                        (f, v) for f, _w, v in ve._pb_iter_fields(bytes(inner_val))
                    )
                    if 1 in fields:
                        response_names.append(bytes(fields[1]).decode("utf-8"))
    assert response_names, "P4: no FunctionResponse frame reached the ERT stream"
    wire_names = [
        call["name"]
        for parsed in parsed_in
        for call in parsed["tool_calls"]
        if call["name"] not in ("list_mcp", "client:list_mcp")
    ]
    assert any(name in wire_names for name in response_names), (
        f"P4: response names {response_names} did not echo the wire dispatch names {wire_names}"
    )
    print(f"   P4 ok: FunctionResponse echoed {response_names} (wire {wire_names})")

    assert second["finish"] in ("tool_calls", "stop"), (
        f"P5: follow-up turn ended abnormally ({second['finish']!r})"
    )
    print("   P5 ok: parked run resumed and produced a normal step")

    # P6: a DSH title request issued while the first turn is still running must return the
    # generated title, not the provisional prompt echo.
    title_prompt = "请用一句话说明你正在做什么，不要调用任何工具。"
    plain_messages = [
        {"role": "system", "content": "You are a helpful assistant."},
        {"role": "user", "content": title_prompt},
    ]
    plain_body = {"model": spec.public_id, "stream": True}
    plain_base = ve._gemini_base_payload(
        spec, plain_body, plain_messages, credential_id=credentials[0].credential_id
    )
    _, plain_boot_key, _ = ve._robin_conversation_keys(
        credentials[0].credential_id, plain_messages, body=plain_body
    )

    async def run_plain_turn() -> str:
        plain_gen, plain_err = await client.chat_stream(
            spec, plain_base, plain_messages
        )
        assert plain_gen is not None, f"P6: plain turn failed: {plain_err}"
        parts = []
        async for raw in plain_gen:
            for event in sse_events(raw):
                for choice in event.get("choices") or []:
                    delta = choice.get("delta") or {}
                    if delta.get("content"):
                        parts.append(delta["content"])
        return "".join(parts)

    plain_started = time.monotonic()
    plain_task = asyncio.create_task(run_plain_turn())
    for _ in range(120):
        state = ve._robin_find_session_by_boot_key(
            plain_boot_key, principal_id=credentials[0].credential_id
        )
        if state is not None and state.conversation_id:
            break
        await asyncio.sleep(0.1)

    title_messages = [
        {
            "role": "user",
            "content": (
                "Generate the session title from this JSON array of human messages: "
                + json.dumps(
                    [{"role": "user", "text": title_prompt}], ensure_ascii=False
                )
            ),
        }
    ]
    title_base = ve._gemini_base_payload(
        spec,
        {"model": spec.public_id},
        title_messages,
        credential_id=credentials[0].credential_id,
    )
    print(f"   [p6] conversation ready after {time.monotonic() - plain_started:.1f}s")
    title_started = time.monotonic()
    title_gen, title_err = await client.chat_stream(spec, title_base, title_messages)
    assert title_gen is not None, f"P6: title request failed: {title_err}"
    title_parts = []
    async for raw in title_gen:
        for event in sse_events(raw):
            for choice in event.get("choices") or []:
                delta = choice.get("delta") or {}
                if delta.get("content"):
                    title_parts.append(delta["content"])
    resolved_title = "".join(title_parts).strip()
    plain_answer = await plain_task
    print(
        f"   [p6] plain turn finished at {time.monotonic() - plain_started:.1f}s, "
        f"title request took {time.monotonic() - title_started:.1f}s"
    )
    prompt_norm = ve._robin_normalize_title_text(title_prompt)
    assert plain_answer.strip(), "P6: the bound turn produced no answer"
    assert resolved_title, "P6: title request returned an empty title"
    assert ve._robin_normalize_title_text(resolved_title) != prompt_norm, (
        f"P6: title request returned the prompt echo {resolved_title!r}"
    )
    print(
        f"   P6 ok: title={resolved_title!r} after "
        f"{time.monotonic() - title_started:.1f}s"
    )

    # Release any still-parked runtime so the process exits without dangling sessions.
    for key in list(ve._ROBIN_PARKED_STREAMS):
        ve._robin_release_parked_stream_sync(ve._robin_pop_parked_stream(key))
    await asyncio.sleep(0.2)
    print("\nRESULT: PASS")


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except AssertionError as exc:
        print(f"\nRESULT: FAIL - {exc}")
        traceback.print_exc()
        raise SystemExit(1) from None
