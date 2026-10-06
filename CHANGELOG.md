# Changelog

All notable changes to Visual Stimulator are documented here.

## Repository identity and licensing — 2026-10-06

- Public project name standardized as **Visual Stimulator** to reflect its general experimental scope beyond MEA.
- Historical v2.0 manual naming and selected internal compatibility identifiers are retained for traceability and backward compatibility.
- Project licensed under **GPL-3.0-or-later**.
- Added `CITATION.cff` to support scientific citation.

## v2.1.3

- Deep bilingual-interface audit covering secondary windows, runtime status strings, validation paths, and dynamically assembled labels.
- Translation remains presentation-only: internal protocol identifiers and saved values are unchanged.
- Current application version reported by the source: `2.1.3`.

## v2.1.2

- Expanded bilingual-interface coverage across static labels, runtime status strings, previews, protocol-builder text, validation messages, and dialogs.
- Translation remains presentation-only.

## v2.1.1

- English becomes the default operator-interface language for new/public installations.
- Language preference is persisted in the local configuration.
- Spanish remains available from the application interface.

## v2.1.0

- Added bilingual Spanish/English operator interface.
- Internal stimulus identifiers and saved protocol values remain unchanged to preserve compatibility with the v2.0.1 branch.

## v2.0.1

Maintenance release based directly on v2.0 Stable.

- Moved `Manual / Ayuda` to the top application bar while retaining the `F1` shortcut.
- Removed the duplicated manual button from `Sistema / TTL`.
- Kept offline-manual discovery compatible with development and packaged installations.

## v2.0

Stable release built directly from v1.32.

- Preserved the validated v1.32 experimental logic: stimuli, timing, PsychoPy worker, protocols, MaxOne Legacy FTDI, generic TTL trigger box, UNIVERSAL, AUX EVENT, pause markers, and Asymmetric Drift.
- Added offline documentation through `MEA_Visual_Stimuli_Manual_v2_0.pdf`.
- Prepared the stable branch for standalone executable/offline installation.

## Earlier development history

The v2.0 stable branch was built on the validated v1.32 architecture. Earlier development included, among other changes:

- robust measured-refresh timing based on real `win.flip()` intervals (v1.32);
- Asymmetric Drift stimulus mode (v1.30);
- absolute monotonic pacing for multi-monitor timing (v1.29);
- protocol-pause markers in AUX EVENT (v1.28);
- AUX EVENT as a readable experimental-state track while UNIVERSAL retains frame/event synchronization (v1.27).

For the experimental rationale, timing details, TTL architecture, and validation measurements, see the v2.0 technical manual in `docs/`.
