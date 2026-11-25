#!/usr/bin/env python3
"""HAR corpus non-introduction harness and compliance scanner for Robin `[invalid URL]` fixes."""

from __future__ import annotations

import argparse
import base64
import difflib
import gzip
import hashlib
import json
import os
import re
import struct
import sys
import tempfile
from pathlib import Path
from typing import Any

_REPO_ROOT = Path(__file__).resolve().parents[1]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))


def _load_voice_edge_module() -> Any:
    """Import `voice_edge` with isolated temporary state storage."""
    tmp_dir = Path(tempfile.mkdtemp(prefix="ve_har_harness_state_"))
    os.environ.setdefault(
        "VE_GEMINI_STATE_DB", str(tmp_dir / "gemini_dsh_state.sqlite3")
    )
    os.environ.setdefault(
        "VE_ROBIN_SESSION_CACHE_FILE", str(tmp_dir / "robin_session_cache.json")
    )
    import importlib

    return importlib.import_module("voice_edge")


_TARGET_RPCS = (
    "RobinService/ProcessQuery",
    "RobinService/ListConversationTurns",
)
_PSEUDO_TLD_IDENT_RE = re.compile(
    r"\b[A-Za-z_][A-Za-z0-9_]*\.(?:py|sh|io|id|in|md|ts|js|rs|go|is_closed)\b"
)
_INVALID_URL_RE = re.compile(r"\[invalid URL\]", re.IGNORECASE)
_MD_LINK_TARGET_RE = re.compile(r"\]\(([^)\r\n]+)\)")


def _decode_har_response_bytes(content: dict[str, Any]) -> tuple[bytes | None, str]:
    """Decode HAR response content strictly without silent skips.

    Returns `(raw_bytes, error_reason)`. `raw_bytes is None` with `error_reason == "EMPTY_BODY"`
    represents a valid empty HTTP response body; any non-empty error_reason is a hard failure.
    """
    text = content.get("text")
    encoding = str(content.get("encoding") or "").strip().lower()
    if text is None or text == "":
        return None, "EMPTY_BODY"
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


def _count_literal_discussion_markers(ve: Any, text: str) -> int:
    """Count standalone `[invalid URL]` occurrences outside local Markdown link forms."""
    masked = ve._ROBIN_INVALID_URL_MD_LINK_RE.sub(lambda m: " " * len(m.group(0)), text)
    return len(_INVALID_URL_RE.findall(masked))


def _count_code_identifiers(text: str) -> dict[str, int]:
    """Count pseudo-TLD code identifiers outside Markdown link targets."""
    masked = _MD_LINK_TARGET_RE.sub("]()", text)
    counts: dict[str, int] = {}
    for m in _PSEUDO_TLD_IDENT_RE.finditer(masked):
        start, end = m.span()
        ctx = masked[max(0, start - 20) : min(len(masked), end + 20)].lower()
        if "invalid url" in ctx:
            continue
        tok = m.group(0)
        counts[tok] = counts.get(tok, 0) + 1
    return counts


def _stream_with_cuts(ve: Any, text: str, cuts: list[int]) -> str:
    """Feed cumulative snapshots at sorted cut offsets through AnswerProjector + ActionCardFilter."""
    proj = ve._RobinAnswerProjector()
    filt = ve._RobinActionCardFilter()
    out: list[str] = []
    prev = 0
    for cut in cuts:
        if cut <= prev or cut > len(text):
            continue
        prev = cut
        delta = proj.update_snapshot(text[:cut])
        if delta:
            vis = filt.feed(delta)
            if vis:
                out.append(vis)
    if prev < len(text):
        delta = proj.update_snapshot(text)
        if delta:
            vis = filt.feed(delta)
            if vis:
                out.append(vis)
    tail = proj.flush()
    if tail:
        vis = filt.feed(tail)
        if vis:
            out.append(vis)
    end_vis = filt.flush()
    if end_vis:
        out.append(end_vis)
    return "".join(out)


def _compute_boundary_cuts(ve: Any, text: str) -> list[int]:
    """Compute targeted split offsets around markers, URLs, links, and code identifiers."""
    cuts_set: set[int] = set()
    n = len(text)
    if n == 0:
        return []
    for step in (17, 64):
        for i in range(step, n, step):
            cuts_set.add(i)
    tokens = (
        "[invalid URL]",
        "[invalid url]",
        "](",
        "%23",
        "%20",
        "#L",
        *ve._ROBIN_GOOGLE_SEARCH_REDIRECT_PREFIXES,
        *ve.GEMINI_ROBIN_ACTION_CARD_MARKERS,
    )
    low = text.lower()
    for tok in tokens:
        tlow = tok.lower()
        start = 0
        while True:
            idx = low.find(tlow, start)
            if idx < 0:
                break
            for off in (-2, -1, 0, 1, 2, len(tok) // 2, len(tok) - 1, len(tok)):
                pos = idx + off
                if 0 < pos < n:
                    cuts_set.add(pos)
            start = idx + 1
    for m in _PSEUDO_TLD_IDENT_RE.finditer(text):
        s, e = m.span()
        mid = (s + e) // 2
        for pos in (s, mid, e):
            if 0 < pos < n:
                cuts_set.add(pos)
    cuts_set.add(n)
    return sorted(cuts_set)


def _first_diff_offset(a: str, b: str) -> int:
    limit = min(len(a), len(b))
    for i in range(limit):
        if a[i] != b[i]:
            return i
    return limit


def _verify_text_pipeline(
    ve: Any,
    *,
    har_hash: str,
    entry_idx: int,
    rpc_name: str,
    field_path: str,
    snapshots: list[str],
    failures: list[str],
) -> tuple[bool, int]:
    """Run whole-text, frame-streamed, and boundary-split checks without leaking raw text."""
    if not snapshots:
        return True, 0
    final_raw = snapshots[-1]
    full_out, _ = ve._robin_strip_action_card_markers(final_raw)

    # 1. Streamed frame-by-frame (when multiple cumulative snapshots exist):
    proj = ve._RobinAnswerProjector()
    filt = ve._RobinActionCardFilter()
    frame_parts: list[str] = []
    for snap in snapshots:
        d = proj.update_snapshot(snap)
        if d:
            vis = filt.feed(d)
            if vis:
                frame_parts.append(vis)
    tail = proj.flush()
    if tail:
        vis = filt.feed(tail)
        if vis:
            frame_parts.append(vis)
    end_vis = filt.flush()
    if end_vis:
        frame_parts.append(end_vis)
    streamed_out = "".join(frame_parts)

    # 2. Boundary-split + flush on final_raw:
    boundary_cuts = _compute_boundary_cuts(ve, final_raw)
    boundary_out = _stream_with_cuts(ve, final_raw, boundary_cuts)

    ok = True
    if streamed_out != full_out:
        ok = False
        off = _first_diff_offset(streamed_out, full_out)
        failures.append(
            f"FAIL har={har_hash} entry={entry_idx} rpc={rpc_name} path={field_path} "
            f"cat=STREAM_FULL_MISMATCH offset={off} len_stream={len(streamed_out)} len_full={len(full_out)}"
        )
    if boundary_out != full_out:
        ok = False
        off = _first_diff_offset(boundary_out, full_out)
        failures.append(
            f"FAIL har={har_hash} entry={entry_idx} rpc={rpc_name} path={field_path} "
            f"cat=BOUNDARY_FULL_MISMATCH offset={off} len_boundary={len(boundary_out)} len_full={len(full_out)}"
        )

    raw_inv = len(_INVALID_URL_RE.findall(final_raw))
    out_inv = len(_INVALID_URL_RE.findall(full_out))
    if out_inv > raw_inv:
        ok = False
        failures.append(
            f"FAIL har={har_hash} entry={entry_idx} rpc={rpc_name} path={field_path} "
            f"cat=INTRODUCED_INVALID_URL raw_count={raw_inv} out_count={out_inv}"
        )

    raw_lit = _count_literal_discussion_markers(ve, final_raw)
    out_lit = _count_literal_discussion_markers(ve, full_out)
    if out_lit != raw_lit:
        ok = False
        lit_cat = (
            "DROPPED_LITERAL_DISCUSSION"
            if out_lit < raw_lit
            else "INTRODUCED_LITERAL_INVALID_URL"
        )
        failures.append(
            f"FAIL har={har_hash} entry={entry_idx} rpc={rpc_name} path={field_path} "
            f"cat={lit_cat} raw_lit={raw_lit} out_lit={out_lit}"
        )

    raw_idents = _count_code_identifiers(final_raw)
    out_idents = _count_code_identifiers(full_out)
    for ident_key, raw_cnt in raw_idents.items():
        out_cnt = out_idents.get(ident_key, 0)
        if out_cnt < raw_cnt:
            ok = False
            failures.append(
                f"FAIL har={har_hash} entry={entry_idx} rpc={rpc_name} path={field_path} "
                f"cat=CODE_IDENT_DEGRADED ident_len={len(ident_key)} raw_cnt={raw_cnt} out_cnt={out_cnt}"
            )
            break

    return ok, raw_lit


def run_har_corpus(har_dir: Path) -> int:
    """Execute Group 4 full HAR corpus non-introduction verification."""
    ve = _load_voice_edge_module()
    har_files = sorted(har_dir.glob("*.har")) if har_dir.is_dir() else []
    if not har_files:
        print("FAIL: zero HAR files discovered in target directory")
        return 1

    total_rpcs = 0
    pass_count = 0
    fail_count = 0
    empty_body_count = 0
    non_grpc_http_error_count = 0
    literal_discussion_total = 0
    failures: list[str] = []

    for har_path in har_files:
        raw_bytes = har_path.read_bytes()
        har_hash = hashlib.sha256(raw_bytes).hexdigest()[:12]
        try:
            doc = json.loads(raw_bytes.decode("utf-8"))
            entries = doc["log"]["entries"]
        except Exception as exc:
            fail_count += 1
            failures.append(
                f"FAIL har={har_hash} entry=-1 rpc=HAR_PARSE cat=JSON_ERROR err={type(exc).__name__}"
            )
            continue

        for entry_idx, entry in enumerate(entries):
            url = str(entry.get("request", {}).get("url") or "")
            rpc_name = ""
            for cand in _TARGET_RPCS:
                if cand in url:
                    rpc_name = cand.rsplit("/", 1)[-1]
                    break
            if not rpc_name:
                continue
            total_rpcs += 1

            resp = entry.get("response") or {}
            status = int(resp.get("status") or 0)
            content = resp.get("content") or {}
            body_bytes, err_reason = _decode_har_response_bytes(content)
            if err_reason == "EMPTY_BODY":
                empty_body_count += 1
                continue
            if err_reason or body_bytes is None:
                fail_count += 1
                failures.append(
                    f"FAIL har={har_hash} entry={entry_idx} rpc={rpc_name} path=http.body "
                    f"cat={err_reason} status={status}"
                )
                continue

            frames, frame_err = _strict_unpack_grpc_frames(body_bytes)
            if frame_err:
                # Non-200 HTTP responses with non-gRPC content-type are tracked independently.
                mime = str(content.get("mimeType") or "").lower()
                if status >= 400 and "grpc" not in mime:
                    non_grpc_http_error_count += 1
                    continue
                fail_count += 1
                failures.append(
                    f"FAIL har={har_hash} entry={entry_idx} rpc={rpc_name} path=grpc.frame "
                    f"cat={frame_err} bytes={len(body_bytes)}"
                )
                continue

            entry_ok = True
            if rpc_name == "ProcessQuery":
                snapshots: list[str] = []
                for f_idx, frame in enumerate(frames):
                    try:
                        parsed = ve._robin_parse_process_query_response(frame)
                    except Exception as exc:
                        entry_ok = False
                        failures.append(
                            f"FAIL har={har_hash} entry={entry_idx} rpc={rpc_name} path=1003.1.1.2.1 "
                            f"cat=PROTOBUF_PARSE_ERROR frame={f_idx} err={type(exc).__name__}"
                        )
                        break
                    ans = str(parsed.get("answer_text") or "")
                    if ans and (not snapshots or len(ans) >= len(snapshots[-1])):
                        snapshots.append(ans)
                if entry_ok and snapshots:
                    ok_pipe, lit_cnt = _verify_text_pipeline(
                        ve,
                        har_hash=har_hash,
                        entry_idx=entry_idx,
                        rpc_name=rpc_name,
                        field_path="1003.1.1.2.1",
                        snapshots=snapshots,
                        failures=failures,
                    )
                    literal_discussion_total += lit_cnt
                    entry_ok = ok_pipe
            else:
                try:
                    turns = ve._robin_parse_conversation_turns(body_bytes)
                except Exception as exc:
                    entry_ok = False
                    failures.append(
                        f"FAIL har={har_hash} entry={entry_idx} rpc={rpc_name} path=1.4.1 "
                        f"cat=PROTOBUF_PARSE_ERROR err={type(exc).__name__}"
                    )
                    turns = []
                for t_idx, turn in enumerate(turns):
                    ans = str(turn.get("answer_text") or "")
                    if not ans:
                        continue
                    ok_pipe, lit_cnt = _verify_text_pipeline(
                        ve,
                        har_hash=har_hash,
                        entry_idx=entry_idx,
                        rpc_name=rpc_name,
                        field_path=f"1.4.1.turn[{t_idx}]",
                        snapshots=[ans],
                        failures=failures,
                    )
                    literal_discussion_total += lit_cnt
                    if not ok_pipe:
                        entry_ok = False

            if entry_ok:
                pass_count += 1
            else:
                fail_count += 1

    if total_rpcs == 0:
        print(f"FAIL: zero target RPCs discovered across {len(har_files)} HAR files")
        return 1

    print(
        f"HAR_CORPUS_SUMMARY har_files={len(har_files)} total_rpcs={total_rpcs} "
        f"PASS={pass_count} FAIL={fail_count} EMPTY_BODY={empty_body_count} "
        f"NON_GRPC_HTTP_ERROR={non_grpc_http_error_count} "
        f"literal_discussion={literal_discussion_total}"
    )
    for line in failures:
        print(line)
    return 0 if fail_count == 0 else 1


# ==================== Group 5: Compliance Scanner ====================

_SAFE_SYNTHETIC_TOKENS = (
    "selftest",
    "c_test_",
    "r_test_",
    "rc_test_",
    "c_veh_",
    "r_veh_",
    "rc_veh_",
    "c_inf_",
    "test.pdf",
    "sample",
    "dummy",
    "fake",
    "00000000-0000-4000-8000-",
)

_COMPLIANCE_RULES: tuple[tuple[str, re.Pattern[str], bool], ...] = (
    (
        "HAR_FILENAME",
        re.compile(r"(?<![A-Za-z0-9_*.-])[A-Za-z0-9][A-Za-z0-9_.-]{2,}\.har\b"),
        False,
    ),
    (
        "ENTRY_INDEX",
        re.compile(r"\bentry\s*(?:\[\s*\d+\s*\]|#\s*\d+)", re.IGNORECASE),
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
    """Extract `# ...` comment text (outside quotes) and single/multi-line docstring text.

    Returns `(comment_or_doc_text, next_in_doc)` where `next_in_doc` is `'\"\"\"'`,
    `\"'''\"`, or `""` to track multi-line docstring bodies across consecutive lines.
    """
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
    """Dynamically construct positive/negative compliance cases at runtime without literal leakage."""
    slash = chr(47)
    positive_cases: list[tuple[str, list[tuple[int, str]]]] = [
        (
            "HAR_FILENAME",
            [(1, "# ref " + "capture" + "_sample" + ".h" + "ar")],
        ),
        (
            "ENTRY_INDEX",
            [(1, "# see " + "ent" + "ry" + "[" + "6" + "2]")],
        ),
        (
            "DATE_LITERAL",
            [(1, "# updated " + "20" + "26" + "-" + "11" + "-" + "26")],
        ),
        (
            "DATE_LITERAL",
            [(1, "stamp = '" + "20" + "26" + "11" + "26" + "'")],
        ),
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
        (
            "CREDENTIAL_TOKEN",
            [(1, "tok = '" + "ya2" + "9." + "a" * 16 + "'")],
        ),
        (
            "CREDENTIAL_TOKEN",
            [(1, "ck = '" + "__Secure-" + "1PSID=" + "x" * 12 + "'")],
        ),
        (
            "COMMENT_EXTERNAL_VERSION",
            [(1, "# client version " + "1." + "86." + "0")],
        ),
        (
            "COMMENT_EXTERNAL_VERSION",
            [(1, 'CONST = "ok"  # trailing ver ' + "1." + "86." + "0")],
        ),
        (
            "COMMENT_EXTERNAL_VERSION",
            [
                (1, '"""Header line'),
                (2, "    multiline doc ver " + "2." + "3." + "4"),
                (3, '"""'),
            ],
        ),
        (
            "REAL_SESSION_ID",
            [(1, "cid = '" + "c_" + "a1b2c3d4e5f60708" + "'")],
        ),
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
        'token = "[invalid URL]"',
        'glob_pat = "*.har"',
        'ws = "/w/workspace/sample.py"',
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


def run_compliance_scan(baseline_path: Path) -> int:
    """Scan added lines of `voice_edge.py` against baseline plus full harness file."""
    ve_path = _REPO_ROOT / "voice_edge.py"
    harness_path = Path(__file__).resolve()
    if not baseline_path.is_file():
        print("FAIL: baseline file not found")
        return 1

    ve_added = _added_lines_against_baseline(baseline_path, ve_path)
    harness_lines = [
        (i, line)
        for i, line in enumerate(
            harness_path.read_text(encoding="utf-8").splitlines(), start=1
        )
    ]

    violations = _scan_lines("voice_edge.py(added)", ve_added) + _scan_lines(
        "test/gemini_invalid_url_har_harness.py", harness_lines
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
        description="Robin [invalid URL] HAR regression harness & compliance scanner"
    )
    parser.add_argument(
        "--har-dir",
        type=Path,
        default=_REPO_ROOT / ".memory" / "har",
        help="Directory containing HAR files to scan",
    )
    parser.add_argument(
        "--compliance-selftest",
        action="store_true",
        help="Run runtime-generated positive/negative self-tests for the compliance scanner",
    )
    parser.add_argument(
        "--compliance-scan",
        action="store_true",
        help="Scan voice_edge.py added lines (vs --baseline) and full harness source",
    )
    parser.add_argument(
        "--baseline",
        type=Path,
        default=None,
        help="Baseline voice_edge.py path for incremental compliance scan",
    )
    args = parser.parse_args(argv)

    if args.compliance_selftest:
        return run_compliance_selftest()
    if args.compliance_scan:
        if args.baseline is None:
            print("FAIL: --baseline is required with --compliance-scan")
            return 1
        return run_compliance_scan(args.baseline)
    return run_har_corpus(args.har_dir)


if __name__ == "__main__":
    raise SystemExit(main())
