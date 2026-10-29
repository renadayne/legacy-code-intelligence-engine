# Semantic parser input and original-byte evidence

LegacyGraph 0.3.0 parser policy (libclang 18.1.1): inventory decoding alone
cannot configure Clang's source decoder. The generated CP932 probe reproduces
`illegal character encoding in string literal`: warning for a narrow literal,
error for a wide literal with the Windows MSVC target. The UTF-8 views remove
those encoding diagnostics. No application source was used for this work.

## Use

From the checkout, with the semantic dependency installed:

```sh
python -m legacygraph scan --repo examples/mfc_demo \
  --manifest examples/mfc_demo/manifest.json --db .legacygraph/demo.db \
  --legacy-encoding cp932 --parser-input utf8-overlay --json
python -m legacygraph status --db .legacygraph/demo.db \
  --check-invalidated --legacy-encoding cp932 --parser-input utf8-overlay --json
```

`utf8-overlay` is the default analysis model. `native` retains Clang's filesystem
byte input for comparison; it does not fix Clang's CP932 decoding. Status and
scan-manifest checks accept the same policy options; use the policy intended for
the next scan. `-finput-charset=cp932` is not used or advertised as supported.

The decoder takes BOMs first (UTF-8, UTF-16 LE/BE, UTF-32 LE/BE), then strict
UTF-8, then the explicitly configured fallback. Supported fallback codecs are
CP932, Shift-JIS, CP1252, Latin-1, ASCII and UTF-8, including Python codec aliases.
The parser requires a strict, byte-for-byte round trip, reports unsupported
input explicitly, and never silently tries another legacy codec or replacement
characters. A file whose bytes are valid UTF-8 is interpreted as UTF-8; this is
a deterministic policy, not language/encoding detection. Arbitrary mixed codecs
within a single file and stateful codecs are unsupported. Inventory alone keeps
its existing permissive fallback chain; its success is not parser acceptance.

## Include discovery and evidence

Clang discovers the actual include graph; no repository-wide header rewrite or
SDK transcoding is performed. The main file and explicit contained forced
headers are supplied as in-memory UTF-8 views. Consumed contained headers of any
extension are decoded from Clang's consumed buffers and added as overlays when
needed. Clang may reject a UTF-16/32 header before exposing its contents: that
round is used only for discovery, then a frozen disk snapshot supplies the next
overlay. Parsing repeats until no new overlay is required, with a limit of 64
rounds; exhaustion is an explicit failure, never verified partial AST data.
Repeated parses add cost, especially with nested legacy headers.

Only canonical paths inside the repository get overlays. External SDK headers
and symlinks escaping the root remain outside the overlay. Existing compiler
include access and external-symbol reference behavior are unchanged. Intermediate
discovery diagnostics/ASTs are discarded; final diagnostics determine readiness.

Each transformed buffer retains character-boundary maps to original bytes.
Entity occurrences, call sites, observations, relation evidence, macro offsets
and diagnostics use mapped original offsets. Lines are physical source lines;
columns are one-based **original byte columns**, including CRLF and multibyte
encodings, not display-character columns. A location without a contained source
still uses the existing main-file fallback; it does not prove the diagnostic
originated there. Signatures and call text decode original slices strictly,
without replacement characters. Evidence hashes and file hashes use original
bytes, and source files are never rewritten.

The final consumed buffers must match the expected overlay (or native bytes),
and original disk hashes must still match frozen originals. Intentional
transcoding is not mutation. Actual source changes or unexpected buffers mark
the TU stale; scanner checks still cover changes before persistence and during
MFC augmentation. As with hash-based snapshot validation generally, a change
that is reverted between observations cannot be detected.

The stored parser version includes the input-policy version, mode and canonical
fallback codec. A policy change invalidates previous parse runs even when bytes
are unchanged; equivalent codec aliases reuse runs. No database schema migration
is needed. File versions remain keyed by original hash. When displaying new
semantic evidence, snippets use the parse run's frozen encoding policy rather
than potentially older inventory decoding metadata for the same bytes. Historical
runs without a policy continue using their recorded inventory encoding.

Failed/stale TU exclusion, atomic publication/rollback and build-context isolation
remain in force. A published generation can have incomplete coverage: inspect
`failed_translation_units`, TU diagnostics and `verified_tu_runs` rather than
using publication alone as success. CLI scan returns 3 for failed requested TUs.

## Semantic boundaries

This is a Unicode source **analysis model**, not exact legacy-build emulation.
Transcoding narrow literals can change byte representation, `sizeof`, character
values, conditional compilation and compile-time behavior. Even a clean AST
therefore does not establish equivalence to the original compiler. `wchar_t`
mode and other compiler/ABI settings are separate concerns.

Non-trivial objects passed through varargs, types defined in result declarations,
nested block-comment warnings and trailing preprocessor tokens remain independent
compatibility issues. This change neither suppresses them nor establishes their
private-source cause. Error limits are not raised as a compatibility fix. No
private-project acceptance or Windows SDK/MFC certification is claimed; any such
evaluation remains separate and is not requested from the user by this workflow.

## Validation and synthetic measurement

Executed on Linux, Python 3.12.14, libclang 18.1.1. The suite includes 11 new
generated-fixture tests (95 tests total): independent narrow/wide CP932 failure,
main/header and transitive mixed encoding, BOMs, CRLF, original byte columns,
call evidence hashes, legacy signatures, diagnostics after multibyte text,
MFC-like active/inactive/comment registrations, original snippets and hashes,
mutation/incorrect-buffer rejection, external-header containment, incremental
no-op, header edits, policy invalidation and partial coverage. Existing atomic
rollback and context-isolation suites also pass.

Reproduce the benchmark without any application input:

```sh
python scripts/benchmark_encoding.py --functions 500
```

One observed run (generated 3-file fixture, 38,561 original bytes, 500 main-file
functions, 1,002 occurrences, zero diagnostics):

| Measurement | Result |
| --- | ---: |
| UTF-8 views and byte-map construction | 0.012317 s |
| Peak traced Python allocation for view/map construction, separate run | 761,732 bytes |
| Semantic parse plus extraction and validation | 0.548226 s |
| Native libclang parse calls within that parse | 2 |
| Sum of native parse call wall time | 0.016057 s |
| First database scan, including discovery, parse, MFC and persistence | 0.681435 s |
| Unchanged incremental scan (verified no-op) | 0.003317 s |

These are single-run wall timings with no controlled before/after baseline and
no cold OS-cache guarantee. Native parse timing excludes subsequent lazy AST
traversal. The allocation measurement excludes native Clang/SQLite memory and
is not whole-process RSS. This does not identify a private workload bottleneck,
prove a speed improvement, or establish enterprise-scale performance. Mapping
construction is linear in source size and keeps compact arrays for transformed
files; UTF-8 files without a BOM need no offset arrays.
