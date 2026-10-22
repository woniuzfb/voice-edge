#!/usr/bin/env python3
"""Restart-resume harness: a second turn must continue the SAME Gemini conversation.

Two turns run in two separate Python processes sharing one state DB, which is exactly
what "restart the script, then continue an existing DSH session" does:

  phase 1  fresh session -> real turn -> session parent committed to SQLite
  phase 2  new process, same DB -> follow-up turn -> must reuse conversation_id and
           must not silently open a brand-new Gemini conversation

Usage:
    .venv/bin/python test/gemini_session_resume_harness.py

Exit code 0 means the conversation was resumed after a restart. State lives in a temp
dir, so the developer's live session store is untouched.
"""

import argparse
import asyncio
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TMP = os.path.join(tempfile.gettempdir(), f"ve_resume_harness_{os.getpid()}")
DB = os.path.join(TMP, "state.sqlite3")
CACHE = os.path.join(TMP, "robin_sessions.json")

os.environ.setdefault("VE_GEMINI_LOG_DEBUG", "0")
os.environ["VE_GEMINI_STATE_DB"] = DB
os.environ["VE_ROBIN_SESSION_CACHE_FILE"] = CACHE
sys.path.insert(0, ROOT)

import voice_edge as ve  # noqa: E402

PROMPT1 = "请用一句话说明你此刻能做什么，不要调用任何工具。"
PROMPT2 = "刚才那句话是什么？只复述一遍，不要调用工具。"


def sse(chunk):
    for line in chunk.decode("utf-8", "replace").splitlines():
        line = line.strip()
        if line.startswith("data:") and line[5:].strip() not in ("", "[DONE]"):
            try:
                yield json.loads(line[5:].strip())
            except json.JSONDecodeError:
                pass


async def turn(messages, tools=None):
    creds = ve._gemini_discover_credentials()
    spec = ve.GeminiModelSpec.from_public_id("gemini:gemini-4-argon")
    client = ve.GeminiHTTPClient(creds)
    body = {"model": spec.public_id, "stream": True}
    if tools:
        body["tools"] = tools
    base = ve._gemini_base_payload(
        spec, body, messages, credential_id=creds[0].credential_id
    )
    gen, err = await client.chat_stream(spec, base, messages)
    assert gen is not None, f"chat_stream failed: {err}"
    content, calls, finish = [], {}, None
    async for raw in gen:
        for ev in sse(raw):
            for ch in ev.get("choices") or []:
                d = ch.get("delta") or {}
                if d.get("content"):
                    content.append(d["content"])
                for tc in d.get("tool_calls") or []:
                    slot = calls.setdefault(
                        tc.get("index", 0), {"name": "", "args": ""}
                    )
                    fn = tc.get("function") or {}
                    slot["name"] += fn.get("name") or ""
                    slot["args"] += fn.get("arguments") or ""
                if ch.get("finish_reason"):
                    finish = ch["finish_reason"]
    return "".join(content), calls, finish, creds[0].credential_id, spec


def session_row(boot_key: str, principal: str):
    row = ve._GEMINI_DSH_STORE.find_session(boot_key=boot_key)
    return row or {}


async def phase1(state_path: str):
    messages = [
        {"role": "system", "content": "You are a helpful assistant."},
        {"role": "user", "content": PROMPT1},
    ]
    answer, _, finish, principal, _spec = await turn(messages)
    assert answer.strip(), f"phase1 produced no answer (finish={finish})"
    _, boot_key, full_key = ve._robin_conversation_keys(
        principal, messages, body={"model": "gemini:gemini-4-argon"}
    )
    row = session_row(boot_key, principal)
    conv = str(row.get("conversation_id") or "")
    req = str(row.get("parent_req_id") or "")
    resp = str(row.get("parent_resp_id") or "")
    assert conv and req and resp, f"phase1 did not commit a parent: {row}"
    state = {
        "boot_key": boot_key,
        "full_key": full_key,
        "principal": principal,
        "conv": conv,
        "req": req,
        "resp": resp,
        "answer": answer,
    }
    await asyncio.to_thread(
        Path(state_path).write_text, json.dumps(state, ensure_ascii=False), "utf-8"
    )
    print(
        json.dumps(
            {"phase": 1, **{k: state[k] for k in ("conv", "req", "resp")}},
            ensure_ascii=False,
        )
    )


async def phase2(state_path: str):
    state = json.loads(await asyncio.to_thread(Path(state_path).read_text, "utf-8"))
    messages = [
        {"role": "system", "content": "You are a helpful assistant."},
        {"role": "user", "content": PROMPT1},
        {"role": "assistant", "content": state["answer"]},
        {"role": "user", "content": PROMPT2},
    ]
    # The restart lookup must already resolve the old conversation before any network
    # call: this is the exact assertion that fails when a restart opens a new session.
    spec = ve.GeminiModelSpec.from_public_id("gemini:gemini-4-argon")
    creds = ve._gemini_discover_credentials()
    check = ve._gemini_base_payload(
        spec,
        {"model": spec.public_id, "stream": True},
        messages,
        credential_id=creds[0].credential_id,
    )
    resolved = str(check.get("_ve_robin_conv_id") or "")
    print(
        json.dumps(
            {
                "phase": 2,
                "resolved_conv": resolved,
                "parent_req": check.get("_ve_robin_parent_req_id"),
            },
            ensure_ascii=False,
        )
    )
    assert resolved == state["conv"], (
        "restart lookup did not resolve the old conversation: "
        f"expected {state['conv']}, got {resolved!r}"
    )
    answer, _, finish, _principal, _spec = await turn(messages)
    row = session_row(state["boot_key"], state["principal"])
    conv = str(row.get("conversation_id") or "")
    print(
        json.dumps(
            {
                "phase": 2,
                "conv": conv,
                "answer": answer[:120],
                "finish": finish,
            },
            ensure_ascii=False,
        )
    )
    assert conv == state["conv"], (
        "restart lost the conversation: phase1="
        f"{state['conv']} phase2={conv} (a new Gemini conversation was opened)"
    )
    assert answer.strip(), f"phase2 produced no answer (finish={finish})"


def run_phase(phase: int, state_path: str):
    cmd = [
        sys.executable,
        os.path.abspath(__file__),
        "--phase",
        str(phase),
        "--state",
        state_path,
    ]
    env = dict(os.environ)
    env["VE_GEMINI_STATE_DB"] = DB
    env["VE_ROBIN_SESSION_CACHE_FILE"] = CACHE
    env["VE_GEMINI_LOG_DEBUG"] = "0"
    out = subprocess.run(
        cmd, cwd=ROOT, env=env, capture_output=True, text=True, timeout=420
    )
    if out.returncode != 0:
        print(out.stdout[-2000:])
        print(out.stderr[-4000:])
        raise SystemExit(f"phase {phase} failed with exit {out.returncode}")
    lines = [ln for ln in out.stdout.splitlines() if ln.startswith("{")]
    merged: dict = {}
    for ln in lines:
        merged.update(json.loads(ln))
    return merged


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--phase", type=int, choices=[1, 2], default=0)
    ap.add_argument("--state", default=os.path.join(TMP, "state.json"))
    args = ap.parse_args()
    os.makedirs(TMP, exist_ok=True)

    if args.phase == 1:
        asyncio.run(phase1(args.state))
        return
    if args.phase == 2:
        asyncio.run(phase2(args.state))
        return

    print(f"# temp state: {TMP}")
    first = run_phase(1, args.state)
    print(f"# phase1 conv={first['conv']} parent_req={first['req']}")
    second = run_phase(2, args.state)
    print(f"# phase2 conv={second['conv']} answer={second['answer']!r}")
    if second["conv"] != first["conv"]:
        raise SystemExit("RESULT: FAIL - conversation changed after restart")
    print("RESULT: PASS - same conversation resumed after restart")


if __name__ == "__main__":
    main()
