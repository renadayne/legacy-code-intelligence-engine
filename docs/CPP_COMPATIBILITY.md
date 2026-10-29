# C++ compatibility and AST performance (parser 0.3.1)

## Match the original wchar_t build mode

Parser 0.3.1 recognizes `/Zc:wchar_t` and `/Zc:wchar_t-` in build-context
`extra_flags` and translation-unit `per_file_flags`. They require
`compiler_family: "msvc"` or `"clang-cl"`, which selects the existing Windows
target configuration. Other compiler families report
`clang.unsupported_wchar_context` and fail the TU explicitly.

| Setting | Analysis behavior |
| --- | --- |
| Absent, or `/Zc:wchar_t` | Native, distinct C++ `wchar_t` type |
| `/Zc:wchar_t-` | Typedef mode using libclang's `-Xclang -fno-wchar` |

Choose the setting from the original build. Do not switch modes solely to remove
a diagnostic: overload identities differ even when `wchar_t` and
`unsigned short` have the same width. In typedef mode, source or SDK headers
must supply the typedef; LegacyGraph does not inject it. The standalone driver
argument `-fno-wchar` is rejected by the tested libclang 18.1.1.

Context flags are processed first, then per-file flags; the last wchar setting
wins. Native mode uses Clang's default rather than an unsupported inverse flag.
The compile database importer retains the original `/Zc:wchar_t-` setting.

Example context fragment for a build that actually uses typedef mode:

```json
{
  "id": "legacy-debug",
  "project_name": "LegacyApp",
  "compiler_family": "msvc",
  "platform": "x64",
  "extra_flags": ["/Zc:wchar_t-"],
  "defines": ["UNICODE", "_UNICODE"]
}
```

The `UNICODE` definitions above are an example; use the actual build's defines,
include paths and headers. Wide/narrow text selection is separate from whether
`wchar_t` is native. No runtime dependency was added.

## Diagnostics that still need source/build investigation

The encoding overlay and wchar mode do not make all legacy C++ valid:

- Native `wchar_t*` and `unsigned short*` are distinct pointer types.
- A `const wchar_t*` cannot be passed to an API requiring writable `wchar_t*`.
- List-initialization narrowing remains an error.
- Passing a non-trivial object through `...` remains an error. A typed overload
  or an explicitly obtained pointer may be appropriate depending on the API.
- Defining a type in a function result declaration remains an error.
- String-literal constness, nested comments and trailing preprocessor tokens
  retain their diagnostics; the error limit is not raised to mask failures.

Failed or stale TUs publish no verified entities/relations. Inspect scan
`failed_translation_units` and diagnostics; CLI exit 3 indicates failed TUs.
Synthetic fixtures reproduce these categories, not the cause in any private
project. Windows SDK/MFC and private-source acceptance remain unverified.

## Bounded AST path cache

Repeated canonical path checks dominated the generated AST profile. The parser
now caches at most 4,096 exact path spellings, including rejected paths, only
during one `ast_evidence` phase. It preserves native filesystem semantics and
does not case-fold or merge spellings. Further spellings use ordinary checks.
The cache is cleared on exit, including exceptions, and is never reused across
translation units.

Before accepting the payload, `parser_source_validation` resolves all cached
spellings again with caching disabled. A changed result, including a newly
present path or a symlink moving into/out of the root, makes the TU stale.
Existing original-byte hashes and consumed-buffer validation still apply.
Use a stable source snapshot: these checks are not filesystem locking and do
not establish protection against every possible concurrent change.

Parser version increases from 0.3.0 to 0.3.1. The next ordinary scan invalidates
old parser runs and reparses their TUs; subsequent unchanged scans are no-ops.
Changing wchar flags also changes context identity and preserves isolation.
No database schema migration is required.

## Reproduce validation

From the checkout with Python 3.11+ and `libclang==18.1.1`:

```sh
python -m unittest discover -s tests -v
python scripts/benchmark_ast.py --declarations 300 --compare --repeats 3
```

The comparison reads the fixed parser at Git commit
`c889a85dd069448f2c52cba525d92900dddb0a60`; that commit must exist locally.
It creates its own temporary files and accepts no application path or manifest.
For payload comparison only, both parsers receive the same version label, since
production evidence correctly carries different parser versions.

The recovered implementation passed 125 tests on Linux with Python 3.12.14 and
libclang 18.1.1, with no skips. Seventeen added tests cover type/overload behavior,
context invalidation, cache lifetime/bounds, encoding/evidence parity, symlink
retargeting, disappearance and negative-path revalidation.

Measured means from three alternating, unprofiled runs per implementation:

| Headers | TU | Input | AST baseline (s) | AST 0.3.1 (s) | Parse baseline (s) | Parse 0.3.1 (s) |
| --- | --- | --- | ---: | ---: | ---: | ---: |
| In repo | Clean | UTF-8 | 0.548963 | 0.102505 | 0.557490 | 0.110934 |
| In repo | Clean | CP932 | 0.552370 | 0.098268 | 0.560140 | 0.106643 |
| In repo | Error | UTF-8 | 0.048921 | 0.007813 | 0.056796 | 0.015615 |
| In repo | Error | CP932 | 0.047379 | 0.007786 | 0.054492 | 0.016010 |
| External | Clean | UTF-8 | 0.049123 | 0.006564 | 0.055358 | 0.013439 |
| External | Clean | CP932 | 0.048362 | 0.007135 | 0.055399 | 0.014695 |
| External | Error | UTF-8 | 0.047118 | 0.006636 | 0.060168 | 0.014223 |
| External | Error | CP932 | 0.045230 | 0.006039 | 0.052089 | 0.013944 |

Each case generates 300 declarations, shared headers and one TU, totaling
30,785–30,876 bytes. All eight comparisons assert identical payloads (entities,
relations, evidence, diagnostics and dependencies) and macro/file metadata.
The JSON output includes separate profiles, raw paired timings, fixture sizes
and phase means. Profiling overhead is excluded from the paired means.
Observed AST speedups of about 5.4–7.5x apply only to these synthetic inputs.
External headers and erroneous TUs intentionally receive less AST traversal;
these measurements do not predict full scans or private-project performance.
