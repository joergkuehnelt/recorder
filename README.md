# sound-recorder

Lean macOS command-line recorder for Apple Silicon. Every recording run starts with device selection, then continues in rolling 60-minute segments until you stop it.

## Why this architecture

This project uses native macOS AVFoundation APIs through PyObjC instead of PortAudio-based wrappers.

- Native capture path on macOS reduces moving parts on M1 and M2 machines.
- AVFoundation writes directly to finalized audio files, which avoids the usual callback queue and buffer-drain issues seen in user-space audio loops.
- Device discovery comes from the same framework family as capture, so the CLI stays lean.

## Chosen file format

The recorder writes `.m4a` files with AAC audio.

- Smaller than uncompressed WAV for 60-minute chunks.
- Native container and codec support on macOS.
- Clean finalization when a segment closes.

If you need lossless output later, the implementation can be switched to CAF without changing the chunk rotation model.

## File naming

Each finished segment is renamed after recording completes:

`DDMMYYYY-startHHMM-endHHMM.m4a`

Example:

`19052026-start0915-end1015.m4a`

## Requirements

- macOS on Apple Silicon
- Python 3.9 or newer running as arm64, with 3.11 recommended
- Microphone permission granted to the terminal app you use

## Setup

```bash
python3 -m venv .venv
source .venv/bin/activate
python -m pip install --upgrade pip
python -m pip install -e .
```

To confirm you are on a native Apple Silicon Python:

```bash
python -c "import platform; print(platform.machine())"
```

Expected output:

```text
arm64
```

## Usage

List devices only:

```bash
sound-recorder --list-devices
```

Start an interactive recording session:

```bash
sound-recorder
```

Write recordings to a specific folder:

```bash
sound-recorder --output-dir ./captures
```

Run a quick 1-minute test:

```bash
sound-recorder --segment-minutes 1
```

Every recording run starts with device selection. Stop with `Ctrl-C`. The current segment is finalized and renamed before the process exits.

## VS Code Tasks

The workspace includes these tasks:

- `Compile recorder sources` for a syntax-only compile check over `src/`
- `List recorder devices` for the AVFoundation device scan
- `Quick recorder test (1 min)` for an interactive short recording run