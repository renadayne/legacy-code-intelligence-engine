# Post-0.3.2: corrected wchar context, diagnostic provenance and source blockers

## What changed in the evidence

The latest redacted operator report is **after** the authorized manifest
correction. Saved context keys match the current manifest. The selected
project property requests typedef wchar and the analysis context now selects
`wchar_mode=typedef`, `wchar_selection=explicit`, with context-level
`/Zc:wchar_t-`, Windows x86, C++14 and Unicode definitions. Per-file overrides
were checked locally. Preserve that selection: the previous native/default
mismatch is closed, not an outstanding configuration fix. Full imported-property
and MSBuild evaluation and original-build equivalence remain unverified.

The previously recorded conversion-overload, wide-pointer and error-limit
diagnostics are absent from that saved result. No error/fatal encoding
diagnostic was recorded. These observations support the context correction;
they do not certify every input or complete semantic coverage.

The report also identifies repeated narrowing at a shared-header sentinel,
non-trivial framework strings passed through trace/format ellipsis calls, and
a missing semicolon after a local enum before a call. These are operator-reported
findings. The tool investigation uses independently generated sources only and
has not inspected or changed the private files, manifest, database or logs.

## Delivered tool changes

### 1. Context expectation verification

`explain-context` can check an operator-supplied expected wchar mode:

```text
python -m legacygraph explain-context --repo <root> --manifest <manifest> --expect-wchar-mode typedef
```

- Exit 0 requires a nonempty import without import warnings, supported argument
  construction, and a matching mode for **every imported TU**.
- Exit 3 includes a mismatch, unknown/unsupported configuration, empty import
  or import warning. It does not mean a source parse was attempted.
- Each row reports `expected_wchar_mode` and `wchar_expectation_matches` when
  this option is used. Without it, the earlier exit behavior remains.
- `wchar_selection_scope` is `default`, `context`, or `translation_unit`;
  `wchar_selected_flag` shows the last explicit wchar flag or null. This helps
  distinguish omitted settings from per-file overrides. For unsupported
  configurations, the selected flag is not a claim of a valid effective mode.

The option asserts an expectation; it never modifies flags, imports XML,
evaluates MSBuild, reads C++ contents, opens an index or invokes libclang parsing.
Do not force a homogeneous expectation across an intentionally mixed-mode
manifest; inspect rows without the option or validate already separate manifests.
Normal output continues to omit source paths/names and raw compiler arguments.
The output cannot certify the original build, so `original_build_match` remains
`unknown`. Command-line Unicode macro state does not prove subsequent source or
header preprocessing state.

### 2. Diagnostic location provenance, classification and blocker summaries

`ClangParser`, `IndexStore`, `scan_repository`, `status`, `contexts` and the
`diagnostics` CLI/query command now record and expose trustworthy per-record
provenance and bounded classification:

```text
python -m legacygraph diagnostics --db <db> [--context <key>] [--class narrowing_initializer] --blocking-only --json
```

- **Per-record provenance and quality**:
  - `location_quality`: `exact`, `fallback`, `none`, or `unknown` (legacy rows).
  - `location_provenance`: `contained_file`, `external_file`, `missing_file`,
    `missing_location`, or `unknown`.
  - `is_exact_location`: `true` only when `location_quality == "exact"` and
    `location_provenance == "contained_file"`.
  - External files outside `repo_root` retain the main-file fallback span on raw
    records for backward compatibility (`location_quality="fallback"`,
    `location_provenance="external_file"`, `is_exact_location=false`) without
    ever exposing absolute external paths.
  - Legacy SQLite databases are migrated additively on open (`SCHEMA_VERSION`
    remains `1`) with conservative backfill: whole-file/config fallbacks become
    `fallback`, missing spans become `none`, and legacy point spans without
    recorded provenance remain `unknown` (`is_exact_location=false`).
- **Retained option and bounded classification**:
  - `diagnostic_option` retains libclang's warning/error option (such as
    `-Wc++11-narrowing`, `-Wnon-pod-varargs`, `-ferror-limit=`).
  - `diagnostic_class` provides bounded classification (`narrowing_initializer`,
    `non_trivial_varargs`, `type_in_result_declaration`, `wchar_pointer_mismatch`,
    `const_qualification_mismatch`, `error_limit_reached`, `missing_include`,
    `unsupported_configuration`, `encoding_failure`, `source_mutation`, etc.)
    distinct from `severity`.
- **Trustworthy blocker summaries**:
  - `blocker_summaries` always contains only error/fatal/unsupported records,
    including when a query filters by severity. It groups diagnostics only when
    `is_exact_location == true` by `(context_key, repo_path, byte_start, byte_end,
    start_line, start_col, content_sha256, diagnostic_class, diagnostic_option,
    severity, code)`,
    preserving `affected_tu_keys`, `affected_main_files`, `parsed_tu_count`, and
    `reused_tu_count` across current and inherited snapshots while retaining all
    raw per-TU records in `diagnostics`.
  - Non-exact (`fallback`, `external_file`, `missing_file`, `missing_location`,
    `unknown`) diagnostics sharing a fallback `main.cpp:1` placeholder are never
    merged as one source defect and report `repo_path=null`, `line=null` with
    `fallback_repo_path` / `fallback_line`.
  - `record_count` and `affected_tu_count` measure diagnostic records and
    affected translation units, never proven distinct defect counts
    (`counts_are_not_defect_counts=true`).
  - `diagnostics --limit N` bounds both raw records and blocker summaries.
    `total`, `total_blocker_summaries` and `diagnostic_summary` describe the full
    filtered result; `diagnostics_truncated`, `blocker_summaries_truncated` and
    returned counts describe the payload. A group's TU detail arrays are limited
    to `min(N, 100)` (100 for scan/status), with `affected_tus_truncated` and
    `omitted_affected_tu_count`; aggregate counts still cover every affected TU.
    Truncation does not delete stored records. Current aggregation still reads
    all matching diagnostics; this output bound is not a constant-memory or
    database performance claim.
  - `--progress` stderr events remain strictly privacy-safe with no source paths,
    messages, or blocker contents.

The parsing policy remains **0.3.2**. Explanation output, diagnostic provenance
and validation tests do not change semantic fingerprints, evidence, progress or
acceptance rules. No new runtime dependency or performance change is introduced.

## Operator action order

1. Preserve the already corrected context. When validating a later tool build,
   use `explain-context --expect-wchar-mode typedef` to confirm the selected
   x86/C++14/Unicode context still matches the saved manifest; no further
   manifest edit is requested here. Full build equivalence remains unverified.
2. Prioritize the shared-header narrowing blocker (`narrowing_initializer`,
   `-Wc++11-narrowing`) first with the application owner. One trustworthy
   contained shared-header location can block many TUs across the build context.
   Confirm the intended sentinel semantics, integer width, signed/unsigned
   comparisons and callers before deciding whether an explicit unsigned
   conversion or all-bits-set constant (`~0u` / `static_cast<unsigned>(-1)`) is
   appropriate; those forms are synthetic test controls only, and LegacyGraph
   never rewrites source or relaxes `-Wc++11-narrowing`.
3. Review `TRACE` and `CString::Format` variadic call contracts
   (`non_trivial_varargs`, `-Wnon-pod-varargs`) locally. Inspect the full
   enclosing multiline call expression when a diagnostic line points to an
   argument continuation line. Explicit pointer extraction (such as
   `GetString()` or `static_cast<LPCTSTR>(...)`) or a typed helper must match
   the real framework API and temporary lifetime rules; never suppress
   `-Wnon-pod-varargs` globally or switch Debug to Release to mask Debug errors.
4. Have the application owner address the missing enum semicolon
   (`type_in_result_declaration`) as a local syntax fix before the following
   call. Clang recovery can report a result-type error and secondary return-type
   errors; the parser never injects punctuation or promotes errored ASTs.
5. Only after separately authorized owner corrections, let the local operator
   validate a representative shared-header-blocked TU before the intended
   workload. Only that separate local rescan can establish private acceptance.
   Use `scan --json`, `status --json` or `diagnostics --blocking-only --json`
   to inspect `blocker_summaries` (`is_exact_location`, `location_provenance`,
   `affected_tu_count`, `record_count`) alongside `verified_tu_runs`. Changing
   manifest flags triggers existing context invalidation; source/header changes
   trigger existing content invalidation. Error-limit termination can hide later
   failures. No raw artifacts need to be shared.

## Per-class outcomes

| Class | Current conclusion | Resolution owner |
| --- | --- | --- |
| Diagnostic fallback vs exact provenance (`location_quality`, `location_provenance`) | Fixed in tool: per-record provenance (`contained_file`, `external_file`, `missing_file`, `missing_location`, `unknown`), option/class metadata, and exact-location `blocker_summaries` are persisted and exposed without leaking external paths or merging fallback lines. | Delivered in LegacyGraph; legacy stores migrate additively on open. |
| Previously native/default versus project typedef wchar (`wchar_pointer_mismatch`) | Corrected before the latest saved result: explicit typedef context and current manifest keys confirmed; previous conversion/wide-pointer diagnostics absent. Existing x86/C++14 support and genuine const errors remain unchanged. | Preserve the corrected manifest and context; no further wchar fix or global default change. Full original-build equivalence remains unverified. |
| Repeated narrowing sentinel (`narrowing_initializer`, `-Wc++11-narrowing`) | Highest-priority shared source blocker: both wchar modes reject negative unsigned initializers. Multiple TUs hitting the same contained header location group into one exact-location blocker summary while keeping raw per-TU records and independent clean TUs intact. | Application owner confirms sentinel contract/width/consumers before source correction. Generated `static_cast<unsigned>(-1)` / `~0u` controls are test-only; no `-Wc++11-narrowing` relaxation. |
| Trace and format varargs (`non_trivial_varargs`, `-Wnon-pod-varargs`) | Generated Debug trace wrapper and format member (including multiline calls where continuation lines carry the diagnostic) reject non-trivial objects. Typed and explicit-pointer/`GetString()` controls pass. Release hides only the generated trace branch, demonstrating a context change, not a Debug fix. | Application owner reviews full multiline calls and actual framework/API lifetime contracts; no `-Wnon-pod-varargs` suppression. |
| Enum without semicolon (`type_in_result_declaration`) | Generated local enum followed by a call produces result-type and secondary return-type diagnostics. Adding the semicolon in the generated control passes in both modes. | Application owner decides the private syntax correction; no source rewrite or parser recovery promotion. |
| Error limit (`error_limit_reached`, `-ferror-limit=`) | Absent from the latest saved result. Generated error-limit controls have fallback/missing-location provenance and remain unmerged; when present, the diagnostic truncates later errors. | Review any newly surfaced errors after authorized corrections. Raising the limit is not a fix. |
| Encoding (`encoding_failure`) | Existing CP932/UTF-8 overlays, original-byte evidence, BOM/CRLF tests remain. Contained multibyte header diagnostics map to exact original byte offsets and columns. | Complete private encoding correctness remains subject to local verification. |

No faithful, safe Clang extension has been demonstrated to accept these three
remaining source constructs in the represented context. Application-owner
corrections are required for verified coverage of the affected TUs under the
current model. The tool changes distinguish exact shared-header blockers from
fallback placeholders; they do not fix application syntax/ABI contracts, promote
errored ASTs, or promise a clean private scan. The tool author has not rerun the
operator's actual-MFC-header probes; portable invented-type controls are not SDK
certification.

## Generated regression checks

```sh
python -m unittest discover -s tests -p test_report_followup.py -v
python -m unittest discover -s tests -v
```

The generated tests in `tests/test_report_followup.py` cover default mismatch,
explicit correction, per-file override, unsupported contexts, mixed-context
checking, x86/C++14 pointer and const behavior, Debug and multiline
trace/format controls, enum recovery diagnostics, shared-header partial
coverage, per-record provenance (`contained_file`, `external_file`,
`missing_file`, `missing_location`), no external path leakage, exact vs
fallback blocker grouping across TUs/contexts/inherited snapshots, CLI/query
surfaces (`scan`, `status`, `diagnostics`, `contexts`), and conservative
migration of legacy databases.
