# CI Test Reporters (`reporters/`)

Zero-dependency JavaScript reporters that format Datalog AI experiment comparisons and test summaries in CI pipelines:

- [jest.cjs](jest.cjs) — Jest custom reporter (`CommonJS`).
- [vitest.mjs](vitest.mjs) — Vitest custom reporter (`ESM`).
- [summary.mjs](summary.mjs) — Shared summary formatter and threshold gate helpers.

Each reporter prints a one-line summary and, with the `outputFile` option, writes a JSON summary (`passed`, `failed`, `skipped`, `passRate`, `cases`) that a CI step can attach or upload next to a Datalog experiment.
