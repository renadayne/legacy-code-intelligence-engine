# Remaining semantic errors: investigation and parser 0.3.2

The [post-0.3.2 follow-up](POST_032_CONTEXT_HANDOFF.md) records the newer
operator-confirmed wchar discrepancy, shared-header blocker and enum syntax
finding, plus `--expect-wchar-mode` validation. The earlier unknown local
selection discussed below is superseded by that report, not by a private scan
performed by this agent.

This investigation uses the tool and invented C++ fixtures only. It does not
establish the effective settings, source causes or acceptance of a private
application. A committed partial generation still excludes errored TUs;
completion of scan/output is not full semantic coverage.

## Confirmed tool defect and fix

Parser 0.3.1 passed arbitrary `-Wno-*` flags to Clang. On generated inputs,
`-Wno-c++11-narrowing` changed an erroneous unsigned list initializer into a
`parsed_clean` result with entities. `-Wno-non-pod-varargs` did the same for a
non-trivial ellipsis argument. This could violate verified-graph expectations.
It is a confirmed tool defect, not evidence those flags were used in the local
run described by the report.

Parser 0.3.2 rejects arbitrary diagnostic suppressions and blanket `-w` as
unsupported configuration. Only the existing small allowlist for unknown
pragmas, pragma-once outside headers and Microsoft include warnings remains.
Rejection returns a failed TU with `clang.unsupported_flag`, no verified facts
and scan exit 3. It does not silently strip a flag or downgrade an error.
Remove such flags from an analysis manifest only after reviewing the original
build; remaining source errors must still be addressed or kept unsupported.

The parser version changes because acceptance policy changed. Ordinary scans
invalidate 0.3.1 results, including previously clean ones, and retract facts
when the TU now fails. No database migration or runtime dependency is added.
The new explanation command itself causes no invalidation or database writes.

## Per-class findings

| Class | Confirmed using invented input | Supported action or remaining limit |
| --- | --- | --- |
| Signed-to-unsigned narrowing | `unsigned` aggregate initialized with `-1` errors under the selected Windows C++ model. A representable unsigned value is a clean control. | No faithful new compatibility behavior is established. Keep the error; operator must review original language mode and source intent locally. No global suppression or source rewrite. |
| Non-trivial varargs | Free-function and member ellipsis calls with a non-trivial string-like object error; typed calls and explicitly obtained pointers are clean controls. An implicit pointer conversion is not a general ellipsis fix. | No MFC/ABI compatibility fix is established by these stubs. The operator must review the actual API contract locally; no application execution occurred. |
| `wchar_t*` / `unsigned short*`, text overloads | Full manifest → context/per-file flags → argument construction → types path reproduces a failing native-mode call and a clean typedef-mode control. Native/typedef, const, Unicode/non-Unicode and overrides remain tested. | Existing 0.3.1 support is preserved. Choose mode from the original build and inspect the effective analysis setting. The private setting and whether it explains any given overload failure remain unknown. |
| Type defined in function result | Invented inline type definition in a result declaration errors; a separate declaration is a clean control. | No supported extension fix was established. Do not reconstruct or rewrite the private declaration. |
| Error limit | Existing generated many-error test reaches Clang's error limit. | This is truncation, not another root cause. Later errors may remain hidden; increasing the limit is not a fix. |
| Lexical/preprocessor warnings | Literal constness, nested comments and trailing directive tokens remain visible in generated tests. | Preserve diagnostics and avoid attributing unknown/fallback locations to a private source cause. |
| Historical encoding | Direct libclang probe again produces error severity 3 for CP932 wide literals and warning severity 2 for narrow literals; corresponding UTF-8 buffers have neither diagnostic. | Existing overlays and original-byte mapping remain. Absence of recorded encoding errors under an error limit is not proof of complete encoding correctness. |

Switching wchar mode does not remove constness, fix narrowing, make varargs ABI
safe, or repair declaration syntax. Equal type widths do not imply identity.
No additional supported compiler extension resolving these classes has been
demonstrated. Verified coverage cannot be promised without correct build facts
and, for genuinely invalid constructs, authorized source changes. This task
does not introduce a relaxed/recovered-AST trust model.

## Local operator validation without sharing source

Run from the tool checkout, using local values for the placeholders:

```text
python -m legacygraph explain-context --repo <source-root> --manifest <manifest-path>
```

This opt-in JSON command imports the manifest and calls the same argument builder
as parsing. It does not read source contents, start native parsing, or open an
index. Ordinary output omits paths, identifiers, raw compiler arguments and
macro values. `imported_tu_ordinal` identifies the order of successfully imported
TUs, not original manifest positions when entries were skipped.

1. Check `import_warning_count` and `configuration_supported`. Exit 3 means an
   empty import, import warnings, or unsupported configuration; do not interpret
   such output as verified settings. Diagnostic codes identify rejected policy.
2. Check `target`, `language_standard`, `wchar_mode` and `wchar_selection` against
   the original build locally. `default` indicates no explicit wchar flag;
   it does not establish that the default matches the build. The importer has
   existing MSVC defaults: make compiler family and architecture explicit when
   needed. This command describes the selected model, not compiler discovery.
3. `command_line_macros` reports only `defined`, `undefined`, or `unspecified`
   for `UNICODE` and `_UNICODE`. A macro defined as zero is still defined for
   `#ifdef`. Headers/source may redefine macros. Consequently
   `source_preprocessing_verified` is false and `original_build_match` unknown.
4. Select native or typedef mode only using original build facts. Context flags
   precede per-file flags; the last wchar setting wins. Typedef mode requires
   the real source/SDK typedef and an MSVC/clang-cl context. Do not inject one
   simply to eliminate a diagnostic.
5. An authorized operator can rescan locally with the existing encoding/progress
   options, then inspect exit status, errored/failed TU counts and diagnostics.
   Publication and completed output do not cancel partial coverage. No source,
   manifest, paths, logs or database exports need to be sent to the external agent.

## Regression validation

```sh
python -m unittest discover -s tests -p test_semantic_report.py -v
python -m unittest discover -s tests -v
```

The new generated regressions exercise context explanation without source/native
library/database access, manifest overrides and actual overload identities,
invalid configuration, suppression rejection, method varargs, policy upgrade
retraction and incremental no-op after a valid configuration is restored.
The full suite retains original-byte/encoding, cache containment and symlinks,
source mutation, MFC macro activity, atomic publication, progress and HTTP checks.
Validation on Linux, Python 3.12.14 and libclang 18.1.1: all 131 tests passed,
none skipped; the bundled demo parsed both TUs cleanly with parser 0.3.2.
No performance change or new performance benchmark is part of this update.
