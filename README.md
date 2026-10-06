# MEA Visual Stimuli

**Scientific visual-stimulation software for MEA experiments, with PsychoPy presentation, reproducible protocols, timing logs, and optional TTL synchronization.**

Current release: **v2.1.3**

MEA Visual Stimuli is a Windows-oriented experimental tool for presenting controlled visual stimuli on a dedicated stimulation display while the operator works from a separate interface. It is designed around reproducibility, explicit timing records, protocol construction, and synchronization with external acquisition systems.

The current v2.1.x branch preserves the validated experimental architecture documented for v2.0 and adds a bilingual Spanish/English operator interface. English is the default language for new installations.

## Screenshots

### Operator interface

![ON/OFF configuration](docs/images/menuOnOFf.png)

### Motion stimuli

![Motion configuration](docs/images/menumotion.png)

![Moving bar stimulus](docs/images/movingbar.png)

### Noise stimuli

![Noise configuration](docs/images/noise.png)

### Protocol builder

![Protocol builder](docs/images/menuprotocols.png)

### System / TTL

![System and TTL configuration](docs/images/menuTTL.png)

### Red operator-interface mode

![Red operator-interface mode](docs/images/menured.png)

## Main capabilities

- Dedicated PsychoPy presentation worker for the experimental monitor.
- Physical display geometry expressed through screen dimensions and retina/experimental-plane distance.
- Stimulus families including:
  - ON / OFF
  - Moving Bar and Moving Spot
  - Looming, Receding, and annular motion
  - Drifting, Static and Phase-Reversal gratings
  - Static and Drifting Gabors
  - Asymmetric Drift
  - dense, Gaussian and frozen noise
  - receptive-field mapping and Sparse Noise
  - temporal stimuli including Chirp, sinusoidal/gaussian flicker, and contrast series
- Reproducible multi-step protocols with configurable pauses.
- Optional session metadata.
- Per-run logging of frame timing, TTL events, and executed configuration.
- Measured display refresh based on real `win.flip()` intervals rather than relying only on nominal monitor values.
- Detection/reporting of timing irregularities through `possible_dropped_frame`.
- Optional TTL operation: simulation, MaxOne Legacy FTDI, generic FTDI-based TTL trigger box, or disabled mode.
- UNIVERSAL frame/event synchronization and AUX EVENT state track in the validated generic TTL workflow.
- Safe abort/return-to-black behavior.
- Dark red operator-interface mode for low-light experimental environments.
- Spanish and English operator interface.

## Download and installation

For normal use, download the Windows installer from the **GitHub Releases** section.

The standalone installer is intended to run without requiring Jupyter, Anaconda, VS Code, or a separate manual Python installation.

### Current release

**v2.1.3 — Windows 10 x64 installer**

Release asset:

`Visual_Stimulator_v2_1_3_Setup_Win10_x64.exe`

SHA-256:

```text
bf733439e707f5ba65884fb50928b5024e286c601cbe646adbaba2317fd67f2f
```

## Documentation

The technical/user manual is available here:

[`docs/MEA_Visual_Stimuli_Manual_v2_0.pdf`](docs/MEA_Visual_Stimuli_Manual_v2_0.pdf)

The v2.0 manual documents the validated experimental architecture used by the v2.1.x branch. The later 2.1.x releases primarily extend the bilingual interface and presentation-layer coverage while retaining internal stimulus/protocol identifiers for compatibility.

See [`CHANGELOG.md`](CHANGELOG.md) for the subsequent maintenance history.

## Source code

The source is provided in two equivalent forms:

- [`src/MEA_Visual_Stimuli_v2_1_3.py`](src/MEA_Visual_Stimuli_v2_1_3.py) — easier to inspect, search and diff on GitHub.
- [`src/MEA_Visual_Stimuli_v2_1_3.ipynb`](src/MEA_Visual_Stimuli_v2_1_3.ipynb) — Jupyter notebook retained for development continuity.

The application uses Python/Tkinter for the operator interface and PsychoPy for experimental visual presentation. PySerial is used by FTDI-based trigger modes.

## Experimental records and reproducibility

Each execution can generate records describing the executed configuration and optional session metadata, per-frame timing, TTL events/states, and timing/diagnostic results.

For reproducibility, keep these files together with the corresponding acquisition file from the MEA/PowerLab or other recording system.

Application-generated logs and experimental CSV files are intentionally excluded from Git through `.gitignore`.

## Timing and validation

The v2.0 validation documented in the manual was performed on the specific laboratory setup described there. In the reported final test:

- measured display refresh: **60.0285 Hz**;
- a nominal 2 s period corresponded to **120 frames**;
- median interval between onsets: **2.0001 s**;
- total flip duration for a nominal 28 s protocol: approximately **28.041 s**;
- FRAME→EVENT delay: mean **3.239 ms**;
- EVENT width: mean **1.230 ms**;
- AUX pause marker: mean **1.193 ms**.

These measurements demonstrate the behavior of the tested setup; they are **not a guarantee for other hardware or operating environments**.

## Important experimental considerations

Before scientific use on a new setup:

- verify physical display dimensions and viewing/retina distance;
- confirm the measured refresh rate;
- test the intended stimuli and protocols;
- inspect timing diagnostics and `possible_dropped_frame`;
- validate TTL polarity, level, timing and cabling on the actual acquisition hardware;
- verify that AUX EVENT is in the expected LOW state before definitive acquisition when using the documented generic TTL workflow;
- calibrate monitor gamma/luminance when physical luminance or contrast is quantitatively important.

The red interface mode only changes the operator-interface palette; it does not modify the stimulation display, system gamma/LUT, or provide a substitute for controlled scotopic illumination.

## Tested acquisition workflow

The v2.0 manual documents validation with a **PowerLab 4/20T** and **LabChart**, using the generic TTL trigger-box workflow.

That hardware combination is a documented validation case, not a requirement for using the software.

## Version history

See [`CHANGELOG.md`](CHANGELOG.md).

## Artificial-intelligence-assisted development

This project has been developed with significant assistance from artificial-intelligence tools.

The application code has been generated and modified iteratively with **ChatGPT by OpenAI**, based on specifications, experimental requirements, design decisions, testing, validation observations, and requested corrections provided by **Santi Milla Navarro**.

Santi Milla Navarro has directed the functional development of the project: defining objectives and experimental behavior, testing successive versions on real hardware, identifying errors, validating experimental workflows, and specifying the required corrections and improvements. ChatGPT has acted as a development tool for code generation/modification, documentation, review, and repository organization.

This statement is included to provide transparency about the development process.

## Author

© 2026 Santi Milla Navarro

Project directed by **Santi Milla Navarro**.

## License

A license will be selected before the repository is made public.
