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

## Session safety

The recorder now protects against overlapping sessions and unfinished files:

- It keeps a single active recorder session per output directory.
- If a previous recorder process is still active, a new start first asks that process to stop cleanly.
- If the app finds leftover `.partial.m4a` files from an interrupted run, it recovers and renames them before starting a new recording.
- If macOS delays finalization during shutdown, the unfinished file is preserved and recovered on the next launch instead of being discarded.

## Level safety

Every recording run now includes an arming step before the first segment starts.

- The CLI always shows a live peak meter during arming and recording.
- During arming, the recorder listens for the loudest expected signal and sets a safe fixed gain with headroom.
- That gain then stays fixed instead of continuously riding the file level.
- If later peaks still get too close to clipping, the recorder prints a warning and steps the gain down once, then keeps the new fixed value.

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

## Deployment To Another MacBook Pro M1

There are two practical deployment paths:

- Run directly from a Git clone if you control the target machine and want the current repository state.
- Use the release bundle if you want to transfer one fixed snapshot without Git history.

### Run From Git Clone

This is the simplest option for your own MacBook Pro M1 because you can pull updates later with Git and rerun the same bootstrap flow.

Step by step:

1. Clone the repository on the target Mac.
2. Move into the project directory.
3. Run the bootstrap script to create the virtual environment and install dependencies.
4. Run the post-install check to verify Python architecture and device discovery.
5. Start a short smoke test before the first long recording run.

Example:

```bash
git clone https://github.com/joergkuehnelt/recorder.git recorder
cd recorder
chmod +x scripts/bootstrap_m1.sh scripts/post_install_check.sh
./scripts/bootstrap_m1.sh
./scripts/post_install_check.sh
python -m sound_recorder --segment-minutes 1
```

What this gives you:

- the full repository remains available on the target machine
- updates can be pulled with `git pull`
- the same bootstrap and verification scripts can be reused after updates

Use this path when the target Mac has Git access to the repository and you want the easiest maintenance story.

### Run From Release Bundle

Copy the project folder to the target machine, then run:

```bash
chmod +x scripts/bootstrap_m1.sh
./scripts/bootstrap_m1.sh
```

If you want a clean transfer bundle first, create it on the source machine with:

```bash
chmod +x scripts/create_release_bundle.sh
./scripts/create_release_bundle.sh
```

That produces a versioned archive and SHA-256 checksum in `releases/`.

On the target MacBook Pro M1:

```bash
tar -xzf sound-recorder-0.1.0-macos-arm64.tar.gz
cd recorder
chmod +x scripts/bootstrap_m1.sh
./scripts/bootstrap_m1.sh
chmod +x scripts/post_install_check.sh
./scripts/post_install_check.sh
```

What the bootstrap script does:

- verifies macOS and Apple Silicon
- verifies that Python runs natively as `arm64`
- creates `.venv` if needed
- installs the project and pinned AVFoundation dependency range
- runs `compileall` as a final sanity check

If you need a specific Python executable on the target machine, use:

```bash
PYTHON_BIN=/opt/homebrew/bin/python3.11 ./scripts/bootstrap_m1.sh
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

Tune the arming time and peak thresholds:

```bash
sound-recorder --arming-duration 4 --target-peak-dbfs -12 --warning-peak-dbfs -4
```

Every recording run starts with device selection. Stop with `Ctrl-C`. The current segment is finalized and renamed before the process exits.

On a newly deployed Mac, first verify device access with:

```bash
source .venv/bin/activate
python -m sound_recorder --list-devices
```

Or run the combined post-install verification:

```bash
chmod +x scripts/post_install_check.sh
./scripts/post_install_check.sh
```

Then start a short smoke test:

```bash
python -m sound_recorder --segment-minutes 1
```

## VS Code Tasks

The workspace includes these tasks:

- `Compile recorder sources` for a syntax-only compile check over `src/`
- `List recorder devices` for the AVFoundation device scan
- `Quick recorder test (1 min)` for an interactive short recording run