# Sample project rules

This is a small teaching corpus, not a required style for other repositories.

- **[ERR.1]** Propagate configuration parsing errors; do not silently substitute a successful-looking default. (should-fix)
- **[REL.1]** Preserve the existing usable configuration until its replacement is prepared and validated; a failed replacement must retain or restore usable state. (should-fix) <!-- digest: Preserve usable configuration until replacement is prepared and validated; retain or restore usable state on failure -->
  Scope: ordinary local configuration files, one writer, no concurrent replacement.
  Prepared means serialization, validation and file writing have completed before switching.
  These examples do not establish power-loss durability or concurrent-reader guarantees.
- **[R.1]** Review correctness as well as these rules. Use unmapped for a real defect no code rule covers.
