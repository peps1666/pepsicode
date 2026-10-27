# qq-bridge utilities

- Source: https://github.com/Derpyu520/qq-bridge
- Commit: `a49877f4e58d04e47532d44b803360d0f001160d`
- Retrieved: 2026-10-27
- License: MIT; original copyright notice and complete upstream LICENSE retained.
- `md-to-plain.js`: unmodified upstream file; Markdown conversion and Unicode-safe
  QQ message splitting are used directly by `src/bridge.js`.
- `test-md-to-plain.mjs`: upstream regression script; only the import path is
  changed to `./md-to-plain.js`, so it runs against the vendored file.
- `atomic-write.js`: extracted `renameWithRetry` and `atomicWriteJson` from
  `src/bridge.js`. Imports and the export modifier were added; implementation
  is unchanged (comments abbreviated). Reused for Windows file-lock retries
  and atomic state replacement.

The rest of the upstream repository is not part of this integration. Its main
bridge requires DSH-specific authentication, sessions and APIs. Pepsi uses its
own existing authenticated agent server instead. No SnowLuma SDK, MCP catalogue,
client binary or installer is redistributed. The user supplies a running
OneBot v11 gateway. Upstream third-party license commentary is retained as part
of the original LICENSE file, not adopted as additional MIT conditions.
