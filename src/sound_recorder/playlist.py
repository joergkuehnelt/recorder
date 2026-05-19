from __future__ import annotations

import json
import os
import re
import shlex
import shutil
import subprocess
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Callable, Dict, List, Optional


APP_SUPPORT_DIR = Path.home() / "Library" / "Application Support" / "sound-recorder"
LOCAL_STATE_PATH = APP_SUPPORT_DIR / "local-state.json"
SCRIPT_SUFFIXES = {".sh", ".command", ".py"}
PLAYLIST_KEYWORDS = ("playlist", "play-list", "play_list")
SONG_HISTORY_TOKEN = "song_history"
LAST_STATE_NAME = "last_state.json"
URL_RE = re.compile(r"\s*(?:\||-)?\s*https?://\S+")


@dataclass
class PlaylistLaunchResult:
    started: bool
    script_path: Optional[Path] = None
    song_history_path: Optional[Path] = None
    last_entry: Optional[str] = None
    last_state_path: Optional[Path] = None
    last_state_entry: Optional[str] = None


def maybe_start_playlist_companion(
    input_func: Callable[[str], str] = input,
    print_func: Callable[..., None] = print,
    documents_dir: Optional[Path] = None,
    state_path: Path = LOCAL_STATE_PATH,
) -> PlaylistLaunchResult:
    documents_root = (documents_dir or (Path.home() / "Documents")).expanduser().resolve()
    state = _load_local_state(state_path)
    remembered_script = _resolve_saved_path(state.get("playlist_script_path"))
    candidates = discover_playlist_script_candidates(documents_root, remembered_script)
    selected_script = _choose_playlist_script(
        remembered_script=remembered_script,
        candidates=candidates,
        input_func=input_func,
        print_func=print_func,
    )
    if selected_script is None:
        return PlaylistLaunchResult(started=False)

    state["playlist_script_path"] = str(selected_script)
    _save_local_state(state_path, state)

    launch_command = build_script_launch_command(selected_script)
    _launch_script_in_terminal(launch_command)

    song_history_path = find_song_history_log(
        documents_root=documents_root,
        script_path=selected_script,
        remembered_path=_resolve_saved_path(state.get("song_history_log_path")),
    )
    if song_history_path is not None:
        state["song_history_log_path"] = str(song_history_path)
        _save_local_state(state_path, state)

    last_state_path = find_last_state_file(
        documents_root=documents_root,
        script_path=selected_script,
        remembered_path=_resolve_saved_path(state.get("last_state_json_path")),
    )
    if last_state_path is not None:
        state["last_state_json_path"] = str(last_state_path)
        _save_local_state(state_path, state)

    last_entry = read_last_song_history_entry(song_history_path) if song_history_path else None
    last_state_entry = read_last_state_entry(last_state_path) if last_state_path else None
    return PlaylistLaunchResult(
        started=True,
        script_path=selected_script,
        song_history_path=song_history_path,
        last_entry=last_entry,
        last_state_path=last_state_path,
        last_state_entry=last_state_entry,
    )


def discover_playlist_script_candidates(
    documents_root: Path,
    remembered_script: Optional[Path] = None,
) -> List[Path]:
    candidates: List[Path] = []
    if remembered_script is not None and remembered_script.is_file():
        candidates.append(remembered_script)

    if not documents_root.exists():
        return candidates

    scored: List[tuple[int, float, str, Path]] = []
    for path in documents_root.rglob("*"):
        if not path.is_file():
            continue

        lower_name = path.name.lower()
        try:
            lower_parts = str(path.relative_to(documents_root)).lower()
        except ValueError:
            lower_parts = lower_name
        if path.suffix.lower() not in SCRIPT_SUFFIXES and not os.access(path, os.X_OK):
            continue

        score = 0
        if any(keyword in lower_name for keyword in PLAYLIST_KEYWORDS):
            score += 4
        if any(keyword in lower_parts for keyword in PLAYLIST_KEYWORDS):
            score += 2
        if path.suffix.lower() == ".command":
            score += 1
        if score == 0:
            continue

        try:
            mtime = path.stat().st_mtime
        except OSError:
            mtime = 0.0
        scored.append((-score, -mtime, str(path), path))

    seen = {candidate.resolve() for candidate in candidates}
    for _, _, _, path in sorted(scored):
        resolved = path.resolve()
        if resolved in seen:
            continue
        candidates.append(path)
        seen.add(resolved)

    return candidates[:10]


def find_song_history_log(
    documents_root: Path,
    script_path: Optional[Path] = None,
    remembered_path: Optional[Path] = None,
) -> Optional[Path]:
    if remembered_path is not None and remembered_path.is_file():
        return remembered_path

    search_roots: List[Path] = []
    if script_path is not None:
        search_roots.append(script_path.parent)
    search_roots.append(documents_root)

    candidates: List[tuple[float, str, Path]] = []
    seen_roots = set()
    for root in search_roots:
        resolved_root = root.resolve()
        if resolved_root in seen_roots or not root.exists():
            continue
        seen_roots.add(resolved_root)

        for path in root.rglob(f"*{SONG_HISTORY_TOKEN}*"):
            if not path.is_file():
                continue
            try:
                mtime = path.stat().st_mtime
            except OSError:
                mtime = 0.0
            candidates.append((-mtime, str(path), path))

    if not candidates:
        return None

    return sorted(candidates)[0][2]


def find_last_state_file(
    documents_root: Path,
    script_path: Optional[Path] = None,
    remembered_path: Optional[Path] = None,
) -> Optional[Path]:
    if remembered_path is not None and remembered_path.is_file():
        return remembered_path

    search_roots: List[Path] = []
    if script_path is not None:
        search_roots.append(script_path.parent)
    search_roots.append(documents_root)

    candidates: List[tuple[float, str, Path]] = []
    seen_roots = set()
    for root in search_roots:
        resolved_root = root.resolve()
        if resolved_root in seen_roots or not root.exists():
            continue
        seen_roots.add(resolved_root)

        for path in root.rglob(LAST_STATE_NAME):
            if not path.is_file():
                continue
            try:
                mtime = path.stat().st_mtime
            except OSError:
                mtime = 0.0
            candidates.append((-mtime, str(path), path))

    if not candidates:
        return None

    return sorted(candidates)[0][2]


def read_last_song_history_entry(path: Optional[Path]) -> Optional[str]:
    if path is None or not path.is_file():
        return None

    try:
        lines = path.read_text(encoding="utf-8", errors="ignore").splitlines()
    except OSError:
        return None

    for raw_line in reversed(lines):
        cleaned = sanitize_song_history_entry(raw_line)
        if cleaned:
            return cleaned
    return None


def read_last_state_entry(path: Optional[Path]) -> Optional[str]:
    if path is None or not path.is_file():
        return None

    try:
        payload = json.loads(path.read_text(encoding="utf-8", errors="ignore"))
    except (OSError, json.JSONDecodeError):
        return None

    if not isinstance(payload, dict):
        return None

    timestamp = _extract_last_state_value(
        payload,
        "timestamp",
        "played_at",
        "last_played",
        "started_at",
        "start_time",
        "time",
        "date",
    )
    artist = _extract_last_state_value(payload, "artist", "artist_name", "creator", "channel")
    title = _extract_last_state_value(payload, "title", "track", "song", "name")
    if timestamp is None or artist is None or title is None:
        return None

    formatted_time = _format_last_state_time(timestamp)
    if formatted_time is None:
        return None

    return f"{formatted_time} => {str(artist).upper()} => {title}"


def sanitize_song_history_entry(raw_line: str) -> str:
    without_url = URL_RE.sub("", raw_line).strip()
    without_url = re.sub(r"\s{2,}", " ", without_url)
    return without_url.strip(" -|\t")


def build_amber_box_lines(text: str, max_width: Optional[int] = None) -> List[str]:
    available_width = max_width or shutil.get_terminal_size((120, 24)).columns
    inner_limit = max(10, available_width - 6)
    visible_text = _truncate_text(text, inner_limit)
    border = "+-" + ("-" * len(visible_text)) + "-+"
    return [border, f"| {visible_text} |", border]


def build_green_status_line(text: str, max_width: Optional[int] = None) -> str:
    available_width = max_width or shutil.get_terminal_size((120, 24)).columns
    return _truncate_text(text, max(10, available_width - 2))


def build_script_launch_command(script_path: Path) -> str:
    quoted_path = shlex.quote(str(script_path))
    if script_path.suffix.lower() == ".py":
        return f"/usr/bin/env python3 {quoted_path}"
    return f"/bin/zsh {quoted_path}"


def _choose_playlist_script(
    remembered_script: Optional[Path],
    candidates: List[Path],
    input_func: Callable[[str], str],
    print_func: Callable[..., None],
) -> Optional[Path]:
    if remembered_script is not None:
        print_func(f"Remembered playlist script: {remembered_script}")
        while True:
            answer = input_func("Start it now? [Y]es / [N]o / [C]hoose path > ").strip().lower()
            if answer in {"", "y", "yes"}:
                return remembered_script
            if answer in {"n", "no"}:
                return None
            if answer in {"c", "choose"}:
                break
            print_func("Enter Y, N, or C.")

    available = candidates[:]
    if not available:
        print_func("No playlist script candidates were found in Documents.")
        return None

    if len(available) == 1:
        only_script = available[0]
        print_func(f"Detected playlist script: {only_script}")
        while True:
            answer = input_func("Start it now? [Y/n] > ").strip().lower()
            if answer in {"", "y", "yes"}:
                return only_script
            if answer in {"n", "no"}:
                return None
            print_func("Enter Y or N.")

    print_func("Detected playlist script candidates:")
    for index, candidate in enumerate(available, start=1):
        print_func(f" {index:>2}. {candidate}")

    while True:
        answer = input_func(f"Select script [1-{len(available)}] or 0 to skip > ").strip()
        if answer == "0":
            return None
        if answer.isdigit():
            selected_index = int(answer)
            if 1 <= selected_index <= len(available):
                return available[selected_index - 1]
        print_func("Enter one of the listed numbers.")


def _launch_script_in_terminal(command: str) -> None:
    applescript = (
        "on run argv\n"
        "    set commandText to item 1 of argv\n"
        "    tell application \"Terminal\"\n"
        "        activate\n"
        "        do script commandText\n"
        "    end tell\n"
        "end run"
    )
    subprocess.run(["osascript", "-e", applescript, command], check=True)


def _load_local_state(state_path: Path) -> Dict[str, str]:
    if not state_path.exists():
        return {}

    try:
        loaded = json.loads(state_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}

    if not isinstance(loaded, dict):
        return {}
    return {str(key): str(value) for key, value in loaded.items()}


def _save_local_state(state_path: Path, state: Dict[str, str]) -> None:
    state_path.parent.mkdir(parents=True, exist_ok=True)
    state_path.write_text(json.dumps(state, indent=2, sort_keys=True), encoding="utf-8")


def _resolve_saved_path(raw_value: Optional[str]) -> Optional[Path]:
    if not raw_value:
        return None

    path = Path(raw_value).expanduser()
    if path.exists():
        return path.resolve()
    return None


def _truncate_text(text: str, limit: int) -> str:
    if len(text) <= limit:
        return text
    if limit <= 3:
        return text[:limit]
    return text[: limit - 3].rstrip() + "..."


def _extract_last_state_value(payload: Dict[str, object], *keys: str) -> Optional[object]:
    for key in keys:
        if key in payload and payload[key] not in {None, ""}:
            return payload[key]
    return None


def _format_last_state_time(raw_value: object) -> Optional[str]:
    if isinstance(raw_value, (int, float)):
        return _format_timestamp_seconds(float(raw_value))

    if not isinstance(raw_value, str):
        return None

    stripped = raw_value.strip()
    if not stripped:
        return None

    if stripped.isdigit():
        return _format_timestamp_seconds(float(stripped))

    try:
        parsed = datetime.fromisoformat(stripped.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed.astimezone().strftime("%H:%M")


def _format_timestamp_seconds(value: float) -> Optional[str]:
    if value > 10_000_000_000:
        value = value / 1000.0

    try:
        return datetime.fromtimestamp(value).strftime("%H:%M")
    except (OverflowError, OSError, ValueError):
        return None