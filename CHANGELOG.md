# Changelog

## 1.3.1 — 2026-09-19

- Recover ATVV voice sessions after failed GATT operations or connection loss,
  including loss during capture; cancel hung operations and reject stale callbacks.
- Add bounded reconnect backoff and rotating recovery diagnostics.
- Keep daily and candidate build identities explicit; support normal daily
  autostart while preserving isolated candidate state.
- Harden config persistence, timer cancellation and clipboard delivery failures.
- Restore the original horizontal control-page image.
- Add regression tests and document build profiles and verification limits.

See [voice recovery notes](docs/VOICE_RECOVERY_1_3_1.md). WeType remains in
release-playback mode. Long-duration physical-device recovery is not yet
established across hardware/firmware combinations.

## 1.3 — 2026-09-14

- Per-key click, double-click and long-press actions, hold-to-repeat, semantic
  actions and key-card mapping UI.
