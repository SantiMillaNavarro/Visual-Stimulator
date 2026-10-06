# MEA Visual Stimuli v2.1.3

Stable Windows release of MEA Visual Stimuli.

## Highlights

- PsychoPy-based visual stimulation for MEA and related experimental workflows.
- Dedicated stimulation display with separate operator interface.
- Reproducible multi-step protocols.
- ON/OFF, motion, gratings, Gabors, Asymmetric Drift, noise, receptive-field mapping and temporal stimuli.
- Per-frame timing records and experimental configuration logs.
- Optional TTL synchronization through simulation, MaxOne Legacy FTDI or a generic FTDI-based trigger box.
- Bilingual Spanish/English operator interface.
- English is the default language for new installations.
- Expanded translation coverage in v2.1.3 while retaining internal protocol/stimulus identifiers for compatibility.

## Windows installer

`Visual_Stimulator_v2_1_3_Setup_Win10_x64.exe`

SHA-256:

```text
bf733439e707f5ba65884fb50928b5024e286c601cbe646adbaba2317fd67f2f
```

The installer is intended to run without requiring Jupyter, Anaconda, VS Code or a separate manual Python installation.

## Documentation

The v2.0 user/technical manual documents the validated experimental architecture used by the v2.1.x branch.

## Validation note

Timing and TTL values reported in the manual apply to the specific tested laboratory setup and should not be interpreted as guaranteed values for different monitors, FTDI hardware, cabling or acquisition systems. Validate the actual experimental setup before scientific use.
