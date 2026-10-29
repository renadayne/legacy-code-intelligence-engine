# Post-0.3.2: confirmed context discrepancy and remaining source blockers

## What changed in the evidence

The latest redacted operator report confirms that the selected project-level
XML property is `TreatWChar_tAsBuiltInType=false`, while the analysis manifest
selects native/default wchar. This is now a reported configuration discrepancy,
not merely a hypothesis based on type widths. Imported properties, per-file
overrides and the fully evaluated original build remain unverified.

The report also identifies repeated narrowing at a shared-header sentinel,
non-trivial framework strings passed through trace/format ellipsis calls, and
a missing semicolon after a local enum before a call. These are operator-reported
findings. The tool investigation uses independently generated sources only and
has not inspected or changed the private files, manifest, database or logs.

## Delivered tool change

`explain-context` can now check an operator-supplied expected wchar mode:

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

The parsing policy remains **0.3.2**. Explanation output and validation tests
do not change semantic fingerprints, evidence, progress or acceptance rules.
No new runtime dependency or performance change is introduced.

## Operator action order

1. Review the applicable local build configuration and overrides. The explicit
   project property supports typedef mode, but may be overridden by imported
   properties, additional compiler options or per-file settings. Do not infer
   full build equivalence from a single XML property.
2. After local authorization and that review, correct only the intended manifest
   contexts by adding `/Zc:wchar_t-` to existing `extra_flags`; preserve other
   flags and settings. Remove obsolete notes saying the mode is unsupported.
   Check `per_file_flags` too: they come after context flags and the last wchar
   setting wins. This tool task does not perform that private edit.
3. Run the command above locally. For the report's selected context, verify
   `target=i686-pc-windows-msvc`, `language_standard=c++14`, typedef mode and
   the intended Unicode definitions. A minimal **fragment**, not a replacement
   manifest, is:

   ```json
   {"extra_flags": ["/Zc:wchar_t-"]}
   ```

   This mode requires `compiler_family` of `msvc` or `clang-cl` and the actual
   source/SDK typedef. Neither the checker nor parser injects that typedef.
4. Prioritize the shared-header narrowing blocker with the application owner.
   One location can prevent many TUs from becoming verified. Choosing the
   correct sentinel representation requires the application's contract; the
   tool must not replace it with a guessed value or suppress the diagnostic.
5. Review trace/format API argument contracts locally. A class conversion
   operator does not make ellipsis a typed call. An explicit character pointer
   or a typed overload is only appropriate when the actual API and lifetime
   requirements support it. Do not switch Debug to Release to hide failures.
6. Have the application owner address the missing enum semicolon as a separate
   source-correctness decision. Clang recovery can report a result-type error
   and secondary overload errors; the tool must not insert punctuation.
7. Only a separately authorized local rescan can establish private acceptance.
   Compare per-class diagnostics and verified coverage, not just publication
   completion. Changing manifest flags triggers existing context invalidation;
   source/header changes trigger existing content invalidation. Error-limit
   termination can hide later failures. No raw artifacts need to be shared.

## Per-class outcomes

| Class | Current conclusion | Resolution owner |
| --- | --- | --- |
| Native/default versus project typedef wchar | Confirmed discrepancy in the operator report. Existing `/Zc:wchar_t-` support handles generated Unicode conversion and explicit unsigned-short-pointer API cases on x86/C++14. Const mismatch still fails. | Operator checks overrides and corrects the intended manifest; no global parser default change. |
| Repeated narrowing sentinel | Both wchar modes reject the generated negative unsigned initializer. Two generated TUs report the same header location and stay excluded; an independent clean TU can still publish. | Application owner reviews the shared initializer. No faithful new compiler extension has been established here. |
| Trace and format varargs | Generated Debug trace wrapper and format member both reject non-trivial objects. Typed and explicit-pointer controls pass. Release hides only the generated trace branch, demonstrating a context change, not a Debug fix. | Application owner checks actual framework/API contracts; stubs do not establish MFC ABI compatibility. |
| Enum without semicolon | Generated local enum followed by a call produces result-type and secondary return-type diagnostics. Adding the semicolon in the generated control passes in both modes. | Application owner decides the private syntax correction; no source rewrite or parser recovery promotion. |
| Error limit | Still truncates diagnostics; correcting earlier blockers can reveal more errors. | Operator reviews results after authorized corrections. Raising the limit is not a fix. |
| Encoding | Existing CP932/UTF-8 overlays, original-byte evidence, BOM/CRLF tests remain. ASCII fixtures reproduce the current semantic classes. | These errors do not require encoding failure; complete private encoding correctness remains unproven. |

There is no new demonstrated parser compatibility defect in these reported
constructs. The delivered code prevents a configuration mismatch from passing
the requested expectation check; it does not itself expand semantic coverage.
Expanded verified coverage requires the relevant local configuration correction
and, for independent invalid constructs, application-owner action. This is not
a reason to relax the verified-graph contract.

## Generated regression checks

```sh
python -m unittest discover -s tests -p test_report_followup.py -v
python -m unittest discover -s tests -v
```

The seven new tests cover default mismatch, explicit correction, per-file
override, unsupported contexts, mixed-context checking, x86/C++14 pointer and
const behavior, Debug trace/format controls, enum recovery diagnostics and
shared-header partial coverage. The latter also verifies recovery after a
generated header edit and a subsequent unchanged no-op scan.
All inputs are created in temporary directories. No private scan or MSBuild
evaluation is performed; no new benchmark or speed claim accompanies this work.

Validation on Linux, Python 3.12.14 and libclang 18.1.1: all 138 tests passed,
with no skips, including the existing containment/symlink, encoding/evidence,
progress, atomic publication and HTTP regressions.
