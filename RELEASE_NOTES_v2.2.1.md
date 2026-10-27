# Visual Stimulator v2.2.1

Visual Stimulator v2.2.1 is the current Windows release of the open scientific visual-stimulation platform.

## Main addition — FOCUS / ENFOQUE

The v2.2 series adds an operator-controlled focus/alignment target to the experimental display. It is intended to help align the retina, preparation or visual axis before an experiment.

The focus target:

- is rendered directly by PsychoPy on the experimental display;
- is controlled manually from the operator interface;
- does not start an experimental run;
- does not generate TTL events;
- does not create experimental logs;
- cannot be activated during an experimental run;
- is automatically disabled before a stimulus starts.

## v2.2.1 correction

Version 2.2.1 corrects repetitive-substring translation artifacts in the bilingual interface by using safer word-boundary matching where appropriate. This prevents malformed interface strings caused by repeated partial replacements.

The FOCUS implementation itself is unchanged from v2.2.0.

## Experimental architecture

The established stimulus-generation, timing, protocol and TTL architecture remains unchanged by this update. The v2.0 technical manual therefore remains the reference for the validated experimental architecture, while the changelog and release notes document later interface and utility additions.

## Scope

Visual Stimulator is a general-purpose experimental visual-stimulation tool. It can be used with MEA/electrophysiology, ex vivo preparations, in vivo experiments and other setups requiring controlled visual stimulation.

## Installation

Download the packaged Windows installer from this GitHub Release.

Validate display timing, TTL behavior and any hardware-dependent aspect on the actual experimental setup before definitive scientific use.
