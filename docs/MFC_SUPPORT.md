# MFC Evidence & Resource Extractor (`legacygraph/mfc.py`)

This document describes the design, public API, supported macros, observation schemas, and known limitations of the standalone MFC and Windows Resource evidence extractor in `legacygraph/mfc.py`.

Integration review: syntax-only handler observations remain `unresolved` with
`handler_not_semantically_bound` until the semantic adapter verifies a handler.
Only literal `#if 0` is assumed inactive without preprocessing evidence; named
macros such as `FALSE` remain unknown. UTF-16/32 and legacy encodings are
normalized for lexing, with spans mapped back to original bytes. Resource
integer arithmetic is bounded and uses integer division without floating-point
rounding; uncertain redefinitions invalidate earlier constant values.

## 1. Design Principles

1. **Zero External Dependencies**: Implemented strictly with the Python Standard Library (`ast`, `dataclasses`, `re`, `typing`) and no imports from other `legacygraph` modules.
2. **Exact Source Evidence Preservation**: Operates directly on raw `bytes` to preserve exact 0-based `start_byte` / `end_byte` spans and 1-based `line` / `end_line` numbers across UTF-8, Windows legacy encodings, and both LF (`\n`) and CRLF (`\r\n`) line endings.
3. **Stateful Lexing**: Skips single-line (`//`) and block (`/* ... */`) comments, string/character literals, C++11 raw string literals (`R"delim(...)delim"`, `LR"(...)"`, `u8R"(...)"`), line continuations (`\`), and `#define` macro bodies so fake macros are never extracted.
4. **Honest Evidence Taxonomy**: All extracted mappings are labeled with `dispatch_kind="event-registration"` and `extraction_quality="syntactic"`. Message-map entries represent event handler registration—not direct runtime function calls.
5. **No Fabricated Mappings**: Unknown `ON_*` or custom wrapper macros inside a message map, or malformed macro invocations/maps, emit `kind="diagnostic"` observations with specific `reason` codes rather than guessing handler names or fabricating links.

---

## 2. Public API

### 2.1. `scan_mfc`

```python
scan_mfc(
    source: bytes,
    path: str,
    encoding: str = "utf-8",
    active_macro_offsets: set[int] | None = None,
) -> list[dict[str, Any]]
```

- **`source`**: Raw C++ file contents as `bytes`.
- **`path`**: File path string recorded in each observation.
- **`encoding`**: Character encoding used to decode identifier/expression slices (default `'utf-8'`).
- **`active_macro_offsets`**: Optional set of 0-based `start_byte` offsets of macro invocations confirmed active by preprocessing (e.g., from `libclang`).
  - When `active_macro_offsets` is provided: only macro invocations whose `start_byte` is in `active_macro_offsets` receive `condition_state="active"`; absent invocations are marked `"inactive"`.
  - When `active_macro_offsets` is `None`: unconditional regions receive `condition_state="active"`, literal `#if 0` blocks receive `"inactive"`, and other conditional regions receive `"unknown"` unless a branch is provably inactive.

#### Supported Message-Map Macros

| Category | Macros | Event Kind (`event_kind`) | Notes |
|---|---|---|---|
| Map Boundaries | `BEGIN_MESSAGE_MAP(owner, base)`, `END_MESSAGE_MAP()` | — | Establishes `owner_class` and `base_class` context |
| Standard `WM_*` Registry (`WM_MACRO_REGISTRY`) | `ON_WM_CREATE()`, `ON_WM_DESTROY()`, `ON_WM_PAINT()`, `ON_WM_SIZE()`, `ON_WM_CLOSE()`, `ON_WM_TIMER()` | `"wm_message"` | Explicit lookup table (`WM_CREATE` → `OnCreate`, etc.); never strips prefixes arbitrarily |
| Commands & UI Update | `ON_COMMAND(id, memberFxn)`, `ON_UPDATE_COMMAND_UI(id, memberFxn)` | `"command"`, `"update_command_ui"` | Single command ID |
| Controls & Buttons | `ON_BN_CLICKED(id, memberFxn)`, `ON_CONTROL(wNotifyCode, id, memberFxn)` | `"bn_clicked"`, `"control"` | `ON_BN_CLICKED` sets `notification_code="BN_CLICKED"` |
| Notifications | `ON_NOTIFY(wNotifyCode, id, memberFxn)` | `"notify"` | Captures both `notification_code` and `event_id` |
| Custom & Thread Messages | `ON_MESSAGE(msg, memberFxn)`, `ON_REGISTERED_MESSAGE(nMessageVariable, memberFxn)`, `ON_THREAD_MESSAGE(msg, memberFxn)`, `ON_REGISTERED_THREAD_MESSAGE(nMessageVariable, memberFxn)` | `"message"`, `"registered_message"`, `"thread_message"`, `"registered_thread_message"` | Supports parenthesized message expressions |
| Range Macros | `ON_COMMAND_RANGE(id, idLast, memberFxn)`, `ON_UPDATE_COMMAND_UI_RANGE(id, idLast, memberFxn)`, `ON_CONTROL_RANGE(wNotifyCode, id, idLast, memberFxn)`, `ON_NOTIFY_RANGE(wNotifyCode, id, idLast, memberFxn)` | `"command_range"`, `"update_command_ui_range"`, `"control_range"`, `"notify_range"` | Preserves `event_id`, `event_id_end`, and `event_range=(id, idLast)` without materializing individual IDs |

#### Handler Qualification Forms

Both member pointer syntax (`&COrderDlg::OnSave`, `&ui::CMainFrame::OnSave`), qualified class member syntax (`CBaseDlg::OnSave`), and bare member syntax (`OnSave`) are supported:
- `handler_spelling`: Exact source spelling of the handler argument (or fixed handler name for `ON_WM_*`).
- `handler_name`: Unqualified method name (e.g., `"OnSave"`).
- `handler_class`: Explicit class from the handler argument, or `owner_class` when bare.
- `qualified_handler`: Normalized `"{handler_class}::{handler_name}"`.

#### Observation Schema (`scan_mfc`)

Each returned dictionary has `kind` set to `"event_mapping"` or `"diagnostic"`:

```python
{
    "kind": "event_mapping" | "diagnostic",
    "path": str,
    "owner_class": str | None,
    "base_class": str | None,
    "macro": str,
    "event_kind": str | None,
    "event_id": str | None,
    "event_id_end": str | None,
    "event_range": tuple[str, str] | None,
    "notification_code": str | None,
    "handler_spelling": str | None,
    "handler_name": str | None,
    "handler_class": str | None,
    "qualified_handler": str | None,
    "dispatch_kind": "event-registration" | None,
    "extraction_quality": "syntactic",
    "resolution_status": "unresolved",
    "condition_state": "active" | "inactive" | "unknown",
    "reason": str | None,
    "message": str | None,
    "start_byte": int,
    "end_byte": int,
    "line": int,
    "end_line": int,
    "rule_version": "1.0.0",
}
```

Diagnostic `reason` values:
- `"unsupported_macro"`: Unlisted `ON_*` macro or `BEGIN_TEMPLATE_MESSAGE_MAP`.
- `"unknown_map_macro"`: Non-`ON_*` custom macro inside a message map.
- `"malformed_invocation"`: Wrong argument count, empty argument, or unbalanced/missing parentheses on an entry.
- `"malformed_handler"`: Handler argument is not a valid method identifier or member pointer.
- `"malformed_message_map"`: Invalid arguments or missing parentheses on `BEGIN_MESSAGE_MAP` / `END_MESSAGE_MAP`.
- `"unclosed_message_map"`: `BEGIN_MESSAGE_MAP` not closed before another `BEGIN_MESSAGE_MAP` or EOF.
- `"unexpected_end_message_map"`: `END_MESSAGE_MAP()` encountered outside an active map.

---

### 2.2. `scan_resources`

```python
scan_resources(
    source: bytes,
    path: str,
    encoding: str = "utf-8",
) -> list[dict[str, Any]]
```

Extracts symbolic ID definitions (`#define`) from resource headers (`resource.h`) and basic dialog/menu/control declarations from Windows resource scripts (`.rc`, `.rc2`):
- **`kind="resource_define"`**: Records `symbol`, evaluated integer `value` (or `None`), raw `value_expr`, `resolution_status` (`"resolved"` or `"unresolved"`), `condition_state`, and exact byte/line spans. Safe integer constant expressions (`+`, `-`, `*`, `/`, `%`, `<<`, `>>`, `|`, `&`, `^`, `~`, parentheses, hex/octal/binary literals, C integer suffixes `U`/`L`, and previously defined symbols in the same file) are evaluated via `ast.parse` without `eval()`. Unresolved expressions keep `value=None` and `reason="unresolved_constant_expression"`.
- **`kind="resource_dialog"` / `"resource_menu"`**: Records `resource_id`, `resource_type` (`DIALOG`, `DIALOGEX`, `MENU`, `MENUEX`), resolved `value` if known, and byte/line spans.
- **`kind="resource_control"`**: Records `control_kind` (`CONTROL`, `DEFPUSHBUTTON`, `PUSHBUTTON`, `EDITTEXT`, `COMBOBOX`, `LISTBOX`, `LTEXT`, `CHECKBOX`, `MENUITEM`, etc.), `control_class`, `resource_id`, resolved integer `value`, enclosing `parent_id`, `caption`, and byte/line spans.

---

## 3. Limitations

1. **Syntactic Scope Only**: `scan_mfc` does not expand project-custom wrapper macros or include headers. Custom wrapper macros inside a message map are reported as diagnostics (`reason="unknown_map_macro"` or `"unsupported_macro"`).
2. **Explicit `ON_WM_*` Subset**: Only the 6 core `ON_WM_*` macros (`CREATE`, `DESTROY`, `PAINT`, `SIZE`, `CLOSE`, `TIMER`) are registered in `WM_MACRO_REGISTRY` for MVP; other `ON_WM_*` macros produce diagnostics with `reason="unsupported_macro"` until added to the versioned registry.
3. **Single-File Resource Evaluation**: `scan_resources` evaluates constant expressions against `#define` symbols seen earlier in the same file. Cross-file `#include "resource.h"` resolution is deferred to the indexer/merger layer.
4. **Registration vs. Execution**: Extracted mappings represent static message-map registration (`handles_event` / `dispatch_kind="event-registration"`), not guaranteed runtime message delivery or `SendMessage`/`PostMessage` target resolution.
