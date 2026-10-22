"""Title-request race harness: DSH asks for the title BEFORE the turn creates its session.

DSH fires the title request a few milliseconds ahead of the main chat request (measured
20:39:52.651 vs .658). The title request must wait for that session and return the
generated title, never the raw prompt.

Usage: .venv/bin/python test/gemini_title_race_harness.py   (exit 0 = pass)
"""

import argparse
import asyncio
import json
import os
import sys
import tempfile
import time
from pathlib import Path

TMP = tempfile.mkdtemp(prefix="ve_title_race_")
os.environ["VE_GEMINI_STATE_DB"] = os.path.join(TMP, "state.sqlite3")
os.environ["VE_ROBIN_SESSION_CACHE_FILE"] = os.path.join(TMP, "robin_sessions.json")
os.environ.setdefault("VE_GEMINI_LOG_DEBUG", "1")
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import voice_edge as ve  # noqa: E402

PROMPT = "请用一句话说明你此刻能做什么，不要调用任何工具。"

# DSH injects workspace instructions and a runtime snapshot as extra user messages
# (see session.v4.jsonl: a 5338-char `<system-reminder>` and a 482-char runtime block).
AGENTS_REMINDER = (
    "<system-reminder>\n"
    "The following workspace instructions may be relevant to your work.\n\n"
    "Instructions from: AGENTS.md\n\n# AGENTS.md\n\nFollow the project rules.\n"
    "</system-reminder>"
)
RUNTIME_CONTEXT = (
    "Current runtime context. This snapshot supersedes earlier runtime-context "
    "snapshots.\n\nCurrent DSH file policy: workspace-write. Any available operation "
    "enforced by the DSH file sandbox may modify files under the session workspace: "
    '"/tmp".\n\nApproval policy: ask.'
)


def sse(chunk):
    for line in chunk.decode("utf-8", "replace").splitlines():
        line = line.strip()
        if line.startswith("data:") and line[5:].strip() not in ("", "[DONE]"):
            try:
                yield json.loads(line[5:].strip())
            except json.JSONDecodeError:
                pass


async def collect(gen):
    text = []
    async for raw in gen:
        for ev in sse(raw):
            for ch in ev.get("choices") or []:
                d = ch.get("delta") or {}
                if d.get("content"):
                    text.append(d["content"])
    return "".join(text)


async def main(order: str):
    creds = ve._gemini_discover_credentials()
    spec = ve.GeminiModelSpec.from_public_id("gemini:gemini-4-argon")
    client = ve.GeminiHTTPClient(creds)
    principal = creds[0].credential_id

    # Context messages come first, exactly like the failing 20:39 request: the boot key
    # must still be derived from PROMPT on both sides.
    msgs = [
        {"role": "system", "content": "You are a helpful assistant."},
        {"role": "user", "content": AGENTS_REMINDER},
        {"role": "user", "content": RUNTIME_CONTEXT},
        {"role": "user", "content": PROMPT},
    ]
    base = ve._gemini_base_payload(
        spec, {"model": spec.public_id, "stream": True}, msgs, credential_id=principal
    )

    title_msgs = [
        {
            "role": "user",
            "content": "Generate the session title from this JSON array of human messages: "
            + json.dumps([{"role": "user", "text": PROMPT}], ensure_ascii=False),
        }
    ]
    tbase = ve._gemini_base_payload(
        spec, {"model": spec.public_id}, title_msgs, credential_id=principal
    )

    main_boot = str(base.get("_ve_boot_key") or "")
    title_boot, title_human = ve._robin_extract_title_request_boot_key(
        title_msgs, principal_id=principal
    )
    print(
        f"main_boot={main_boot} title_boot={title_boot} match={main_boot == title_boot}"
    )
    assert title_human == PROMPT, title_human
    assert main_boot == title_boot, (
        "boot_key drifted between the title request and the chat request: "
        f"{title_boot} != {main_boot}"
    )

    def stream_error(err: object) -> str:
        body = getattr(err, "body", b"") or b""
        return f"status={getattr(err, 'status_code', '?')} body={bytes(body)[:300]!r}"

    async def do_title():
        started = time.monotonic()
        gen, err = await client.chat_stream(spec, tbase, title_msgs)
        assert gen is not None, stream_error(err)
        text = await collect(gen)
        return text.strip(), time.monotonic() - started

    async def run_chat():
        started = time.monotonic()
        gen, err = await client.chat_stream(spec, base, msgs)
        assert gen is not None, stream_error(err)
        text = await collect(gen)
        return text, time.monotonic() - started

    if order == "title-first":
        # Case 2: the title request arrives before the chat request has created its
        # session (measured 7ms earlier at 20:39:52).
        title_task = asyncio.create_task(do_title())
        await asyncio.sleep(0.01)
        answer, chat_elapsed = await run_chat()
        title, elapsed = await title_task
    else:
        # Case 1: the chat request is already in flight (it may still be opening its
        # upstream stream), and the title request follows shortly after.
        chat_task = asyncio.create_task(run_chat())
        await asyncio.sleep(0.4)
        title_task = asyncio.create_task(do_title())
        answer, chat_elapsed = await chat_task
        title, elapsed = await title_task

    print(f"chat turn finished in {chat_elapsed:.1f}s")
    prompt_norm = ve._robin_normalize_title_text(PROMPT)
    print(f"answer={answer[:40]!r}")
    print(f"title={title!r} after {elapsed:.1f}s")
    assert title, "title request returned an empty title"
    assert ve._robin_normalize_title_text(title) != prompt_norm, (
        f"title request returned the prompt echo: {title!r}"
    )
    print("RESULT: PASS")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--order", choices=("title-first", "chat-first"), default="title-first"
    )
    asyncio.run(main(parser.parse_args().order))
