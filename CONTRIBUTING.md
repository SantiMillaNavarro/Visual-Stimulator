# Contributing to Visual Stimulator

Thank you for considering a contribution.

Visual Stimulator is intended to remain a free, open and actively improved scientific tool for controlled visual stimulation across MEA/electrophysiology, ex vivo, in vivo and other experimental workflows.

## Ways to contribute

You can contribute by:

- reporting reproducible bugs;
- proposing new stimuli or protocol features;
- improving compatibility with displays, acquisition systems or trigger hardware;
- improving documentation or translations;
- submitting tested code changes through pull requests.

For substantial changes, opening an Issue first is encouraged so the intended behavior and compatibility implications can be discussed.

## Scientific and timing-sensitive changes

Changes affecting any of the following should be treated as experimentally significant:

- stimulus geometry or temporal behavior;
- display-refresh measurement or frame pacing;
- `win.flip()` scheduling;
- TTL generation, polarity, pulse width or event timing;
- protocol execution, pause/resume behavior or saved protocol compatibility;
- logging used for reproducibility or quality control.

When possible, include:

- operating system and relevant hardware;
- display refresh and display configuration;
- the stimulus/protocol used for testing;
- timing or TTL logs before and after the change;
- acquisition hardware used for physical validation, if applicable;
- any known limitations.

A change that works on one setup should not automatically be described as universally validated.

## Compatibility

Please avoid changing existing internal stimulus identifiers, protocol schema values or exported protocol identifiers unless a migration path is provided. Backward compatibility with existing protocols is important.

## Experimental data and privacy

Do not commit laboratory logs, session metadata, CSV files or other experimental records containing information that should not be public. The repository `.gitignore` excludes the common local output paths, but contributors remain responsible for checking commits before publication.

## Code and documentation

Keep changes focused and explain why they are needed. Documentation should distinguish between:

- behavior implemented in software;
- behavior validated experimentally;
- behavior expected but not yet physically validated.

## License

By contributing code to this repository, you agree that your contribution may be distributed under the project's **GNU General Public License v3.0 or later (GPL-3.0-or-later)**.

## Citation

If Visual Stimulator contributes to published research, citation of the software and the version used is appreciated. See `CITATION.cff` for citation metadata.
