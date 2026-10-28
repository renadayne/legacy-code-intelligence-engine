# Implementation acceptance

Each implementation step is reviewed and tested by Codex before a separate commit is pushed to `origin/main`.

| Step | Acceptance evidence |
| --- | --- |
| Foundation | Package/CLI run on Python 3.11+, SQLite persists contexts and observations, transactional updates retract deleted inputs, failed writes preserve the published index. |
| Semantic indexing | Real libclang parses the bundled C++ fixture, resolves overloads without name guessing, preserves context/internal-linkage isolation, records indirect/virtual calls honestly, and invalidates consumed headers. |
| MFC and resources | Supported macros produce source-located event-registration evidence; comments/strings and inactive or unknown branches cannot become confirmed runtime calls. |
| Queries and export | Search, callers/callees, bounded paths, and Markdown/JSON exports work end to end; budget truncation and source freshness are explicit. |
| Explorer | Local-only static UI loads bounded graph fragments, shows source/evidence, supports expansion/filtering/path queries, and exports packs without external network assets. |
| Release review | Unit/integration suites pass; documented demo works from a fresh index; no source modification; optional parser absence is handled; Git working tree is clean after the final push. |

The bundled sample uses MFC-like macro stubs so it can be parsed without Windows SDK/MFC installation. It does not establish compatibility with a company's real MFC codebase or prove 1–5 million LoC performance. Those validations require the internal pilot described in `IMPLEMENTATION_PLAN.vi.md`.

Runtime must not download dependencies, execute build commands from imported manifests, upload source, or require frontend build tooling. libclang is an explicitly installed optional dependency for semantic indexing; metadata/query/export functionality should fail clearly or remain available as appropriate when it is missing.
