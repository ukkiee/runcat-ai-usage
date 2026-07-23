# Decisions — usage-reading

### design r1

- R-1 accept — File rename breaks existing launchd installations immediately
- R-2 accept — Legacy Claude scale drift is not contained by clamping; remedy narrowed to recording the observed scale evidence and preserving last-good when the legacy shape cannot be trusted, rather than inferring scale
- R-3 accept — Discarding an invalid reset can overwrite the only recoverable reset state

### design r2

- R-1b accept — R-1 still open: one shipped release does not migrate installed plists; remedy sharpened by the human from an indefinite shim awaiting removal to a permanent split, where the entry point never moves and the implementation lives beside it, so no removal date and no plist migration ever exist

### design r3

- approve — 0 findings. Round 3 was authorised explicitly by the human after round 2 returned needs-attention. Gate passed on verdict, not on waiver.
