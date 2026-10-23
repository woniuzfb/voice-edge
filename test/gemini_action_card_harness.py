#!/usr/bin/env python3
"""Deterministic harness pinning how the relay deals with a server-side ActionCard.

Why this exists
---------------
On 2026-10-23 a real DSH turn (3rd round of the same Gemini conversation) ended with
this as the *entire* assistant answer:

    "\\n\\nhttp://googleusercontent.com/action_card_content/14288659078536846835"

That URL is how the Gemini server encodes an ActionCard inside the plain answer text
(``.1003.1.1.2.1``). The relay only looked for a card in the protobuf field
``.1003.1.1.5``, so the marker went straight to DSH as the model's answer: the user saw
an authorization card inside the Gemini app, and the DSH turn "completed" with a URL.

Root cause, measured live (probes D/E/F, see ``gemini_action_card_probe.py``): the server
asks for GUI consent for every ``execute_mcp`` call that reaches an MCP tool advertised
with ``toolAnnotations.destructiveHint == true`` — the same tool dispatched normally once
that flag is false, and native ``run_command`` / ``write_to_file`` never card at all.
Probe F adds that shell gets no special treatment, so the relay only has to advertise
nothing destructive; this harness pins that contract and the text-level closure.

This harness drives the real production decode path
(``_RobinStreamDecoder.feed_bytes`` -> ``feed_pq_frame`` -> ``AnswerProjector``) with the
exact wire shape that was captured, so the behaviour is pinned instead of guessed.

Pinned properties
-----------------
  P1  a card marker that arrives in the answer text is never forwarded to DSH verbatim
      (no ``googleusercontent.com/action_card_content`` in any ``content`` delta);
  P2  when the card is the only content, DSH still gets a usable, card-free closure
      sentence, and the turn is finalised as completed;
  P3  the legacy ``.1003.1.1.5`` protobuf card field keeps closing, and a card payload
      that embeds the marker plus control bytes is cleaned rather than echoed;
  P4  a card marker appended to real answer text is stripped while the real text survives;
  P5  the advertised MCP catalog declares nothing destructive (the actual card trigger)
      while shell stays reachable through the native adapter.

Modes
-----
    # replay the captured shapes, exit 0 only when every pin holds
    .venv/bin/python test/gemini_action_card_harness.py

    # watchdog: scan every dsh session log for card markers and print the timeline
    .venv/bin/python test/gemini_action_card_harness.py --scan

State is redirected into a temp dir, so no real conversation store is touched.
"""

import argparse
import glob
import json
import os
import subprocess
import sys
import tempfile

TMP = tempfile.mkdtemp(prefix="ve_action_card_harness_")
os.environ["VE_GEMINI_STATE_DB"] = os.path.join(TMP, "state.sqlite3")
os.environ["VE_ROBIN_SESSION_CACHE_FILE"] = os.path.join(TMP, "robin_sessions.json")

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

import voice_edge as ve  # noqa: E402

CARD_URL = "http://googleusercontent.com/action_card_content/14288659078536846835"
CARD_MARKER = "googleusercontent.com/action_card_content"
CLOSURE_HINT = (
    "确认"  # any usable closure sentence must at least mention the confirmation
)


def _grpc_frame(payload: bytes) -> bytes:
    """Wrap a protobuf message the way the server frames it on the wire."""
    return b"\x00" + len(payload).to_bytes(4, "big") + payload


def _pq_frame(
    *,
    answer: str = "",
    card_field: str = "",
    completed: int | None = 2,
    response_id: str = "rc_card_harness",
) -> bytes:
    """Build ``ProcessQueryResponse { 1003 { 1 { 1 { ...candidate... } } } }``."""
    candidate = ve._pb_bytes_field(1, response_id)
    if answer:
        candidate += ve._pb_bytes_field(2, ve._pb_bytes_field(1, answer))
    if card_field:
        candidate += ve._pb_bytes_field(5, card_field)
    if completed is not None:
        candidate += ve._pb_varint_field(19, completed)
    inner = ve._pb_bytes_field(1, candidate)
    return ve._pb_bytes_field(1003, ve._pb_bytes_field(1, inner))


def _decode(frames: list[bytes]) -> tuple[str, str, object]:
    """Feed frames through the production decoder and collect the SSE deltas."""
    dec = ve._RobinStreamDecoder("chatcmpl-card-harness", 0, "gemini:gemini-4-argon")
    content: list[str] = []
    reasoning: list[str] = []
    for frame in frames:
        for chunk in dec.feed_bytes(_grpc_frame(frame)):
            delta = chunk["choices"][0]["delta"]
            if delta.get("content"):
                content.append(delta["content"])
            if delta.get("reasoning_content"):
                reasoning.append(delta["reasoning_content"])
    return "".join(content), "".join(reasoning), dec


def pin_p1_2_card_only() -> list[str]:
    """The exact shape captured from the real turn: card marker is the whole answer."""
    content, _reasoning, dec = _decode([_pq_frame(answer="\n\n" + CARD_URL)])
    failures: list[str] = []
    if CARD_MARKER in content:
        failures.append(
            f"P1: card marker leaked to DSH as content {content!r} "
            "(regression: DSH would store a googleusercontent URL as the answer)"
        )
    if CLOSURE_HINT not in content and not failures:
        failures.append(
            f"P2: closure sentence does not mention the confirmation: {content!r}"
        )
    if not dec.is_completed:
        failures.append("P2: turn was not finalised as completed")
    return failures


def pin_p3_legacy_card_field() -> list[str]:
    """The documented ``.1003.1.1.5`` card field must keep closing, cleaned."""
    content, _reasoning, _dec = _decode(
        [_pq_frame(card_field="是否允许执行 git status？")]
    )
    failures: list[str] = []
    if "确认" not in content or "是否允许执行 git status" not in content:
        failures.append(
            f"P3: legacy protobuf card field no longer closes with its text: {content!r}"
        )
    # A real card payload embeds the same marker plus protobuf control bytes.
    noisy, _reasoning, _dec = _decode(
        [
            _pq_frame(
                card_field="G\nE"
                + CARD_URL
                + "\t\nRun write? Allow Gemini to run write?"
            )
        ]
    )
    if CARD_MARKER in noisy:
        failures.append(f"P3: card payload echoed its marker: {noisy!r}")
    if "Run write?" not in noisy:
        failures.append(f"P3: readable card text was dropped: {noisy!r}")
    if "\t" in noisy:
        failures.append(f"P3: control bytes leaked into the answer: {noisy!r}")
    return failures


def pin_p4_card_appended_to_answer() -> list[str]:
    """Real answer text must survive while the marker itself is stripped."""
    content, _reasoning, _dec = _decode(
        [_pq_frame(answer="改动都对。" + "\n\n" + CARD_URL)]
    )
    failures: list[str] = []
    if "改动都对。" not in content:
        failures.append(f"P4: real answer text was dropped: {content!r}")
    if CARD_MARKER in content:
        failures.append(
            f"P4: card marker still forwarded inside a normal answer: {content!r}"
        )
    return failures


def pin_p5_catalog_contract() -> list[str]:
    """The actual card trigger: nothing advertised destructive, every tool still dispatchable."""
    tools = [
        {"type": "function", "function": {"name": "bash", "description": "Run shell."}},
        {"type": "function", "function": {"name": "read", "description": "Read."}},
        {"type": "function", "function": {"name": "write", "description": "Write."}},
        {"type": "function", "function": {"name": "edit", "description": "Edit."}},
    ]
    ledger = ve._RobinToolLedger("harness_catalog", tools)
    advertised = {item["name"] for item in ledger.normalized_mcp_tools}
    failures: list[str] = []
    if advertised != {"bash", "read", "write", "edit"}:
        failures.append(f"P5: catalog lost tools: {sorted(advertised)}")
    labelled = [
        item["name"]
        for item in ledger.normalized_mcp_tools
        if item.get("toolAnnotations", {}).get("destructiveHint") is not False
    ]
    if labelled:
        failures.append(
            f"P5: tools advertised as destructive (server will demand GUI consent): {labelled}"
        )
    inv, frame_bytes = ledger.register_ert_call(
        {
            "name": "client:run_command",
            "id": "call_p5_native",
            "args": {"CommandLine": "echo ok", "Cwd": "/tmp"},
        }
    )
    if frame_bytes is not None or inv is None or inv.dsh_tool_name != "bash":
        failures.append(
            "P5: native run_command no longer dispatches to DSH bash "
            f"(intercepted={frame_bytes is not None}, inv={inv})"
        )
    return failures


def run_pins() -> int:
    print(f"action-card harness (state db: {os.environ['VE_GEMINI_STATE_DB']})")
    print(f"captured marker: {CARD_URL}\n")
    failures: list[str] = []
    for label, fn in (
        ("P1/P2 card-only answer", pin_p1_2_card_only),
        ("P3 legacy .1003.1.1.5 field", pin_p3_legacy_card_field),
        ("P4 card appended to real answer", pin_p4_card_appended_to_answer),
        ("P5 MCP catalog contract", pin_p5_catalog_contract),
    ):
        got = fn()
        print(f"{'PASS' if not got else 'FAIL'}  {label}")
        for line in got:
            print(f"      {line}")
        failures.extend(got)
    print()
    if failures:
        print(f"{len(failures)} pinned behaviour(s) broken")
        return 1
    print("all pinned behaviours held")
    return 0


def _decompress(path: str) -> bytes:
    try:
        return subprocess.run(
            ["zstd", "-d", "-c", path], capture_output=True, timeout=60
        ).stdout
    except (OSError, subprocess.SubprocessError):
        return b""


def scan_sessions() -> int:
    """Watchdog: report every persisted turn whose assistant text carries a card marker."""
    pattern = os.path.join(
        os.path.expanduser("~"),
        ".veh",
        "sessions",
        "*",
        "*",
        "session.v4.jsonl.zstd",
    )
    scanned = 0
    hits = 0
    for path in sorted(glob.glob(pattern)):
        raw = _decompress(path)
        if not raw:
            continue
        scanned += 1
        turn = 0
        step = 0
        last_call: dict[str, object] = {}
        for line in raw.splitlines():
            try:
                row = json.loads(line)
            except ValueError:
                continue
            kind = row.get("type")
            data = row.get("data") or {}
            if kind == "turn/start":
                turn = data.get("turn") or turn
                last_call = {}
            elif kind == "step/start":
                step = data.get("step") or step
            elif kind == "tool/call":
                last_call = {
                    "name": data.get("name"),
                    "arguments": str(data.get("arguments") or ""),
                    "step": data.get("step"),
                }
            elif kind == "assistant/message":
                message = data.get("message") or {}
                for part in message.get("content") or []:
                    text = str(part.get("text") or "")
                    if CARD_MARKER not in text:
                        continue
                    hits += 1
                    print(
                        f"{os.path.basename(os.path.dirname(path))} "
                        f"turn={turn} step={step} model={message.get('source', {}).get('model', '')}"
                    )
                    print(f"    assistant text: {text.strip()[:200]!r}")
                    # A consent card replaces the tool call, so the step that carries the
                    # marker has no tool/call of its own: the last dispatched call is the
                    # previous step's, and the gated call never reaches the relay.
                    if last_call:
                        print(
                            f"    last dispatched call: step{last_call['step']} "
                            f"{last_call['name']} {last_call['arguments'][:160]}"
                        )
                    else:
                        print("    last dispatched call: none in this turn")
                    print(
                        "    gated call: no tool/call record for this step "
                        "(the server answered with a card instead of dispatching)"
                    )
    print(
        f"\nscanned {scanned} session log(s), {hits} turn(s) carried an action-card marker"
    )
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--scan",
        action="store_true",
        help="scan persisted dsh session logs for action-card markers instead of replaying pins",
    )
    args = parser.parse_args()
    return scan_sessions() if args.scan else run_pins()


if __name__ == "__main__":
    sys.exit(main())
