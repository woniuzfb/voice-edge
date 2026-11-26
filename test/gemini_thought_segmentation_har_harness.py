#!/usr/bin/env python3
"""Full HAR thought-segmentation replay harness and compliance sentinel."""

from __future__ import annotations

import argparse
import base64
import difflib
import gzip
import hashlib
import importlib.util
import json
import os
import re
import shutil
import struct
import sys
import tempfile
from pathlib import Path
from typing import Any

_REPO_ROOT = Path(__file__).resolve().parents[1]

_TARGET_RPCS = (
    "RobinService/ProcessQuery",
    "RobinService/ListConversationTurns",
)

# Independent reference separator (never delegates to voice_edge production helper).
_REF_CJK_TERMINAL_PUNCT: frozenset[str] = frozenset("。！？…：；")
_REF_EN_TERMINAL_PUNCT: frozenset[str] = frozenset(".!?:;")
_REF_CLOSING_WRAPPERS: str = ")]}\"'`*\u201d’）\uff3d\uff5d】》」』〉\u3015"
_REF_CLOSING_WRAPPER_SET: frozenset[str] = frozenset(_REF_CLOSING_WRAPPERS)
_REF_CJK_RE = re.compile(r"[\u3400-\u4dbf\u4e00-\u9fff\uf900-\ufaff]")


def _reference_text_separator(prev_text: str) -> str:
    """Independent reference separator for same-block `text -> text` boundaries."""
    if not prev_text or prev_text.endswith("\n"):
        return ""
    has_trailing_space = prev_text[-1].isspace()
    trimmed = prev_text.rstrip()
    if not trimmed:
        return ""
    last_ch = trimmed[-1]
    if last_ch in _REF_CJK_TERMINAL_PUNCT:
        return ""
    if last_ch in _REF_EN_TERMINAL_PUNCT:
        return "" if has_trailing_space else " "
    if last_ch in _REF_CLOSING_WRAPPER_SET:
        core = trimmed.rstrip(_REF_CLOSING_WRAPPERS)
        if core:
            core_last = core[-1]
            if core_last in _REF_CJK_TERMINAL_PUNCT:
                return ""
            if core_last in _REF_EN_TERMINAL_PUNCT:
                return "" if has_trailing_space else " "
    if _REF_CJK_RE.search(trimmed):
        return "。"
    return ". "


def _load_voice_edge_module(voice_edge_path: Path) -> tuple[Any, Path]:
    """Load `voice_edge.py` from `voice_edge_path` with isolated temporary state storage."""
    tmp_dir = Path(tempfile.mkdtemp(prefix="ve_thought_seg_harness_"))
    os.environ["VE_GEMINI_STATE_DB"] = str(tmp_dir / "gemini_dsh_state.sqlite3")
    os.environ["VE_ROBIN_SESSION_CACHE_FILE"] = str(
        tmp_dir / "robin_session_cache.json"
    )
    mod_name = f"voice_edge_under_test_{hashlib.sha256(str(voice_edge_path).encode()).hexdigest()[:12]}"
    spec = importlib.util.spec_from_file_location(mod_name, str(voice_edge_path))
    if spec is None or spec.loader is None:
        raise RuntimeError(f"Unable to load module spec from {voice_edge_path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[mod_name] = module
    spec.loader.exec_module(module)
    return module, tmp_dir


def _is_pq_aborted_empty_stream(entry: dict[str, Any], rpc_name: str) -> bool:
    """Exact predicate for aborted empty ProcessQuery streams (zero silent skips)."""
    if rpc_name != "RobinService/ProcessQuery":
        return False
    resp = entry.get("response") or {}
    content = resp.get("content") or {}
    text = content.get("text")
    size = content.get("size")
    body_size = resp.get("bodySize")
    elapsed = entry.get("time")
    return (
        text in (None, "")
        and (size is None or size == 0)
        and (body_size is None or body_size <= 0)
        and (elapsed is None or elapsed <= 0)
    )


def _decode_har_response_bytes(
    entry: dict[str, Any], rpc_name: str
) -> tuple[bytes | None, str]:
    """Decode HAR response content strictly without silent skips."""
    resp = entry.get("response") or {}
    content = resp.get("content") or {}
    text = content.get("text")
    encoding = str(content.get("encoding") or "").strip().lower()
    if text is None or text == "":
        if _is_pq_aborted_empty_stream(entry, rpc_name):
            return None, "PQ_ABORTED_EMPTY_STREAM"
        size = content.get("size")
        body_size = resp.get("bodySize")
        if (isinstance(size, (int, float)) and size > 0) or (
            isinstance(body_size, (int, float)) and body_size > 0
        ):
            return None, "MISSING_BODY_TRUNCATED"
        return None, "UNEXPECTED_EMPTY_BODY"
    if not isinstance(text, str):
        return None, "INVALID_CONTENT_TEXT_TYPE"
    if encoding == "base64":
        try:
            return base64.b64decode(text, validate=True), ""
        except Exception:
            return None, "BASE64_DECODE_ERROR"
    if encoding in ("", "plain", "latin-1", "latin1", "iso-8859-1", "utf-8", "utf8"):
        try:
            return text.encode("latin-1"), ""
        except UnicodeEncodeError:
            try:
                return text.encode("utf-8"), ""
            except Exception:
                return None, "PLAIN_ENCODE_ERROR"
    return None, f"UNKNOWN_ENCODING:{encoding}"


def _strict_unpack_grpc_frames(raw: bytes) -> tuple[list[bytes], str]:
    """Unpack length-prefixed gRPC frames strictly, failing on malformed headers or corrupt gzip."""
    frames: list[bytes] = []
    pos = 0
    total = len(raw)
    while pos < total:
        if total - pos < 5:
            return [], f"TRUNCATED_GRPC_HEADER_AT_{pos}"
        flag, length = struct.unpack(">BI", raw[pos : pos + 5])
        if flag not in (0, 1):
            return [], f"INVALID_GRPC_FLAG_{flag}_AT_{pos}"
        if pos + 5 + length > total:
            return [], f"TRUNCATED_GRPC_PAYLOAD_AT_{pos}_NEED_{length}"
        payload = raw[pos + 5 : pos + 5 + length]
        pos += 5 + length
        if flag == 1:
            try:
                payload = gzip.decompress(payload)
            except OSError:
                return [], f"CORRUPT_GZIP_FRAME_AT_{pos - length}"
        frames.append(payload)
    return frames, ""


def _strict_validate_protobuf(buf: bytes) -> str:
    """Validate top-level protobuf wire framing strictly; return empty string on success."""
    pos = 0
    total = len(buf)
    while pos < total:
        tag = 0
        shift = 0
        while True:
            if pos >= total:
                return "TRUNCATED_PB_TAG"
            b = buf[pos]
            pos += 1
            tag |= (b & 0x7F) << shift
            if not (b & 0x80):
                break
            shift += 7
            if shift > 63:
                return "VARINT_OVERFLOW_TAG"
        field_num = tag >> 3
        wire_type = tag & 0x07
        if field_num <= 0:
            return "INVALID_PB_FIELD_NUM"
        if wire_type == 0:
            shift = 0
            while True:
                if pos >= total:
                    return "TRUNCATED_PB_VARINT"
                b = buf[pos]
                pos += 1
                if not (b & 0x80):
                    break
                shift += 7
                if shift > 63:
                    return "VARINT_OVERFLOW_VAL"
        elif wire_type == 1:
            if pos + 8 > total:
                return "TRUNCATED_PB_FIXED64"
            pos += 8
        elif wire_type == 2:
            length = 0
            shift = 0
            while True:
                if pos >= total:
                    return "TRUNCATED_PB_LEN"
                b = buf[pos]
                pos += 1
                length |= (b & 0x7F) << shift
                if not (b & 0x80):
                    break
                shift += 7
                if shift > 63:
                    return "VARINT_OVERFLOW_LEN"
            if pos + length > total:
                return "TRUNCATED_PB_BYTES"
            pos += length
        elif wire_type == 5:
            if pos + 4 > total:
                return "TRUNCATED_PB_FIXED32"
            pos += 4
        else:
            return f"UNSUPPORTED_PB_WIRE_TYPE_{wire_type}"
    return ""


def _verify_projected_boundaries(
    ve: Any,
    proj: Any,
    *,
    tag: str,
    stats: dict[str, int],
    failures: list[str],
) -> None:
    """Structured row-slice boundary verification (no global regex scans)."""
    full_text: str = proj.full_text()
    rows = list(proj.rows)
    stats["thought_rows_total"] += len(rows)

    if not rows:
        if full_text != "":
            failures.append(f"{tag} EMPTY_ROWS_NONEMPTY_FULL_TEXT len={len(full_text)}")
        return

    # Verify StateTape / row.text immutability against fresh display-state parsing.
    expected_texts: list[tuple[str, str]] = []
    for entry in proj.tape.entries:
        parsed_s = ve._robin_parse_display_state(entry.raw_bytes)
        if parsed_s.text_s4:
            expected_texts.append(("text", parsed_s.text_s4))
        for ti in parsed_s.tool_items:
            expected_texts.append(("tool", ti.display_text))
    if len(expected_texts) != len(rows):
        failures.append(
            f"{tag} ROW_COUNT_MISMATCH expected={len(expected_texts)} got={len(rows)}"
        )
    else:
        for idx, ((ekind, etxt), row) in enumerate(
            zip(expected_texts, rows, strict=True)
        ):
            if row.kind != ekind or row.text != etxt:
                failures.append(f"{tag} ROW_MUTATED row={idx} kind={row.kind}")
                return

    block_starts: set[int] = set(getattr(proj, "_block_start_row_indices", {0}))

    first_text: str = rows[0].text
    if not full_text.startswith(first_text):
        failures.append(f"{tag} ROW0_MISMATCH")
        return
    pos = len(first_text)

    for idx in range(1, len(rows)):
        prev = rows[idx - 1]
        row = rows[idx]
        if idx in block_starts:
            expected_sep = ""
            boundary_kind = "block_start"
        elif row.kind == "tool" or prev.kind == "tool":
            expected_sep = "  \n"
            if prev.kind == "text" and row.kind == "tool":
                stats["text_to_tool_boundaries_checked"] += 1
                boundary_kind = "text_to_tool"
            elif prev.kind == "tool" and row.kind == "text":
                stats["tool_to_text_boundaries_checked"] += 1
                boundary_kind = "tool_to_text"
            else:
                stats["tool_to_tool_boundaries_checked"] += 1
                boundary_kind = "tool_to_tool"
        else:
            expected_sep = _reference_text_separator(prev.text)
            stats["text_to_text_boundaries_checked"] += 1
            boundary_kind = "text_to_text"

        if full_text.startswith(expected_sep + row.text, pos):
            actual_sep = expected_sep
            start_i = pos + len(actual_sep)
            end_i = start_i + len(row.text)
        else:
            found_at = full_text.find(row.text, pos, pos + 16)
            if found_at < 0:
                failures.append(
                    f"{tag} ROW_SLICE_MISSING row={idx} kind={boundary_kind}"
                )
                return
            actual_sep = full_text[pos:found_at]
            start_i = found_at
            end_i = start_i + len(row.text)
            failures.append(
                f"{tag} BOUNDARY_SEP_MISMATCH row={idx} kind={boundary_kind} "
                f"expected_len={len(expected_sep)} got_len={len(actual_sep)} "
                f"has_nl={int(chr(10) in actual_sep)}"
            )

        if boundary_kind == "text_to_text":
            if (
                "\n" in actual_sep
                or actual_sep not in ("", " ", "。", ". ")
                or len(actual_sep) > 3
            ):
                failures.append(
                    f"{tag} INVALID_TEXT_SEP row={idx} len={len(actual_sep)} "
                    f"has_nl={int(chr(10) in actual_sep)}"
                )

        if full_text[start_i:end_i] != row.text:
            failures.append(f"{tag} ROW_SLICE_CONTENT_MISMATCH row={idx}")
            return
        pos = end_i

    if pos != len(full_text):
        failures.append(
            f"{tag} TRAILING_OFFSET_MISMATCH end_last={pos} full_len={len(full_text)}"
        )


def run_har_corpus(har_dir: Path, voice_edge_path: Path) -> int:
    """Replay all HAR files in `har_dir` and validate structured thought segmentation."""
    if not har_dir.is_dir():
        print("FAIL: har directory not found")
        return 1
    har_files = sorted(har_dir.glob("*.har"))
    if not har_files:
        print("FAIL: har_files=0 (insufficient coverage)")
        return 1

    ve, tmp_dir = _load_voice_edge_module(voice_edge_path)
    stats: dict[str, int] = {
        "har_files": len(har_files),
        "pq_entries": 0,
        "pq_aborted_empty_entries": 0,
        "lct_entries": 0,
        "thought_rows_total": 0,
        "text_to_text_boundaries_checked": 0,
        "text_to_tool_boundaries_checked": 0,
        "tool_to_text_boundaries_checked": 0,
        "tool_to_tool_boundaries_checked": 0,
    }
    failures: list[str] = []

    try:
        for har_path in har_files:
            raw_har_bytes = har_path.read_bytes()
            har_id = hashlib.sha256(raw_har_bytes).hexdigest()[:12]
            try:
                har_obj = json.loads(raw_har_bytes.decode("utf-8"))
            except Exception:
                failures.append(f"har={har_id} INVALID_HAR_JSON")
                continue
            entries = (har_obj.get("log") or {}).get("entries") or []
            for e_idx, entry in enumerate(entries):
                req_url = str((entry.get("request") or {}).get("url") or "")
                matched_rpc = ""
                for rpc in _TARGET_RPCS:
                    if rpc in req_url:
                        matched_rpc = rpc
                        break
                if not matched_rpc:
                    continue

                tag = f"har={har_id} idx={e_idx} rpc={matched_rpc.split('/')[-1]}"
                raw_body, dec_err = _decode_har_response_bytes(entry, matched_rpc)
                if dec_err == "PQ_ABORTED_EMPTY_STREAM":
                    stats["pq_aborted_empty_entries"] += 1
                    continue
                if dec_err or raw_body is None:
                    failures.append(f"{tag} DECODE_FAIL reason={dec_err}")
                    continue

                frames, unpack_err = _strict_unpack_grpc_frames(raw_body)
                if unpack_err:
                    failures.append(f"{tag} GRPC_UNPACK_FAIL reason={unpack_err}")
                    continue

                pb_failed = False
                for f_idx, frame in enumerate(frames):
                    pb_err = _strict_validate_protobuf(frame)
                    if pb_err:
                        failures.append(
                            f"{tag} frame={f_idx} PROTOBUF_FAIL reason={pb_err}"
                        )
                        pb_failed = True
                        break
                if pb_failed:
                    continue

                if matched_rpc == "RobinService/ProcessQuery":
                    stats["pq_entries"] += 1
                    # Replay frame-by-frame via StateTape + ThoughtProjector (streaming)
                    tape_stream = ve._RobinStateTape()
                    proj_stream = ve._RobinThoughtProjector(tape_stream)
                    stream_deltas: list[str] = []
                    prev_full = ""

                    # Also replay via batched consume_delta every 3 frames
                    tape_batch = ve._RobinStateTape()
                    proj_batch = ve._RobinThoughtProjector(tape_batch)
                    batch_deltas: list[str] = []

                    # Also drive production _RobinStreamDecoder to confirm zero crash & matching projector
                    dec = ve._RobinStreamDecoder(
                        f"chatcmpl-har-{har_id}-{e_idx}",
                        1700000000,
                        "gemini:gemini-4-argon",
                    )

                    for f_idx, frame in enumerate(frames):
                        parsed = ve._robin_parse_process_query_response(frame)
                        resp_id = str(parsed.get("response_id") or "")
                        for live_raw in parsed.get("live_states_raw") or []:
                            tape_stream.append_live(live_raw, response_id=resp_id)
                            tape_batch.append_live(live_raw, response_id=resp_id)
                        hist_raw = parsed.get("history_states_raw") or []
                        if hist_raw:
                            tape_stream.reconcile_terminal_history(
                                hist_raw, response_id=resp_id
                            )
                            tape_batch.reconcile_terminal_history(
                                hist_raw, response_id=resp_id
                            )
                        cur_full = proj_stream.full_text()
                        if not cur_full.startswith(prev_full):
                            failures.append(f"{tag} frame={f_idx} NON_MONOTONIC_PREFIX")
                        prev_full = cur_full
                        stream_deltas.append(proj_stream.consume_delta())
                        if (f_idx + 1) % 3 == 0:
                            batch_deltas.append(proj_batch.consume_delta())
                        dec.feed_pq_frame(frame)

                    batch_deltas.append(proj_batch.consume_delta())
                    final_full = proj_stream.full_text()
                    if "".join(stream_deltas) != final_full:
                        failures.append(f"{tag} STREAM_DELTA_MISMATCH")
                    if "".join(batch_deltas) != final_full:
                        failures.append(f"{tag} BATCH_DELTA_MISMATCH")
                    if dec.run.thought_projector.full_text() != final_full:
                        failures.append(f"{tag} DECODER_PROJECTOR_MISMATCH")
                    _verify_projected_boundaries(
                        ve,
                        proj_stream,
                        tag=tag,
                        stats=stats,
                        failures=failures,
                    )
                else:
                    stats["lct_entries"] += 1
                    for _f_idx, frame in enumerate(frames):
                        turns = ve._robin_parse_conversation_turns(frame)
                        for t_idx, turn in enumerate(turns):
                            hist_raw = turn.get("history_states_raw") or []
                            if not hist_raw:
                                continue
                            resp_id = str(turn.get("response_id") or "")
                            tape_lct = ve._RobinStateTape()
                            proj_lct = ve._RobinThoughtProjector(tape_lct)
                            tape_lct.reconcile_terminal_history(
                                hist_raw, response_id=resp_id
                            )

                            # Incremental equivalence for LCT states
                            tape_inc = ve._RobinStateTape()
                            proj_inc = ve._RobinThoughtProjector(tape_inc)
                            inc_deltas: list[str] = []
                            prev_inc = ""
                            for raw_s in hist_raw:
                                tape_inc.append_live(raw_s, response_id=resp_id)
                                cur_inc = proj_inc.full_text()
                                if not cur_inc.startswith(prev_inc):
                                    failures.append(
                                        f"{tag} turn={t_idx} LCT_NON_MONOTONIC_PREFIX"
                                    )
                                prev_inc = cur_inc
                                inc_deltas.append(proj_inc.consume_delta())
                            if "".join(inc_deltas) != proj_lct.full_text():
                                failures.append(
                                    f"{tag} turn={t_idx} LCT_STREAM_ONESHOT_MISMATCH"
                                )
                            _verify_projected_boundaries(
                                ve,
                                proj_lct,
                                tag=f"{tag} turn={t_idx}",
                                stats=stats,
                                failures=failures,
                            )
    finally:
        shutil.rmtree(tmp_dir, ignore_errors=True)

    if stats["text_to_text_boundaries_checked"] == 0:
        failures.append("INSUFFICIENT_COVERAGE: text_to_text_boundaries_checked == 0")

    print(
        "HAR_REPLAY_SUMMARY "
        + " ".join(f"{k}={v}" for k, v in stats.items())
        + f" failures={len(failures)}"
    )
    for item in failures[:40]:
        print(f"FAIL {item}")
    if len(failures) > 40:
        print(f"FAIL ... ({len(failures) - 40} more failures redacted)")
    return 0 if not failures else 1


# ==================== Compliance Sentinel ====================

_SAFE_SYNTHETIC_TOKENS = (
    "selftest",
    "test_",
    "_test",
    "00000000-0000-4000-8000-000000000001",
)

_COMPLIANCE_RULES: tuple[tuple[str, re.Pattern[str], bool], ...] = (
    (
        "HAR_FILENAME",
        re.compile(r"[A-Za-z0-9_.-]+\.har\b", re.IGNORECASE),
        False,
    ),
    (
        "ENTRY_INDEX",
        re.compile(r"\bentry\s*(?:\[\s*\d+\s*\]|#\s*\d+\b|\s+\d+\b)", re.IGNORECASE),
        False,
    ),
    (
        "DATE_LITERAL",
        re.compile(
            r"\b20\d{2}[-_/]\d{2}[-_/]\d{2}\b|"
            r"(?<![\d.])20\d{2}(?:0[1-9]|1[0-2])(?:0[1-9]|[12]\d|3[01])(?![\d.])"
        ),
        False,
    ),
    (
        "LOCAL_HOME_PATH",
        re.compile(
            r"(?:/Users/|/home/|C:\\Users\\|/var/folders/|/private/tmp/)[A-Za-z0-9_.-]+"
        ),
        False,
    ),
    (
        "CREDENTIAL_TOKEN",
        re.compile(
            r"\b(?:ya29\.[A-Za-z0-9_-]{10,}|1//[A-Za-z0-9_-]{10,}|"
            r"__Secure-1PSID\s*=\s*[^\s;\"']+|SAPISID\s*=\s*[^\s;\"']+)"
        ),
        False,
    ),
    (
        "COMMENT_EXTERNAL_VERSION",
        re.compile(r"\bv?\d+\.\d+\.\d+(?:\.\d+)?\b"),
        True,
    ),
    (
        "COMMENT_BINARY_OFFSET",
        re.compile(r"\b0x[0-9a-fA-F]{4,}\b"),
        True,
    ),
    (
        "REAL_SESSION_ID",
        re.compile(r"\b(?:c|r|rc)_[0-9a-f]{12,}\b"),
        False,
    ),
    (
        "CAPTURE_BLOB_OR_CACHE_KEY",
        re.compile(r"/contrib_service/ttl_\w+/[A-Za-z0-9_-]{16,}"),
        False,
    ),
)

_ENV_REGISTRY_INDEX_LINE_RE = re.compile(r"^#\s+[A-Z][A-Z0-9_]{2,}\s+.+\s+L\d+\s*$")
_IPV4_ADDR_RE = re.compile(r"^(?:\d{1,3}\.){3}\d{1,3}$")


def _extract_comment_or_doc_text(line: str, in_doc: str) -> tuple[str, str]:
    """Extract `# ...` comment text (outside quotes) and single/multi-line docstring text."""
    parts: list[str] = []
    i = 0
    n = len(line)
    if in_doc:
        close_idx = line.find(in_doc)
        if close_idx < 0:
            return line, in_doc
        parts.append(line[:close_idx])
        i = close_idx + 3
        in_doc = ""

    in_quote = ""
    while i < n:
        ch = line[i]
        if in_quote:
            if ch == "\\" and i + 1 < n:
                i += 2
                continue
            if ch == in_quote:
                in_quote = ""
            i += 1
            continue
        if ch == "#":
            parts.append(line[i:])
            break
        if line[i : i + 3] in ('"""', "'''"):
            delim = line[i : i + 3]
            before = line[:i].strip()
            is_doc_start = before == "" or before.lower() in ("r", "u", "f", "rf", "fr")
            close_idx = line.find(delim, i + 3)
            if close_idx >= 0:
                if is_doc_start:
                    parts.append(line[i + 3 : close_idx])
                i = close_idx + 3
                continue
            if is_doc_start:
                parts.append(line[i + 3 :])
                return "\n".join(parts), delim
            i += 3
            continue
        if ch in ('"', "'"):
            in_quote = ch
        i += 1
    return "\n".join(parts), in_doc


def _scan_lines(label: str, indexed_lines: list[tuple[int, str]]) -> list[str]:
    """Scan `(lineno, text)` pairs and return redacted violation records."""
    violations: list[str] = []
    in_doc = ""
    prev_lineno = -1
    for lineno, line in indexed_lines:
        if prev_lineno >= 0 and lineno != prev_lineno + 1:
            in_doc = ""
        prev_lineno = lineno
        comment_text, in_doc = _extract_comment_or_doc_text(line, in_doc)
        is_env_registry_row = bool(_ENV_REGISTRY_INDEX_LINE_RE.match(line.strip()))
        for rule_name, pattern, comments_only in _COMPLIANCE_RULES:
            if comments_only:
                if not comment_text or (
                    rule_name == "COMMENT_EXTERNAL_VERSION" and is_env_registry_row
                ):
                    continue
                target_text = comment_text
            else:
                target_text = line
            for m in pattern.finditer(target_text):
                matched = m.group(0)
                if rule_name == "COMMENT_EXTERNAL_VERSION" and _IPV4_ADDR_RE.match(
                    matched
                ):
                    continue
                low_match = matched.lower()
                if rule_name in (
                    "REAL_SESSION_ID",
                    "CAPTURE_BLOB_OR_CACHE_KEY",
                ) and any(tok in low_match for tok in _SAFE_SYNTHETIC_TOKENS):
                    continue
                violations.append(
                    f"VIOLATION file={label} line={lineno} rule={rule_name} match_len={len(matched)}"
                )
    return violations


def _added_lines_against_baseline(
    baseline_path: Path, current_path: Path
) -> list[tuple[int, str]]:
    """Return `(lineno, line)` for all lines added relative to `baseline_path`."""
    base_lines = baseline_path.read_text(encoding="utf-8").splitlines()
    curr_lines = current_path.read_text(encoding="utf-8").splitlines()
    diff = difflib.unified_diff(base_lines, curr_lines, n=0)
    added: list[tuple[int, str]] = []
    curr_lineno = 0
    hunk_re = re.compile(r"^@@ -\d+(?:,\d+)? \+(\d+)(?:,\d+)? @@")
    for dl in diff:
        if dl.startswith(("+++", "---")):
            continue
        m = hunk_re.match(dl)
        if m:
            curr_lineno = int(m.group(1))
            continue
        if dl.startswith("+"):
            added.append((curr_lineno, dl[1:]))
            curr_lineno += 1
    return added


def run_compliance_selftest() -> int:
    """Dynamically build positive/negative sentinel cases at runtime without literal leakage."""
    slash = chr(47)
    positive_cases: list[tuple[str, list[tuple[int, str]]]] = [
        ("HAR_FILENAME", [(1, "# ref " + "capture" + "_sample" + ".h" + "ar")]),
        ("ENTRY_INDEX", [(1, "# see " + "ent" + "ry" + "[" + "6" + "2]")]),
        ("ENTRY_INDEX", [(1, "# see " + "ent" + "ry" + " " + "8" + "1")]),
        ("DATE_LITERAL", [(1, "# updated " + "20" + "26" + "-" + "11" + "-" + "26")]),
        ("DATE_LITERAL", [(1, "stamp = '" + "20" + "26" + "11" + "26" + "'")]),
        (
            "LOCAL_HOME_PATH",
            [(1, "p = '" + slash + "Us" + "ers" + slash + "alice" + slash + "repo'")],
        ),
        (
            "LOCAL_HOME_PATH",
            [
                (
                    1,
                    "p = '"
                    + slash
                    + "var"
                    + slash
                    + "folders"
                    + slash
                    + "ab"
                    + slash
                    + "tmp_dir'",
                )
            ],
        ),
        (
            "LOCAL_HOME_PATH",
            [
                (
                    1,
                    "p = '" + slash + "private" + slash + "tmp" + slash + "demo_file'",
                )
            ],
        ),
        ("CREDENTIAL_TOKEN", [(1, "tok = '" + "ya2" + "9." + "a" * 16 + "'")]),
        ("CREDENTIAL_TOKEN", [(1, "ck = '" + "__Secure-" + "1PSID=" + "x" * 12 + "'")]),
        ("COMMENT_EXTERNAL_VERSION", [(1, "# client ver " + "1." + "86." + "0")]),
        (
            "COMMENT_EXTERNAL_VERSION",
            [
                (1, '"""Doc header'),
                (2, "    multiline ver " + "2." + "3." + "4"),
                (3, '"""'),
            ],
        ),
        ("COMMENT_BINARY_OFFSET", [(1, "# offset " + "0" + "x" + "1a2b3c")]),
        ("REAL_SESSION_ID", [(1, "cid = '" + "c_" + "a1b2c3d4e5f60708" + "'")]),
        (
            "CAPTURE_BLOB_OR_CACHE_KEY",
            [
                (
                    1,
                    "b = '"
                    + slash
                    + "contrib_service"
                    + slash
                    + "ttl_1d"
                    + slash
                    + "Zx9Yq8Wp7Vo6Un5Tm4'",
                )
            ],
        ),
    ]
    for expected_rule, synthetic_lines in positive_cases:
        found = _scan_lines("selftest_pos", synthetic_lines)
        if not any(f"rule={expected_rule}" in v for v in found):
            print(f"COMPLIANCE_SELFTEST_FAIL expected={expected_rule}")
            return 1

    negative_cases: list[str] = [
        'cid = "c_test_01"',
        'uid = "00000000-0000-4000-8000-000000000001"',
        'fname = "test.pdf"',
        'blob = "/contrib_service/ttl_1d/selftest_blob_0001"',
        'glob_pat = "*.har"',
        'ws = "/w/workspace/sample.py"',
        "# protocol field S.4 carries thought text",
        'PROTO_CONST = "1.2.3"  # plain trailing note without version',
        "num = 20261345",
    ]
    for idx, clean_line in enumerate(negative_cases, start=1):
        found = _scan_lines("selftest_neg", [(idx, clean_line)])
        if found:
            print(f"COMPLIANCE_SELFTEST_FALSE_POSITIVE case={idx} {found[0]}")
            return 1

    print(
        f"COMPLIANCE_SELFTEST_PASS positives={len(positive_cases)} negatives={len(negative_cases)}"
    )
    return 0


def run_compliance_scan(baseline_path: Path, voice_edge_path: Path) -> int:
    """Scan added lines of `voice_edge.py` against baseline plus full harness source."""
    harness_path = Path(__file__).resolve()
    if not baseline_path.is_file():
        print("FAIL: baseline file not found")
        return 1

    ve_added = _added_lines_against_baseline(baseline_path, voice_edge_path)
    harness_lines = [
        (i, line)
        for i, line in enumerate(
            harness_path.read_text(encoding="utf-8").splitlines(), start=1
        )
    ]

    violations = _scan_lines("voice_edge.py(added)", ve_added) + _scan_lines(
        "test/gemini_thought_segmentation_har_harness.py", harness_lines
    )
    print(
        f"COMPLIANCE_SCAN_SUMMARY ve_added_lines={len(ve_added)} "
        f"harness_lines={len(harness_lines)} violations={len(violations)}"
    )
    for v in violations:
        print(v)
    return 0 if not violations else 1


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Robin thought-segmentation HAR replay harness & compliance sentinel"
    )
    parser.add_argument(
        "--har-dir",
        type=Path,
        default=_REPO_ROOT / ".memory" / "har",
        help="Directory containing HAR files to scan",
    )
    parser.add_argument(
        "--voice-edge-path",
        type=Path,
        default=_REPO_ROOT / "voice_edge.py",
        help="Path to voice_edge.py module under test",
    )
    parser.add_argument(
        "--compliance-sentinel",
        action="store_true",
        help="Run compliance sentinel self-tests and incremental scan (with --baseline-voice-edge)",
    )
    parser.add_argument(
        "--baseline-voice-edge",
        type=Path,
        default=None,
        help="Baseline voice_edge.py path for incremental compliance scan",
    )
    args = parser.parse_args(argv)

    if args.compliance_sentinel:
        rc = run_compliance_selftest()
        if rc != 0:
            return rc
        if args.baseline_voice_edge is not None:
            return run_compliance_scan(args.baseline_voice_edge, args.voice_edge_path)
        return 0

    rc = run_har_corpus(args.har_dir, args.voice_edge_path)
    if rc != 0:
        return rc
    rc = run_compliance_selftest()
    if rc != 0:
        return rc
    if args.baseline_voice_edge is not None:
        return run_compliance_scan(args.baseline_voice_edge, args.voice_edge_path)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
