"""Standalone MFC evidence and resource extractor using only the Python standard library.

Provides syntax-level observation extraction for MFC message maps and Windows
resource definitions while preserving exact source byte spans and conditional
compilation states.
"""
from __future__ import annotations

import ast
import re
from dataclasses import dataclass
from typing import Any

MFC_RULE_VERSION = "1.0.1"

# Explicit, versioned registry of supported standard WM_* message macros.
# Maps macro name -> (event_id, fixed_handler_name).
# Does NOT guess handler names by stripping prefixes arbitrarily.
WM_MACRO_REGISTRY: dict[str, tuple[str, str]] = {
    "ON_WM_CREATE": ("WM_CREATE", "OnCreate"),
    "ON_WM_DESTROY": ("WM_DESTROY", "OnDestroy"),
    "ON_WM_PAINT": ("WM_PAINT", "OnPaint"),
    "ON_WM_SIZE": ("WM_SIZE", "OnSize"),
    "ON_WM_CLOSE": ("WM_CLOSE", "OnClose"),
    "ON_WM_TIMER": ("WM_TIMER", "OnTimer"),
}

# Supported single-ID and range message map macros:
# macro_name -> (event_kind, expected_arity, is_range)
MAP_MACRO_SPECS: dict[str, tuple[str, int, bool]] = {
    "ON_COMMAND": ("command", 2, False),
    "ON_COMMAND_RANGE": ("command_range", 3, True),
    "ON_UPDATE_COMMAND_UI": ("update_command_ui", 2, False),
    "ON_UPDATE_COMMAND_UI_RANGE": ("update_command_ui_range", 3, True),
    "ON_BN_CLICKED": ("bn_clicked", 2, False),
    "ON_CONTROL": ("control", 3, False),
    "ON_CONTROL_RANGE": ("control_range", 4, True),
    "ON_NOTIFY": ("notify", 3, False),
    "ON_NOTIFY_RANGE": ("notify_range", 4, True),
    "ON_MESSAGE": ("message", 2, False),
    "ON_REGISTERED_MESSAGE": ("registered_message", 2, False),
    "ON_THREAD_MESSAGE": ("thread_message", 2, False),
    "ON_REGISTERED_THREAD_MESSAGE": ("registered_thread_message", 2, False),
}

_IDENT_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
_QUALIFIED_IDENT_RE = re.compile(r"^(?:[A-Za-z_][A-Za-z0-9_]*\s*::\s*)*[A-Za-z_][A-Za-z0-9_]*$")
_INT_LITERAL_RE = re.compile(
    r"^[+-]?(?:0[xX][0-9a-fA-F]+|0[bB][01]+|0[0-7]*|[1-9][0-9]*)(?:[uUlL]*)$"
)


@dataclass(frozen=True)
class Token:
    kind: str  # 'ident', 'punct', 'literal', 'other'
    text: str
    start_byte: int
    end_byte: int
    line: int
    end_line: int
    cond_state: str  # 'active', 'inactive', 'unknown'


@dataclass(frozen=True)
class _CondFrame:
    branch_state: str  # 'active', 'inactive', 'unknown'
    seen_active: bool


def _build_line_Lookup(source: bytes) -> list[int]:
    """Return 0-based byte offsets where each 1-based line starts."""
    starts = [0]
    idx = 0
    n = len(source)
    while idx < n:
        nl = source.find(b"\n", idx)
        if nl == -1:
            break
        starts.append(nl + 1)
        idx = nl + 1
    return starts


def _byte_to_line(line_starts: list[int], byte_offset: int) -> int:
    """Binary search 1-based line number for a byte offset."""
    lo = 0
    hi = len(line_starts) - 1
    while lo <= hi:
        mid = (lo + hi) // 2
        if line_starts[mid] <= byte_offset:
            lo = mid + 1
        else:
            hi = mid - 1
    return max(1, lo)


def _Effective_cond(stack: list[_CondFrame]) -> str:
    if not stack:
        return "active"
    states = [f.branch_state for f in stack]
    if "inactive" in states:
        return "inactive"
    if "unknown" in states:
        return "unknown"
    return "active"


def _eval_zero_one_directive(expr_bytes: bytes) -> str:
    """Check if #if / #elif expression is literally 0 or 1 (ignoring comments)."""
    # Strip comments from directive expression
    cleaned = bytearray()
    i = 0
    n = len(expr_bytes)
    while i < n:
        if i + 1 < n and expr_bytes[i : i + 2] == b"//":
            break
        if i + 1 < n and expr_bytes[i : i + 2] == b"/*":
            end = expr_bytes.find(b"*/", i + 2)
            i = n if end == -1 else end + 2
            cleaned.append(ord(" "))
            continue
        cleaned.append(expr_bytes[i])
        i += 1
    text = cleaned.decode("latin1").strip()
    while text.startswith("(") and text.endswith(")"):
        inner = text[1:-1].strip()
        # Verify outer parens actually match each other
        depth = 0
        matched = True
        for ch in inner:
            if ch == "(":
                depth += 1
            elif ch == ")":
                depth -= 1
                if depth < 0:
                    matched = False
                    break
        if matched and depth == 0:
            text = inner
        else:
            break
    if text == "0":
        return "inactive"
    return "unknown"


def _lex_cpp(source: bytes, line_starts: list[int]) -> list[Token]:
    """Tokenize C++ bytes while skipping comments, strings, raw strings, and #define bodies."""
    tokens: list[Token] = []
    cond_stack: list[_CondFrame] = []
    n = len(source)
    i = 0
    at_line_start = True

    def cur_line(pos: int) -> int:
        return _byte_to_line(line_starts, pos)

    while i < n:
        b = source[i]

        # Handle whitespace
        if b in (0x20, 0x09, 0x0B, 0x0C, 0x0D):
            i += 1
            continue
        if b == 0x0A:  # \n
            at_line_start = True
            i += 1
            continue

        # Line continuation outside literals/comments
        if b == 0x5C:  # \
            if i + 1 < n and source[i + 1] == 0x0A:
                i += 2
                continue
            if i + 2 < n and source[i + 1] == 0x0D and source[i + 2] == 0x0A:
                i += 3
                continue

        # Single-line comment
        if b == 0x2F and i + 1 < n and source[i + 1] == 0x2F:  # //
            i += 2
            while i < n:
                if source[i] == 0x5C:
                    if i + 1 < n and source[i + 1] == 0x0A:
                        i += 2
                        continue
                    if i + 2 < n and source[i + 1] == 0x0D and source[i + 2] == 0x0A:
                        i += 3
                        continue
                if source[i] == 0x0A:
                    break
                i += 1
            continue

        # Block comment
        if b == 0x2F and i + 1 < n and source[i + 1] == 0x2A:  # /*
            end = source.find(b"*/", i + 2)
            i = n if end == -1 else end + 2
            continue

        # Preprocessor directive at logical start of line
        if at_line_start and b == 0x23:  # #
            dir_start = i
            i += 1
            # Read full logical line (respecting backslash continuation and block comments)
            while i < n:
                if source[i] == 0x2F and i + 1 < n and source[i + 1] == 0x2F:
                    # Line comment runs to end of physical/continued line
                    i += 2
                    while i < n and source[i] != 0x0A:
                        i += 1
                    break
                if source[i] == 0x2F and i + 1 < n and source[i + 1] == 0x2A:
                    end_c = source.find(b"*/", i + 2)
                    if end_c == -1:
                        i = n
                        break
                    i = end_c + 2
                    continue
                if source[i] == 0x5C:
                    if i + 1 < n and source[i + 1] == 0x0A:
                        i += 2
                        continue
                    if i + 2 < n and source[i + 1] == 0x0D and source[i + 2] == 0x0A:
                        i += 3
                        continue
                if source[i] == 0x0A:
                    break
                i += 1
            dir_bytes = source[dir_start + 1 : i]
            # Parse directive name
            m = re.match(rb"^\s*([A-Za-z_][A-Za-z0-9_]*)(.*)$", dir_bytes, re.DOTALL)
            if m:
                d_name = m.group(1).decode("ascii", errors="ignore")
                d_rest = m.group(2)
                parent_state = _Effective_cond(cond_stack)
                if d_name == "if":
                    if parent_state == "inactive":
                        cond_stack.append(_CondFrame("inactive", True))
                    else:
                        st = _eval_zero_one_directive(d_rest)
                        cond_stack.append(_CondFrame(st, st != "inactive"))
                elif d_name in ("ifdef", "ifndef"):
                    if parent_state == "inactive":
                        cond_stack.append(_CondFrame("inactive", True))
                    else:
                        cond_stack.append(_CondFrame("unknown", True))
                elif d_name == "elif":
                    if cond_stack:
                        top = cond_stack.pop()
                        parent_st = _Effective_cond(cond_stack)
                        if parent_st == "inactive":
                            cond_stack.append(_CondFrame("inactive", True))
                        else:
                            st = _eval_zero_one_directive(d_rest)
                            if st == "inactive":
                                cond_stack.append(_CondFrame("inactive", top.seen_active))
                            else:
                                cond_stack.append(_CondFrame("unknown", True))
                elif d_name == "else":
                    if cond_stack:
                        top = cond_stack.pop()
                        parent_st = _Effective_cond(cond_stack)
                        if parent_st == "inactive":
                            cond_stack.append(_CondFrame("inactive", True))
                        elif top.branch_state == "inactive" and not top.seen_active:
                            # #if 0 ... #else -> unknown if inside unknown parent, else active
                            new_st = "active" if parent_st == "active" else "unknown"
                            cond_stack.append(_CondFrame(new_st, True))
                        else:
                            cond_stack.append(_CondFrame("unknown", True))
                elif d_name == "endif":
                    if cond_stack:
                        cond_stack.pop()
                # Note: #define bodies are automatically skipped because we consumed the logical line!
            at_line_start = True
            continue

        at_line_start = False
        cond = _Effective_cond(cond_stack)

        # Check for raw string literal: R"delim(...)delim" with optional prefix u8, u, U, L
        raw_prefix_len = 0
        if source[i : i + 4] == b'u8R"':
            raw_prefix_len = 3
        elif source[i : i + 3] in (b'uR"', b'UR"', b'LR"'):
            raw_prefix_len = 2
        elif source[i : i + 2] == b'R"':
            raw_prefix_len = 1

        if raw_prefix_len and i + raw_prefix_len < n and source[i + raw_prefix_len] == 0x22:  # "
            start_b = i
            start_ln = cur_line(start_b)
            delim_start = i + raw_prefix_len + 1
            paren_pos = source.find(b"(", delim_start)
            if paren_pos != -1 and (paren_pos - delim_start) <= 16 and b"\n" not in source[delim_start:paren_pos]:
                delim = source[delim_start:paren_pos]
                closing_seq = b")" + delim + b'"'
                end_pos = source.find(closing_seq, paren_pos + 1)
                if end_pos == -1:
                    i = n
                else:
                    i = end_pos + len(closing_seq)
                tokens.append(
                    Token(
                        "literal",
                        source[start_b:i].decode("latin1"),
                        start_b,
                        i,
                        start_ln,
                        cur_line(max(start_b, i - 1)),
                        cond,
                    )
                )
                continue

        # Standard string or char literal (with optional prefix u8, u, U, L)
        str_prefix_len = 0
        if source[i : i + 3] in (b'u8"', b"u8'"):
            str_prefix_len = 2
        elif source[i : i + 2] in (b'u"', b'U"', b'L"', b"u'", b"U'", b"L'"):
            str_prefix_len = 1

        if (str_prefix_len and i + str_prefix_len < n) or b in (0x22, 0x27):
            start_b = i
            start_ln = cur_line(start_b)
            quote_byte = source[i + str_prefix_len]
            i += str_prefix_len + 1
            while i < n:
                c = source[i]
                if c == 0x5C:  # backslash escape
                    if i + 2 < n and source[i + 1] == 0x0D and source[i + 2] == 0x0A:
                        i += 3
                    elif i + 1 < n:
                        i += 2
                    else:
                        i += 1
                    continue
                if c == quote_byte:
                    i += 1
                    break
                if c == 0x0A:  # Unterminated literal at newline
                    break
                i += 1
            tokens.append(
                Token(
                    "literal",
                    source[start_b:i].decode("latin1"),
                    start_b,
                    i,
                    start_ln,
                    cur_line(max(start_b, i - 1)),
                    cond,
                )
            )
            continue

        # Identifier
        if (0x41 <= b <= 0x5A) or (0x61 <= b <= 0x7A) or b == 0x5F:
            start_b = i
            start_ln = cur_line(start_b)
            i += 1
            while i < n:
                c = source[i]
                if (0x41 <= c <= 0x5A) or (0x61 <= c <= 0x7A) or (0x30 <= c <= 0x39) or c == 0x5F:
                    i += 1
                else:
                    break
            tokens.append(
                Token(
                    "ident",
                    source[start_b:i].decode("ascii"),
                    start_b,
                    i,
                    start_ln,
                    cur_line(i - 1),
                    cond,
                )
            )
            continue

        # Numbers
        if 0x30 <= b <= 0x39:
            start_b = i
            start_ln = cur_line(start_b)
            i += 1
            while i < n:
                c = source[i]
                if (0x30 <= c <= 0x39) or (0x41 <= c <= 0x5A) or (0x61 <= c <= 0x7A) or c in (0x5F, 0x2E, 0x27):
                    i += 1
                else:
                    break
            tokens.append(
                Token(
                    "literal",
                    source[start_b:i].decode("ascii", errors="ignore"),
                    start_b,
                    i,
                    start_ln,
                    cur_line(i - 1),
                    cond,
                )
            )
            continue

        # Multi-character punctuation (::, ->, etc.)
        start_b = i
        start_ln = cur_line(start_b)
        if i + 1 < n and source[i : i + 2] in (b"::", b"->", b"<<", b">>", b"<=", b">=", b"==", b"!=", b"&&", b"||"):
            i += 2
            tokens.append(
                Token("punct", source[start_b:i].decode("ascii"), start_b, i, start_ln, cur_line(i - 1), cond)
            )
            continue

        # Single-character punctuation or non-ASCII byte
        if b < 128:
            i += 1
            tokens.append(Token("punct", chr(b), start_b, i, start_ln, start_ln, cond))
        else:
            # Consume contiguous non-ASCII bytes
            i += 1
            while i < n and source[i] >= 128:
                i += 1
            tokens.append(Token("other", "", start_b, i, start_ln, cur_line(i - 1), cond))

    return tokens


def _resolve_condition_state(
    lexer_cond: str,
    macro_start_byte: int,
    active_macro_offsets: set[int] | None,
) -> str:
    """Resolve condition_state given optional preprocessor active macro offsets."""
    if active_macro_offsets is not None:
        if macro_start_byte in active_macro_offsets:
            return "active"
        return "inactive"
    return lexer_cond


def _parse_macro_args(
    tokens: list[Token],
    open_paren_idx: int,
    source: bytes,
    encoding: str,
) -> tuple[list[tuple[str, list[Token]]], int] | None:
    """Parse balanced macro invocation arguments starting at '(' token index.

    Returns (list of (raw_arg_str, arg_tokens), close_paren_token_index) or None if unbalanced.
    """
    if open_paren_idx >= len(tokens) or tokens[open_paren_idx].text != "(":
        return None

    args: list[tuple[str, list[Token]]] = []
    cur_tokens: list[Token] = []
    paren_depth = 1
    brace_depth = 0
    bracket_depth = 0
    idx = open_paren_idx + 1

    while idx < len(tokens):
        tok = tokens[idx]
        if tok.kind == "punct":
            if tok.text == "(":
                paren_depth += 1
            elif tok.text == ")":
                paren_depth -= 1
                if paren_depth == 0:
                    if cur_tokens or args:
                        if not cur_tokens:
                            args.append(("", []))
                        else:
                            raw = source[cur_tokens[0].start_byte : cur_tokens[-1].end_byte].decode(
                                encoding, errors="replace"
                            )
                            args.append((raw.strip(), cur_tokens))
                    return args, idx
            elif tok.text == "{":
                brace_depth += 1
            elif tok.text == "}":
                brace_depth = max(0, brace_depth - 1)
            elif tok.text == "[":
                bracket_depth += 1
            elif tok.text == "]":
                bracket_depth = max(0, bracket_depth - 1)
            elif tok.text == "," and paren_depth == 1 and brace_depth == 0 and bracket_depth == 0:
                if not cur_tokens:
                    args.append(("", []))
                else:
                    raw = source[cur_tokens[0].start_byte : cur_tokens[-1].end_byte].decode(
                        encoding, errors="replace"
                    )
                    args.append((raw.strip(), cur_tokens))
                cur_tokens = []
                idx += 1
                continue

        cur_tokens.append(tok)
        idx += 1

    return None


def _parse_handler_arg(
    raw_handler: str,
    handler_tokens: list[Token],
    owner_class: str,
) -> dict[str, str | None] | None:
    """Validate and normalize an MFC handler argument.

    Supports both `&Class::Handler`, `Class::Handler`, and bare `Handler`.
    Rejects malformed expressions.
    """
    if not handler_tokens:
        return None

    toks = list(handler_tokens)
    # Strip outer parens if someone wrote (&CMyDlg::OnOk)
    while len(toks) >= 2 and toks[0].text == "(" and toks[-1].text == ")":
        # Check if outer parens match
        depth = 0
        matched = True
        for t in toks[1:-1]:
            if t.text == "(":
                depth += 1
            elif t.text == ")":
                depth -= 1
                if depth < 0:
                    matched = False
                    break
        if matched and depth == 0:
            toks = toks[1:-1]
        else:
            break

    if not toks:
        return None

    if toks[0].text == "&":
        toks = toks[1:]
    if not toks:
        return None

    # Reconstruct identifier chain: Ident (:: Ident)*
    parts: list[str] = []
    expect_ident = True
    for t in toks:
        if expect_ident:
            if t.kind != "ident":
                return None
            parts.append(t.text)
            expect_ident = False
        else:
            if t.text != "::":
                return None
            expect_ident = True

    if expect_ident or not parts:
        return None

    if len(parts) == 1:
        method_name = parts[0]
        handler_class = owner_class
        qualified = f"{owner_class}::{method_name}" if owner_class else method_name
    else:
        method_name = parts[-1]
        handler_class = "::".join(parts[:-1])
        qualified = "::".join(parts)

    return {
        "handler_spelling": raw_handler.strip(),
        "handler_name": method_name,
        "handler_class": handler_class,
        "qualified_handler": qualified,
    }


def _is_valid_class_name(raw_name: str, toks: list[Token]) -> bool:
    if not toks or not raw_name:
        return False
    # Allow Simple, Namespace::Class, or simple template Class<T>
    for t in toks:
        if t.kind == "ident":
            continue
        if t.kind == "punct" and t.text in ("::", "<", ">", ","):
            continue
        return False
    return toks[0].kind == "ident"


def _make_diagnostic(
    *,
    path: str,
    owner_class: str | None,
    base_class: str | None,
    macro: str,
    reason: str,
    message: str,
    start_byte: int,
    end_byte: int,
    line: int,
    end_line: int,
    condition_state: str,
) -> dict[str, Any]:
    return {
        "kind": "diagnostic",
        "path": path,
        "owner_class": owner_class,
        "base_class": base_class,
        "macro": macro,
        "event_kind": None,
        "event_id": None,
        "event_id_end": None,
        "event_range": None,
        "notification_code": None,
        "handler_spelling": None,
        "handler_name": None,
        "handler_class": None,
        "qualified_handler": None,
        "dispatch_kind": None,
        "extraction_quality": "syntactic",
        "resolution_status": "unresolved",
        "condition_state": condition_state,
        "reason": reason,
        "message": message,
        "start_byte": start_byte,
        "end_byte": end_byte,
        "line": line,
        "end_line": end_line,
        "rule_version": MFC_RULE_VERSION,
    }


def _scan_mfc_bytes(
    source: bytes,
    path: str,
    encoding: str = "utf-8",
    active_macro_offsets: set[int] | None = None,
) -> list[dict[str, Any]]:
    """Extract MFC message map evidence and diagnostics from raw C++ source bytes.

    Args:
        source: Raw bytes of the C++ file.
        path: File path string for provenance.
        encoding: Source character encoding for decoding token slices.
        active_macro_offsets: Optional set of 0-based byte offsets of macro invocations
            confirmed active by preprocessor/libclang. If provided, only invocations
            whose start_byte is in this set are marked 'active'; others are 'inactive'.
            If None, conditional regions (#ifdef/#if) are marked 'unknown', '#if 0'
            regions are marked 'inactive', and unconditional regions are 'active'.

    Returns:
        List of observation dicts with kind 'event_mapping' or 'diagnostic'.
    """
    if not isinstance(source, (bytes, bytearray, memoryview)):
        raise TypeError("source must be bytes-like")
    source_bytes = bytes(source)
    line_starts = _build_line_Lookup(source_bytes)
    tokens = _lex_cpp(source_bytes, line_starts)

    observations: list[dict[str, Any]] = []

    in_map = False
    map_owner: str | None = None
    map_base: str | None = None
    map_start_tok: Token | None = None
    map_cond_state: str = "active"

    i = 0
    num_tokens = len(tokens)

    while i < num_tokens:
        tok = tokens[i]
        if tok.kind != "ident":
            i += 1
            continue

        name = tok.text

        # Handle BEGIN_MESSAGE_MAP / BEGIN_TEMPLATE_MESSAGE_MAP
        if name in ("BEGIN_MESSAGE_MAP", "BEGIN_TEMPLATE_MESSAGE_MAP"):
            cond = _resolve_condition_state(tok.cond_state, tok.start_byte, active_macro_offsets)
            if in_map and map_start_tok is not None:
                # Previous message map was not closed before a new BEGIN_MESSAGE_MAP
                observations.append(
                    _make_diagnostic(
                        path=path,
                        owner_class=map_owner,
                        base_class=map_base,
                        macro="BEGIN_MESSAGE_MAP",
                        reason="unclosed_message_map",
                        message=f"Unclosed BEGIN_MESSAGE_MAP for '{map_owner}' before new {name}",
                        start_byte=map_start_tok.start_byte,
                        end_byte=tok.start_byte,
                        line=map_start_tok.line,
                        end_line=tok.line,
                        condition_state=map_cond_state,
                    )
                )
                in_map = False

            if name == "BEGIN_TEMPLATE_MESSAGE_MAP":
                parsed = _parse_macro_args(tokens, i + 1, source_bytes, encoding)
                end_idx = parsed[1] if parsed else i
                end_tok = tokens[end_idx]
                observations.append(
                    _make_diagnostic(
                        path=path,
                        owner_class=None,
                        base_class=None,
                        macro=name,
                        reason="unsupported_macro",
                        message="Template message maps are not supported in MVP",
                        start_byte=tok.start_byte,
                        end_byte=end_tok.end_byte,
                        line=tok.line,
                        end_line=end_tok.end_line,
                        condition_state=cond,
                    )
                )
                i = end_idx + 1
                continue

            parsed = _parse_macro_args(tokens, i + 1, source_bytes, encoding)
            if parsed is None:
                observations.append(
                    _make_diagnostic(
                        path=path,
                        owner_class=None,
                        base_class=None,
                        macro=name,
                        reason="malformed_message_map",
                        message="Malformed BEGIN_MESSAGE_MAP: missing or unbalanced parentheses",
                        start_byte=tok.start_byte,
                        end_byte=tok.end_byte,
                        line=tok.line,
                        end_line=tok.end_line,
                        condition_state=cond,
                    )
                )
                i += 1
                continue

            args, close_idx = parsed
            close_tok = tokens[close_idx]
            if len(args) != 2 or not _is_valid_class_name(args[0][0], args[0][1]) or not _is_valid_class_name(args[1][0], args[1][1]):
                observations.append(
                    _make_diagnostic(
                        path=path,
                        owner_class=args[0][0] if args and args[0][0] else None,
                        base_class=args[1][0] if len(args) > 1 and args[1][0] else None,
                        macro=name,
                        reason="malformed_message_map",
                        message=f"BEGIN_MESSAGE_MAP expects (owner_class, base_class), got {len(args)} argument(s)",
                        start_byte=tok.start_byte,
                        end_byte=close_tok.end_byte,
                        line=tok.line,
                        end_line=close_tok.end_line,
                        condition_state=cond,
                    )
                )
                i = close_idx + 1
                continue

            in_map = True
            map_owner = args[0][0].replace(" ", "")
            map_base = args[1][0].replace(" ", "")
            map_start_tok = tok
            map_cond_state = cond
            i = close_idx + 1
            continue

        # Handle END_MESSAGE_MAP
        if name == "END_MESSAGE_MAP":
            cond = _resolve_condition_state(tok.cond_state, tok.start_byte, active_macro_offsets)
            parsed = _parse_macro_args(tokens, i + 1, source_bytes, encoding)
            if parsed is None:
                observations.append(
                    _make_diagnostic(
                        path=path,
                        owner_class=map_owner,
                        base_class=map_base,
                        macro=name,
                        reason="malformed_message_map",
                        message="END_MESSAGE_MAP missing '()'",
                        start_byte=tok.start_byte,
                        end_byte=tok.end_byte,
                        line=tok.line,
                        end_line=tok.end_line,
                        condition_state=cond,
                    )
                )
                in_map = False
                map_owner = None
                map_base = None
                map_start_tok = None
                i += 1
                continue

            args, close_idx = parsed
            close_tok = tokens[close_idx]
            if not in_map:
                observations.append(
                    _make_diagnostic(
                        path=path,
                        owner_class=None,
                        base_class=None,
                        macro=name,
                        reason="unexpected_end_message_map",
                        message="END_MESSAGE_MAP() without matching BEGIN_MESSAGE_MAP",
                        start_byte=tok.start_byte,
                        end_byte=close_tok.end_byte,
                        line=tok.line,
                        end_line=close_tok.end_line,
                        condition_state=cond,
                    )
                )
            elif args:
                observations.append(
                    _make_diagnostic(
                        path=path,
                        owner_class=map_owner,
                        base_class=map_base,
                        macro=name,
                        reason="malformed_message_map",
                        message="END_MESSAGE_MAP() takes no arguments",
                        start_byte=tok.start_byte,
                        end_byte=close_tok.end_byte,
                        line=tok.line,
                        end_line=close_tok.end_line,
                        condition_state=cond,
                    )
                )
            in_map = False
            map_owner = None
            map_base = None
            map_start_tok = None
            i = close_idx + 1
            continue

        # Inside a message map, inspect entries
        if in_map and map_owner is not None:
            cond = _resolve_condition_state(tok.cond_state, tok.start_byte, active_macro_offsets)
            if active_macro_offsets is None and map_cond_state != "active":
                # Inherit outer map condition if preprocessor offsets are not provided
                if map_cond_state == "inactive" or cond == "inactive":
                    cond = "inactive"
                else:
                    cond = "unknown"

            # 1. Check explicit ON_WM_* registry
            if name in WM_MACRO_REGISTRY:
                parsed = _parse_macro_args(tokens, i + 1, source_bytes, encoding)
                if parsed is None:
                    observations.append(
                        _make_diagnostic(
                            path=path,
                            owner_class=map_owner,
                            base_class=map_base,
                            macro=name,
                            reason="malformed_invocation",
                            message=f"Malformed {name} invocation: missing or unbalanced '()'",
                            start_byte=tok.start_byte,
                            end_byte=tok.end_byte,
                            line=tok.line,
                            end_line=tok.end_line,
                            condition_state=cond,
                        )
                    )
                    i += 1
                    continue
                args, close_idx = parsed
                close_tok = tokens[close_idx]
                if args:
                    observations.append(
                        _make_diagnostic(
                            path=path,
                            owner_class=map_owner,
                            base_class=map_base,
                            macro=name,
                            reason="malformed_invocation",
                            message=f"{name}() expects 0 arguments, got {len(args)}",
                            start_byte=tok.start_byte,
                            end_byte=close_tok.end_byte,
                            line=tok.line,
                            end_line=close_tok.end_line,
                            condition_state=cond,
                        )
                    )
                    i = close_idx + 1
                    continue

                wm_id, fixed_handler = WM_MACRO_REGISTRY[name]
                observations.append(
                    {
                        "kind": "event_mapping",
                        "path": path,
                        "owner_class": map_owner,
                        "base_class": map_base,
                        "macro": name,
                        "event_kind": "wm_message",
                        "event_id": wm_id,
                        "event_id_end": None,
                        "event_range": None,
                        "notification_code": wm_id,
                        "handler_spelling": fixed_handler,
                        "handler_name": fixed_handler,
                        "handler_class": map_owner,
                        "qualified_handler": f"{map_owner}::{fixed_handler}",
                        "dispatch_kind": "event-registration",
                        "extraction_quality": "syntactic",
                        "resolution_status": "unresolved",
                        "condition_state": cond,
                        "reason": "handler_not_semantically_bound",
                        "message": None,
                        "start_byte": tok.start_byte,
                        "end_byte": close_tok.end_byte,
                        "line": tok.line,
                        "end_line": close_tok.end_line,
                        "rule_version": MFC_RULE_VERSION,
                    }
                )
                i = close_idx + 1
                continue

            # 2. Check supported parameterized message map macros
            if name in MAP_MACRO_SPECS:
                event_kind, expected_arity, is_range = MAP_MACRO_SPECS[name]
                parsed = _parse_macro_args(tokens, i + 1, source_bytes, encoding)
                if parsed is None:
                    observations.append(
                        _make_diagnostic(
                            path=path,
                            owner_class=map_owner,
                            base_class=map_base,
                            macro=name,
                            reason="malformed_invocation",
                            message=f"Malformed {name} invocation: missing or unbalanced parentheses",
                            start_byte=tok.start_byte,
                            end_byte=tok.end_byte,
                            line=tok.line,
                            end_line=tok.end_line,
                            condition_state=cond,
                        )
                    )
                    i += 1
                    continue

                args, close_idx = parsed
                close_tok = tokens[close_idx]
                if len(args) != expected_arity or any(not a[0] for a in args):
                    observations.append(
                        _make_diagnostic(
                            path=path,
                            owner_class=map_owner,
                            base_class=map_base,
                            macro=name,
                            reason="malformed_invocation",
                            message=f"{name} expects {expected_arity} non-empty arguments, got {len(args)}",
                            start_byte=tok.start_byte,
                            end_byte=close_tok.end_byte,
                            line=tok.line,
                            end_line=close_tok.end_line,
                            condition_state=cond,
                        )
                    )
                    i = close_idx + 1
                    continue

                # Last argument is always the handler in all supported macros
                raw_handler, handler_toks = args[-1]
                handler_info = _parse_handler_arg(raw_handler, handler_toks, map_owner)
                if handler_info is None:
                    observations.append(
                        _make_diagnostic(
                            path=path,
                            owner_class=map_owner,
                            base_class=map_base,
                            macro=name,
                            reason="malformed_handler",
                            message=f"Invalid handler expression '{raw_handler}' in {name}",
                            start_byte=tok.start_byte,
                            end_byte=close_tok.end_byte,
                            line=tok.line,
                            end_line=close_tok.end_line,
                            condition_state=cond,
                        )
                    )
                    i = close_idx + 1
                    continue

                # Extract event_id, event_id_end, notification_code based on macro signature
                notification_code: str | None = None
                event_id: str | None = None
                event_id_end: str | None = None
                event_range: tuple[str, str] | None = None

                if name in ("ON_COMMAND", "ON_UPDATE_COMMAND_UI", "ON_BN_CLICKED", "ON_MESSAGE", "ON_REGISTERED_MESSAGE", "ON_THREAD_MESSAGE", "ON_REGISTERED_THREAD_MESSAGE"):
                    event_id = args[0][0]
                    if name == "ON_BN_CLICKED":
                        notification_code = "BN_CLICKED"
                elif name in ("ON_COMMAND_RANGE", "ON_UPDATE_COMMAND_UI_RANGE"):
                    event_id = args[0][0]
                    event_id_end = args[1][0]
                    event_range = (event_id, event_id_end)
                elif name in ("ON_NOTIFY", "ON_CONTROL"):
                    notification_code = args[0][0]
                    event_id = args[1][0]
                elif name in ("ON_NOTIFY_RANGE", "ON_CONTROL_RANGE"):
                    notification_code = args[0][0]
                    event_id = args[1][0]
                    event_id_end = args[2][0]
                    event_range = (event_id, event_id_end)

                observations.append(
                    {
                        "kind": "event_mapping",
                        "path": path,
                        "owner_class": map_owner,
                        "base_class": map_base,
                        "macro": name,
                        "event_kind": event_kind,
                        "event_id": event_id,
                        "event_id_end": event_id_end,
                        "event_range": event_range,
                        "notification_code": notification_code,
                        "handler_spelling": handler_info["handler_spelling"],
                        "handler_name": handler_info["handler_name"],
                        "handler_class": handler_info["handler_class"],
                        "qualified_handler": handler_info["qualified_handler"],
                        "dispatch_kind": "event-registration",
                        "extraction_quality": "syntactic",
                        "resolution_status": "unresolved",
                        "condition_state": cond,
                        "reason": "handler_not_semantically_bound",
                        "message": None,
                        "start_byte": tok.start_byte,
                        "end_byte": close_tok.end_byte,
                        "line": tok.line,
                        "end_line": close_tok.end_line,
                        "rule_version": MFC_RULE_VERSION,
                    }
                )
                i = close_idx + 1
                continue

            # 3. Unknown ON_* or custom macro invocation inside message map -> diagnostic
            parsed = _parse_macro_args(tokens, i + 1, source_bytes, encoding)
            if parsed is not None:
                _, close_idx = parsed
                close_tok = tokens[close_idx]
                reason = "unsupported_macro" if name.startswith("ON_") else "unknown_map_macro"
                observations.append(
                    _make_diagnostic(
                        path=path,
                        owner_class=map_owner,
                        base_class=map_base,
                        macro=name,
                        reason=reason,
                        message=f"Unsupported or unknown message map macro '{name}'",
                        start_byte=tok.start_byte,
                        end_byte=close_tok.end_byte,
                        line=tok.line,
                        end_line=close_tok.end_line,
                        condition_state=cond,
                    )
                )
                i = close_idx + 1
                continue
            else:
                # Bare identifier or malformed macro inside message map
                observations.append(
                    _make_diagnostic(
                        path=path,
                        owner_class=map_owner,
                        base_class=map_base,
                        macro=name,
                        reason="malformed_invocation",
                        message=f"Malformed or unparenthesized entry '{name}' inside message map",
                        start_byte=tok.start_byte,
                        end_byte=tok.end_byte,
                        line=tok.line,
                        end_line=tok.end_line,
                        condition_state=cond,
                    )
                )
                i += 1
                continue

        i += 1

    # Check if file ended with an unclosed BEGIN_MESSAGE_MAP
    if in_map and map_start_tok is not None:
        last_tok = tokens[-1] if tokens else map_start_tok
        observations.append(
            _make_diagnostic(
                path=path,
                owner_class=map_owner,
                base_class=map_base,
                macro="BEGIN_MESSAGE_MAP",
                reason="unclosed_message_map",
                message=f"Unclosed BEGIN_MESSAGE_MAP for '{map_owner}' at end of file",
                start_byte=map_start_tok.start_byte,
                end_byte=last_tok.end_byte,
                line=map_start_tok.line,
                end_line=last_tok.end_line,
                condition_state=map_cond_state,
            )
        )

    return observations


# ============================================================================
# Resource Extractor (`resource.h` and `.rc` / `.rc2`)
# ============================================================================

_C_INT_SUFFIX_RE = re.compile(r"\b(0[xX][0-9a-fA-F]+|0[bB][01]+|\d+)[uUlL]+\b")
_SAFE_EXPR_CHARS_RE = re.compile(r"^[0-9a-fA-FxXbB_A-Za-z\s\+\-\*\/\%\(\)\<\>\&\^\|\~]+$")

# Standard .rc statement keywords inside DIALOG / DIALOGEX / MENU blocks
_RC_NAMED_CONTROLS = {
    "PUSHBUTTON",
    "DEFPUSHBUTTON",
    "LTEXT",
    "RTEXT",
    "CTEXT",
    "CHECKBOX",
    "AUTOCHECKBOX",
    "RADIOBUTTON",
    "AUTORADIOBUTTON",
    "STATE3",
    "AUTO3STATE",
    "GROUPBOX",
    "ICON",
}
_RC_NAMELESS_CONTROLS = {
    "EDITTEXT",
    "COMBOBOX",
    "LISTBOX",
    "SCROLLBAR",
}


def _safe_eval_constant_expr(expr: str, symbol_table: dict[str, int]) -> int | None:
    """Safely evaluate integer constant expression without eval().

    Supports integer literals (dec, hex, oct, bin with C suffixes like U/L),
    resolved symbols from `symbol_table`, unary +/-/~, and binary +,-,*,/,<<,>>,|,&,^.
    Returns None if expression contains unresolved symbols, division by zero, or unsupported syntax.
    """
    cleaned = expr.strip()
    if not cleaned or len(cleaned) > 2048 or not _SAFE_EXPR_CHARS_RE.match(cleaned):
        return None

    # Strip C integer suffixes (U, L, UL, ULL, etc.)
    cleaned = _C_INT_SUFFIX_RE.sub(r"\1", cleaned)
    # Convert C leading-zero octal literals (e.g. 010) to 0o10
    cleaned = re.sub(r"\b0([0-7]+)\b", r"0o\1", cleaned)

    try:
        tree = ast.parse(cleaned, mode="eval")
    except (SyntaxError, ValueError, RecursionError):
        return None
    if sum(1 for _ in ast.walk(tree)) > 128:
        return None

    def _eval_node(node: ast.AST) -> int | None:
        result = _eval_impl(node)
        return result if result is not None and result.bit_length() <= 128 else None

    def _eval_impl(node: ast.AST) -> int | None:
        if isinstance(node, ast.Expression):
            return _eval_node(node.body)
        if isinstance(node, ast.Constant):
            if isinstance(node.value, int) and not isinstance(node.value, bool):
                return int(node.value)
            return None
        if isinstance(node, ast.Name):
            return symbol_table.get(node.id)
        if isinstance(node, ast.UnaryOp):
            operand = _eval_node(node.operand)
            if operand is None:
                return None
            if isinstance(node.op, ast.UAdd):
                return +operand
            if isinstance(node.op, ast.USub):
                return -operand
            if isinstance(node.op, ast.Invert):
                return ~operand
            return None
        if isinstance(node, ast.BinOp):
            left = _eval_node(node.left)
            right = _eval_node(node.right)
            if left is None or right is None:
                return None
            if isinstance(node.op, ast.Add):
                return left + right
            if isinstance(node.op, ast.Sub):
                return left - right
            if isinstance(node.op, ast.Mult):
                return left * right
            if isinstance(node.op, (ast.Div, ast.FloorDiv)):
                if right == 0:
                    return None
                # C integer division truncates toward zero
                quotient = abs(left) // abs(right)
                return -quotient if (left < 0) != (right < 0) else quotient
            if isinstance(node.op, ast.Mod):
                if right == 0:
                    return None
                quotient = abs(left) // abs(right)
                if (left < 0) != (right < 0):
                    quotient = -quotient
                return left - quotient * right
            if isinstance(node.op, ast.LShift):
                if right < 0 or right > 64:
                    return None
                return left << right
            if isinstance(node.op, ast.RShift):
                if right < 0 or right > 64:
                    return None
                return left >> right
            if isinstance(node.op, ast.BitOr):
                return left | right
            if isinstance(node.op, ast.BitAnd):
                return left & right
            if isinstance(node.op, ast.BitXor):
                return left ^ right
        return None

    return _eval_node(tree)


def _strip_line_comments_preserve_strings(text: str) -> str:
    """Remove // and /* */ comments from a single logical line while preserving string literals."""
    out: list[str] = []
    i = 0
    n = len(text)
    in_str = False
    while i < n:
        ch = text[i]
        if in_str:
            out.append(ch)
            if ch == '"':
                if i + 1 < n and text[i + 1] == '"':  # .rc escaped quote ""
                    out.append('"')
                    i += 2
                    continue
                in_str = False
            elif ch == "\\" and i + 1 < n:
                out.append(text[i + 1])
                i += 2
                continue
            i += 1
            continue

        if ch == '"':
            in_str = True
            out.append(ch)
            i += 1
            continue
        if ch == "/" and i + 1 < n and text[i + 1] == "/":
            break
        if ch == "/" and i + 1 < n and text[i + 1] == "*":
            end = text.find("*/", i + 2)
            if end == -1:
                break
            out.append(" ")
            i = end + 2
            continue
        out.append(ch)
        i += 1
    return "".join(out)


def _split_rc_args(arg_str: str) -> list[str]:
    """Split comma-separated .rc arguments respecting string quotes and parentheses."""
    args: list[str] = []
    cur: list[str] = []
    in_str = False
    depth = 0
    i = 0
    n = len(arg_str)
    while i < n:
        ch = arg_str[i]
        if in_str:
            cur.append(ch)
            if ch == '"':
                if i + 1 < n and arg_str[i + 1] == '"':
                    cur.append('"')
                    i += 2
                    continue
                in_str = False
            elif ch == "\\" and i + 1 < n:
                cur.append(arg_str[i + 1])
                i += 2
                continue
            i += 1
            continue
        if ch == '"':
            in_str = True
            cur.append(ch)
        elif ch == "(":
            depth += 1
            cur.append(ch)
        elif ch == ")":
            depth = max(0, depth - 1)
            cur.append(ch)
        elif ch == "," and depth == 0:
            args.append("".join(cur).strip())
            cur = []
        else:
            cur.append(ch)
        i += 1
    if cur or args:
        args.append("".join(cur).strip())
    return args


def _unquote_rc_string(s: str) -> str | None:
    s = s.strip()
    if s.startswith("L\""):
        s = s[1:]
    if len(s) >= 2 and s.startswith('"') and s.endswith('"'):
        inner = s[1:-1]
        return inner.replace('""', '"').replace('\\"', '"')
    return None


def _prepare_encoded_source(source: bytes, encoding: str) -> tuple[bytes, list[int] | None]:
    """Normalize lexical input while retaining a map back to original bytes."""
    raw = bytes(source)
    bom = 0
    for marker, name in (
        (b"\xff\xfe\x00\x00", "utf-32-le"),
        (b"\x00\x00\xfe\xff", "utf-32-be"),
        (b"\xef\xbb\xbf", "utf-8"),
        (b"\xff\xfe", "utf-16-le"),
        (b"\xfe\xff", "utf-16-be"),
    ):
        if raw.startswith(marker):
            bom, encoding = len(marker), name
            break
    if encoding.lower().replace("_", "-") in ("utf-8", "utf8") and not bom:
        return raw, None
    if encoding.lower().replace("_", "-") == "utf-8-sig":
        encoding = "utf-8"
    text = raw[bom:].decode(encoding, errors="strict")
    normalized = text.encode("utf-8")
    offsets: list[int] = []
    original_offset = bom
    for character in text:
        offsets.extend([original_offset] * len(character.encode("utf-8")))
        original_offset += len(character.encode(encoding))
    offsets.append(len(raw))
    return normalized, offsets


def _scan_encoded(scanner, source: bytes, path: str, encoding: str, active_macro_offsets=None):
    if not isinstance(source, (bytes, bytearray, memoryview)):
        raise TypeError("source must be bytes-like")
    try:
        normalized, offsets = _prepare_encoded_source(source, encoding)
    except (UnicodeError, LookupError) as exc:
        return [{"kind": "diagnostic", "path": path, "reason": "unsupported_encoding",
                 "message": str(exc), "start_byte": 0, "end_byte": len(source),
                 "line": 1, "end_line": 1, "condition_state": "unknown",
                 "resolution_status": "unresolved", "rule_version": MFC_RULE_VERSION}]
    if scanner is _scan_mfc_bytes:
        active = active_macro_offsets
        if active is not None and offsets is not None:
            reverse = {original: index for index, original in enumerate(offsets)}
            active = {reverse[value] for value in active if value in reverse}
        result = scanner(normalized, path, "utf-8", active)
    else:
        result = scanner(normalized, path, "utf-8")
    if offsets is not None:
        for observation in result:
            for field in ("start_byte", "end_byte"):
                if field in observation:
                    observation[field] = offsets[observation[field]]
    return result


def scan_mfc(source: bytes, path: str, encoding: str = "utf-8",
             active_macro_offsets: set[int] | None = None) -> list[dict[str, Any]]:
    """Read syntactic registrations; semantic handler binding is a separate step."""
    return _scan_encoded(_scan_mfc_bytes, source, path, encoding, active_macro_offsets)


def scan_resources(source: bytes, path: str, encoding: str = "utf-8") -> list[dict[str, Any]]:
    """Extract resource evidence with byte spans in the original file encoding."""
    return _scan_encoded(_scan_resources_bytes, source, path, encoding)


def _scan_resources_bytes(
    source: bytes,
    path: str,
    encoding: str = "utf-8",
) -> list[dict[str, Any]]:
    """Extract symbolic #define resource IDs and basic .rc dialogs/controls/menus.

    Safe constant expressions are evaluated without `eval()`; unresolved expressions
    keep `value=None`, `value_expr=<raw>`, and `resolution_status='unresolved'`.
    Byte spans (`start_byte`, `end_byte`) and 1-based line numbers (`line`, `end_line`)
    are preserved accurately across UTF-8/UTF-16/CP1252 and CRLF/LF.
    """
    if not isinstance(source, (bytes, bytearray, memoryview)):
        raise TypeError("source must be bytes-like")
    source_bytes = bytes(source)
    line_starts = _build_line_Lookup(source_bytes)
    observations: list[dict[str, Any]] = []
    symbol_table: dict[str, int] = {}

    # Break source into physical/logical lines with exact byte offsets
    n = len(source_bytes)
    pos = 0
    logical_lines: list[tuple[int, int, int, int, str]] = []
    in_block_comment = False

    while pos < n:
        stmt_start_byte = pos
        stmt_start_line = _byte_to_line(line_starts, stmt_start_byte)
        # Accumulate logical line if trailing backslash
        while pos < n:
            nl = source_bytes.find(b"\n", pos)
            if nl == -1:
                line_end = n
                next_pos = n
            else:
                line_end = nl - 1 if (nl > pos and source_bytes[nl - 1] == 0x0D) else nl
                next_pos = nl + 1

            # Check if line ends with backslash continuation
            if line_end > pos and source_bytes[line_end - 1] == 0x5C:
                pos = next_pos
                if pos < n:
                    continue
            pos = next_pos
            break

        stmt_end_byte = line_end
        # Trim leading/trailing whitespace bytes for accurate span
        raw_slice = source_bytes[stmt_start_byte:stmt_end_byte]
        lstripped = raw_slice.lstrip(b" \t\r\n")
        if not lstripped:
            continue
        actual_start = stmt_start_byte + (len(raw_slice) - len(lstripped))
        rstripped = lstripped.rstrip(b" \t\r\n")
        actual_end = actual_start + len(rstripped)

        text_raw = rstripped.decode(encoding, errors="replace")
        # Handle multiline block comments across logical lines
        cleaned_chunks: list[str] = []
        idx = 0
        t_len = len(text_raw)
        while idx < t_len:
            if in_block_comment:
                end_c = text_raw.find("*/", idx)
                if end_c == -1:
                    idx = t_len
                    break
                in_block_comment = False
                idx = end_c + 2
                cleaned_chunks.append(" ")
            else:
                # Check string literal or comment start
                ch = text_raw[idx]
                if ch == '"':
                    cleaned_chunks.append(ch)
                    idx += 1
                    while idx < t_len:
                        c2 = text_raw[idx]
                        cleaned_chunks.append(c2)
                        if c2 == '"':
                            if idx + 1 < t_len and text_raw[idx + 1] == '"':
                                cleaned_chunks.append('"')
                                idx += 2
                                continue
                            idx += 1
                            break
                        if c2 == "\\" and idx + 1 < t_len:
                            cleaned_chunks.append(text_raw[idx + 1])
                            idx += 2
                            continue
                        idx += 1
                elif text_raw.startswith("//", idx):
                    break
                elif text_raw.startswith("/*", idx):
                    in_block_comment = True
                    idx += 2
                else:
                    cleaned_chunks.append(ch)
                    idx += 1

        cleaned_text = "".join(cleaned_chunks).strip()
        if not cleaned_text:
            continue

        # Also adjust actual_end if trailing // comment was stripped on single line
        if "//" in text_raw or "/*" in text_raw:
            # Re-trim trailing comment from rstripped bytes if possible
            comment_Free_bytes = cleaned_text.encode(encoding, errors="replace")
            pos_in_slice = rstripped.find(comment_Free_bytes)
            if pos_in_slice != -1:
                actual_start = actual_start + pos_in_slice
                actual_end = actual_start + len(comment_Free_bytes)

        s_line = _byte_to_line(line_starts, actual_start)
        e_line = _byte_to_line(line_starts, max(actual_start, actual_end - 1))
        logical_lines.append((actual_start, actual_end, s_line, e_line, cleaned_text))

    # State for .rc dialog/menu blocks and conditional blocks
    cond_stack: list[_CondFrame] = []
    current_container_id: str | None = None
    current_container_kind: str | None = None
    block_depth = 0

    for s_byte, e_byte, s_line, e_line, line_text in logical_lines:
        # Collapse line continuations inside line_text
        normalized = re.sub(r"\\\r?\n", " ", line_text).strip()

        # Preprocessor directives
        if normalized.startswith("#"):
            m_dir = re.match(r"^#\s*([A-Za-z_][A-Za-z0-9_]*)\b(.*)$", normalized, re.DOTALL)
            if not m_dir:
                continue
            d_name = m_dir.group(1)
            d_rest = m_dir.group(2).strip()
            parent_cond = _Effective_cond(cond_stack)

            if d_name == "if":
                if parent_cond == "inactive":
                    cond_stack.append(_CondFrame("inactive", True))
                else:
                    st = _eval_zero_one_directive(d_rest.encode("latin1", errors="ignore"))
                    cond_stack.append(_CondFrame(st, st != "inactive"))
                continue
            if d_name in ("ifdef", "ifndef"):
                if parent_cond == "inactive":
                    cond_stack.append(_CondFrame("inactive", True))
                else:
                    cond_stack.append(_CondFrame("unknown", True))
                continue
            if d_name == "elif":
                if cond_stack:
                    top = cond_stack.pop()
                    p_cond = _Effective_cond(cond_stack)
                    if p_cond == "inactive":
                        cond_stack.append(_CondFrame("inactive", True))
                    else:
                        st = _eval_zero_one_directive(d_rest.encode("latin1", errors="ignore"))
                        cond_stack.append(_CondFrame("inactive" if st == "inactive" else "unknown", True))
                continue
            if d_name == "else":
                if cond_stack:
                    top = cond_stack.pop()
                    p_cond = _Effective_cond(cond_stack)
                    if p_cond == "inactive":
                        cond_stack.append(_CondFrame("inactive", True))
                    elif top.branch_state == "inactive" and not top.seen_active:
                        cond_stack.append(_CondFrame("active" if p_cond == "active" else "unknown", True))
                    else:
                        cond_stack.append(_CondFrame("unknown", True))
                continue
            if d_name == "endif":
                if cond_stack:
                    cond_stack.pop()
                continue

            if d_name == "undef":
                if _Effective_cond(cond_stack) != "inactive":
                    symbol_table.pop(d_rest.split()[0] if d_rest else "", None)
                continue

            if d_name == "define":
                # Match object-like macro: #define NAME VALUE (reject function-like #define NAME(x))
                m_def = re.match(r"^([A-Za-z_][A-Za-z0-9_]*)(\()?(?:\s+(.*))?$", d_rest, re.DOTALL)
                if not m_def:
                    continue
                sym_name, is_func_macro, val_expr = m_def.group(1), m_def.group(2), m_def.group(3)
                if is_func_macro:
                    continue
                if val_expr is None or not val_expr.strip():
                    continue
                val_expr = val_expr.strip()
                cond = _Effective_cond(cond_stack)
                val = _safe_eval_constant_expr(val_expr, symbol_table)
                if val is not None and cond == "active":
                    symbol_table[sym_name] = val
                elif cond != "inactive":
                    symbol_table.pop(sym_name, None)

                observations.append(
                    {
                        "kind": "resource_define",
                        "path": path,
                        "symbol": sym_name,
                        "value": val,
                        "value_expr": val_expr,
                        "resolution_status": "resolved" if val is not None else "unresolved",
                        "condition_state": cond,
                        "reason": None if val is not None else "unresolved_constant_expression",
                        "start_byte": s_byte,
                        "end_byte": e_byte,
                        "line": s_line,
                        "end_line": e_line,
                        "rule_version": MFC_RULE_VERSION,
                    }
                )
            continue

        cond = _Effective_cond(cond_stack)

        # Check for DIALOG / DIALOGEX / MENU header: <ID> DIALOG[EX] ... or <ID> MENU ...
        m_header = re.match(
            r"^([A-Za-z_][A-Za-z0-9_]*|\d+)\s+(DIALOGEX|DIALOG|MENUEX|MENU)\b(.*)$",
            normalized,
            re.IGNORECASE,
        )
        if m_header and block_depth == 0:
            res_id = m_header.group(1)
            res_type = m_header.group(2).upper()
            current_container_id = res_id
            current_container_kind = "dialog" if "DIALOG" in res_type else "menu"
            val = symbol_table.get(res_id) if _IDENT_RE.match(res_id) else _safe_eval_constant_expr(res_id, symbol_table)
            observations.append(
                {
                    "kind": f"resource_{current_container_kind}",
                    "path": path,
                    "resource_id": res_id,
                    "resource_type": res_type,
                    "value": val,
                    "parent_id": None,
                    "caption": None,
                    "condition_state": cond,
                    "start_byte": s_byte,
                    "end_byte": e_byte,
                    "line": s_line,
                    "end_line": e_line,
                    "rule_version": MFC_RULE_VERSION,
                }
            )
            continue

        # Track BEGIN / { and END / } blocks
        upper_tok = normalized.upper()
        if upper_tok == "BEGIN" or normalized == "{":
            block_depth += 1
            continue
        if upper_tok == "END" or normalized == "}":
            block_depth = max(0, block_depth - 1)
            if block_depth == 0:
                current_container_id = None
                current_container_kind = None
            continue

        if current_container_id is not None and block_depth > 0:
            m_stmt = re.match(r"^([A-Za-z0-9_]+)\s+(.*)$", normalized, re.DOTALL)
            if not m_stmt:
                continue
            kw = m_stmt.group(1).upper()
            rest = m_stmt.group(2).strip()
            rc_args = _split_rc_args(rest)

            # Generic CONTROL: CONTROL text, id, class, style, x, y, w, h
            if kw == "CONTROL" and len(rc_args) >= 3:
                caption = _unquote_rc_string(rc_args[0])
                ctrl_id = rc_args[1].strip()
                ctrl_class = _unquote_rc_string(rc_args[2]) or rc_args[2].strip()
                val = _safe_eval_constant_expr(ctrl_id, symbol_table)
                observations.append(
                    {
                        "kind": "resource_control",
                        "path": path,
                        "control_kind": "CONTROL",
                        "control_class": ctrl_class,
                        "resource_id": ctrl_id,
                        "value": val,
                        "parent_id": current_container_id,
                        "caption": caption,
                        "condition_state": cond,
                        "start_byte": s_byte,
                        "end_byte": e_byte,
                        "line": s_line,
                        "end_line": e_line,
                        "rule_version": MFC_RULE_VERSION,
                    }
                )
            elif kw in _RC_NAMED_CONTROLS and len(rc_args) >= 2:
                caption = _unquote_rc_string(rc_args[0])
                ctrl_id = rc_args[1].strip()
                val = _safe_eval_constant_expr(ctrl_id, symbol_table)
                observations.append(
                    {
                        "kind": "resource_control",
                        "path": path,
                        "control_kind": kw,
                        "control_class": kw,
                        "resource_id": ctrl_id,
                        "value": val,
                        "parent_id": current_container_id,
                        "caption": caption,
                        "condition_state": cond,
                        "start_byte": s_byte,
                        "end_byte": e_byte,
                        "line": s_line,
                        "end_line": e_line,
                        "rule_version": MFC_RULE_VERSION,
                    }
                )
            elif kw in _RC_NAMELESS_CONTROLS and len(rc_args) >= 1:
                ctrl_id = rc_args[0].strip()
                val = _safe_eval_constant_expr(ctrl_id, symbol_table)
                observations.append(
                    {
                        "kind": "resource_control",
                        "path": path,
                        "control_kind": kw,
                        "control_class": kw,
                        "resource_id": ctrl_id,
                        "value": val,
                        "parent_id": current_container_id,
                        "caption": None,
                        "condition_state": cond,
                        "start_byte": s_byte,
                        "end_byte": e_byte,
                        "line": s_line,
                        "end_line": e_line,
                        "rule_version": MFC_RULE_VERSION,
                    }
                )
            elif kw == "MENUITEM" and len(rc_args) >= 2:
                caption = _unquote_rc_string(rc_args[0])
                menu_id = rc_args[1].strip()
                val = _safe_eval_constant_expr(menu_id, symbol_table)
                observations.append(
                    {
                        "kind": "resource_control",
                        "path": path,
                        "control_kind": "MENUITEM",
                        "control_class": "MENUITEM",
                        "resource_id": menu_id,
                        "value": val,
                        "parent_id": current_container_id,
                        "caption": caption,
                        "condition_state": cond,
                        "start_byte": s_byte,
                        "end_byte": e_byte,
                        "line": s_line,
                        "end_line": e_line,
                        "rule_version": MFC_RULE_VERSION,
                    }
                )

    return observations
