#!/usr/bin/env python3

import argparse
import csv
import getpass
import hashlib
import json
import os
from pathlib import Path
import re
import signal
import shlex
import shutil
import subprocess
import time
from dataclasses import dataclass
from typing import List, Optional


DEFAULT_BASE_URL = "https://there-is-a.vrpmonkey.help/"
DEFAULT_PASSWORD = "gL59VfgPxoHR"
HASH_CACHE_PATH = Path("./VRP-GameList-hashes.json")
HASH_CACHE_MAX_AGE_SECONDS = 24 * 60 * 60
FAILED_STATE_PATH = Path("./failed-games.json")
FAILED_LOG_PATH = Path("./failed-games.log")
ACTIVE_PROCS: set[subprocess.Popen[str]] = set()
IN_PROGRESS_PATHS: set[Path] = set()


@dataclass
class GameRecord:
    display_name: str
    release_name: str
    game_name_hash: str
    raw: List[str]


class VRPGameListHasher:
    """
    Parses VRP-GameList.txt and computes hashes the same way Rookie does:
    MD5(UTF8(release_name + "\\n")) -> lowercase hex
    """

    def __init__(self, file_path: str | Path):
        self.file_path = Path(file_path)
        self.header: List[str] = []
        self.records: List[GameRecord] = []
        self._load()

    @staticmethod
    def compute_game_name_hash(release_name: str) -> str:
        return hashlib.md5((release_name + "\n").encode("utf-8")).hexdigest()

    def _load(self) -> None:
        with self.file_path.open("r", encoding="utf-8", errors="replace", newline="") as f:
            reader = csv.reader(f, delimiter=";")
            rows = [row for row in reader if row and any(cell.strip() for cell in row)]

        if not rows:
            return

        self.header = rows[0]

        game_name_idx = 0
        release_name_idx = 1

        for row in rows[1:]:
            if len(row) <= release_name_idx:
                continue

            display_name = row[game_name_idx].strip() if len(row) > game_name_idx else ""
            release_name = row[release_name_idx].strip()
            if not release_name:
                continue

            self.records.append(
                GameRecord(
                    display_name=display_name,
                    release_name=release_name,
                    game_name_hash=self.compute_game_name_hash(release_name),
                    raw=row,
                )
            )

    def find_by_release_name(self, release_name: str) -> Optional[GameRecord]:
        target = release_name.strip().lower()
        for r in self.records:
            if r.release_name.lower() == target:
                return r
        return None


def mark_in_progress(path: Path) -> None:
    IN_PROGRESS_PATHS.add(path)


def unmark_in_progress(path: Path) -> None:
    IN_PROGRESS_PATHS.discard(path)


def remove_path(path: Path) -> None:
    if path.is_dir():
        shutil.rmtree(path, ignore_errors=True)
    elif path.exists():
        path.unlink(missing_ok=True)


def cleanup_in_progress_paths() -> None:
    for path in sorted(IN_PROGRESS_PATHS, key=lambda p: len(str(p)), reverse=True):
        remove_path(path)
    IN_PROGRESS_PATHS.clear()


def terminate_active_processes() -> None:
    for proc in list(ACTIVE_PROCS):
        if proc.poll() is None:
            try:
                proc.terminate()
            except Exception:
                pass
    for proc in list(ACTIVE_PROCS):
        if proc.poll() is None:
            try:
                proc.wait(timeout=3)
            except subprocess.TimeoutExpired:
                try:
                    proc.kill()
                except Exception:
                    pass


def handle_termination_signal(signum: int, _frame: object) -> None:
    signame = signal.Signals(signum).name
    print(f"\nReceived {signame}, stopping...")
    terminate_active_processes()
    raise KeyboardInterrupt


def file_is_fresh(path: Path, max_age_seconds: int) -> bool:
    if not path.is_file():
        return False
    age = time.time() - path.stat().st_mtime
    return age < max_age_seconds


def utc_now_iso() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def utc_now_date_time() -> tuple[str, str]:
    now = time.gmtime()
    return time.strftime("%Y-%m-%d", now), time.strftime("%H:%M:%S", now)


def load_failed_state(path: Path) -> dict[str, dict[str, str | int]]:
    if not path.is_file():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}
    if not isinstance(data, dict):
        return {}
    return data


def save_failed_state(path: Path, state: dict[str, dict[str, str | int]]) -> None:
    path.write_text(json.dumps(state, indent=2, sort_keys=True), encoding="utf-8")


def append_failure_log(
    path: Path, release_name: str, game_hash: str, reason: str, details: str = ""
) -> None:
    date_str, time_str = utc_now_date_time()
    entry = {
        "date": date_str,
        "time": time_str,
        "timestamp": utc_now_iso(),
        "release_name": release_name,
        "hash": game_hash,
        "reason": reason,
        "details": details.strip(),
    }
    with path.open("a", encoding="utf-8") as f:
        f.write(json.dumps(entry, ensure_ascii=False) + "\n")


def mark_failed(
    failed_state: dict[str, dict[str, str | int]],
    release_name: str,
    game_hash: str,
    reason: str,
    details: str = "",
) -> None:
    prev = failed_state.get(game_hash, {})
    fail_count = int(prev.get("fail_count", 0)) + 1
    failed_state[game_hash] = {
        "release_name": release_name,
        "last_reason": reason,
        "last_details": details.strip(),
        "last_failed_at": utc_now_iso(),
        "fail_count": fail_count,
    }
    append_failure_log(FAILED_LOG_PATH, release_name, game_hash, reason, details)


def clear_failed(
    failed_state: dict[str, dict[str, str | int]], game_hash: str
) -> None:
    failed_state.pop(game_hash, None)


def was_failed_before(
    failed_state: dict[str, dict[str, str | int]], game_hash: str | None
) -> bool:
    return bool(game_hash and game_hash in failed_state)


def reached_failure_limit(
    failed_state: dict[str, dict[str, str | int]],
    game_hash: str | None,
    max_failures: int,
) -> bool:
    if not game_hash:
        return False
    entry = failed_state.get(game_hash)
    if not isinstance(entry, dict):
        return False
    return int(entry.get("fail_count", 0)) >= max_failures


def prune_unknown_download_dirs(dest_root: Path, valid_dir_names: set[str]) -> int:
    unknown_dirs = [
        entry
        for entry in dest_root.iterdir()
        if entry.is_dir() and entry.name not in valid_dir_names
    ]

    if len(unknown_dirs) > 25:
        print(
            f"Refusing cleanup: found {len(unknown_dirs)} unknown folders "
            "(limit is 25). Deleted 0 folders; exiting."
        )
        raise SystemExit(2)

    removed = 0
    for entry in unknown_dirs:
        print(f"Removing unknown folder: {entry}")
        remove_path(entry)
        removed += 1
    return removed


def prune_empty_dirs(root: Path) -> int:
    removed = 0
    for current_root, dirnames, _filenames in os.walk(root, topdown=False):
        current_path = Path(current_root)
        for dirname in dirnames:
            path = current_path / dirname
            try:
                path.rmdir()
            except OSError:
                continue
            print(f"Removed empty folder: {path}")
            removed += 1
    return removed


def safe_folder_name(name: str) -> str:
    name = (name or "").strip()
    name = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", name)
    name = re.sub(r"\s+", " ", name).strip(". ")
    return name or "unknown_game"


def is_mr_fix_name(name: str) -> bool:
    return "mr-fix" in safe_folder_name(name).lower()


def unique_path(path: Path) -> Path:
    if not path.exists():
        return path
    i = 2
    while True:
        p = path.parent / f"{path.name} ({i})"
        if not p.exists():
            return p
        i += 1


def run_cmd(cmd: list[str]) -> tuple[int, str, str]:
    print(f"CMD: {shlex.join(cmd)}")
    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    ACTIVE_PROCS.add(proc)
    try:
        out, err = proc.communicate()
    finally:
        ACTIVE_PROCS.discard(proc)
    return proc.returncode, out, err


def run_checked(cmd: list[str]) -> None:
    print(f"CMD: {shlex.join(cmd)}")
    proc = subprocess.Popen(cmd)
    ACTIVE_PROCS.add(proc)
    try:
        rc = proc.wait()
    finally:
        ACTIVE_PROCS.discard(proc)
    if rc != 0:
        raise subprocess.CalledProcessError(rc, cmd)


def find_first_001_file(folder: Path) -> Path | None:
    matches = sorted(p for p in folder.rglob("*") if p.is_file() and p.name.endswith("001"))
    return matches[0] if matches else None


def clear_dir(folder: Path) -> None:
    for p in folder.iterdir():
        if p.is_dir():
            shutil.rmtree(p, ignore_errors=True)
        else:
            p.unlink(missing_ok=True)


def copy_contents(src: Path, dst: Path) -> None:
    for p in src.iterdir():
        out = dst / p.name
        if p.is_dir():
            shutil.copytree(p, out, dirs_exist_ok=True)
        else:
            shutil.copy2(p, out)


def move_meta_to_downloads(dest_root: Path) -> None:
    meta_dir = Path("./meta")
    if not meta_dir.is_dir():
        return

    source_meta_dir = meta_dir / ".meta"
    dest_root.mkdir(parents=True, exist_ok=True)
    dest_meta_dir = dest_root / ".meta"
    if source_meta_dir.is_dir():
        remove_path(dest_meta_dir)
        shutil.move(str(source_meta_dir), str(dest_meta_dir))
        print(f"Moved {source_meta_dir} to {dest_meta_dir}")
    else:
        print(f"No {source_meta_dir} folder found; skipping move.")

    remove_path(meta_dir)
    print(f"Removed remaining meta files from {meta_dir}")


def extract_and_replace(game_dir: Path, password: str, sevenzip: str) -> None:
    first_001 = find_first_001_file(game_dir)
    if not first_001:
        print(f"  No *001 archive found in {game_dir.name}; leaving as-is.")
        return

    tmp_extract = unique_path(game_dir.parent / f"{game_dir.name}__extract_tmp")
    tmp_extract.mkdir(parents=True, exist_ok=True)
    mark_in_progress(tmp_extract)

    try:
        cmd = [sevenzip, "x", str(first_001), f"-o{tmp_extract}", f"-p{password}", "-y"]
        rc, out, err = run_cmd(cmd)
        if rc != 0:
            raise RuntimeError(err.strip() or out.strip() or f"7z failed with exit code {rc}")

        items = list(tmp_extract.iterdir())
        src_root = items[0] if len(items) == 1 and items[0].is_dir() else tmp_extract

        clear_dir(game_dir)
        copy_contents(src_root, game_dir)
    finally:
        remove_path(tmp_extract)
        unmark_in_progress(tmp_extract)


def main() -> None:
    parser = argparse.ArgumentParser(
        description=(
            "Download/extract meta.7z, build VRP hash JSON, and download/extract first N games."
        )
    )
    parser.add_argument("--base-url", help="Base URL for HTTP remote")
    parser.add_argument("--password", help="Password for HTTP auth")
    parser.add_argument(
        "password_arg", nargs="?", help="Password for HTTP auth (legacy positional)"
    )
    parser.add_argument(
        "--input",
        default="./meta/VRP-GameList.txt",
        help="Path to VRP-GameList.txt",
    )
    parser.add_argument(
        "--output",
        default="./meta/VRP-GameList-hashes.json",
        help="Path to output JSON file",
    )
    parser.add_argument(
        "--base-dir",
        default=".",
        help="Base working directory for all relative paths",
    )
    parser.add_argument("--dest", default="./downloads", help="Destination root for game downloads")
    parser.add_argument("--rclone", default="rclone", help="Path to rclone binary")
    parser.add_argument(
        "--sevenzip",
        default=None,
        help="7z executable to use (defaults to auto-detect 7z, then 7zz)",
    )
    parser.add_argument("--limit", type=int, default=2, help="Number of games to process")
    parser.add_argument(
        "--max-failures",
        type=int,
        default=3,
        help="Stop retrying a game after this many recorded failures",
    )
    parser.add_argument(
        "--clear-failures",
        action="store_true",
        help="Clear all recorded failure counts before processing downloads",
    )
    parser.add_argument("--dry-run", action="store_true")

    args = parser.parse_args()

    if args.max_failures < 1:
        raise RuntimeError("--max-failures must be at least 1")

    base_dir = Path(args.base_dir).expanduser().resolve()
    base_dir.mkdir(parents=True, exist_ok=True)
    os.chdir(base_dir)
    print(f"Working directory: {base_dir}")

    signal.signal(signal.SIGINT, handle_termination_signal)
    signal.signal(signal.SIGTERM, handle_termination_signal)


    rclone_cmd = shutil.which(args.rclone)
    if not rclone_cmd:
        raise RuntimeError(
            f"rclone executable not found: {args.rclone}. Install rclone or pass --rclone with a valid path."
        )

    base_url = args.base_url or os.getenv("BASE_URL") or DEFAULT_BASE_URL
    if not base_url:
        base_url = input("Enter BASE_URL: ").strip()
    if not base_url:
        raise RuntimeError("BASE_URL is required")
    base_url = base_url.rstrip("/")

    password = args.password or args.password_arg or os.getenv("PASSWORD") or DEFAULT_PASSWORD
    if not password:
        password = getpass.getpass("Enter password: ").strip()
    if not password:
        raise RuntimeError("Password is required")

    seven_zip_cmd = args.sevenzip or ("7z" if shutil.which("7z") else "7zz" if shutil.which("7zz") else None)
    if not seven_zip_cmd:
        raise RuntimeError("Neither 7z nor 7zz is installed")

    use_hash_cache = file_is_fresh(HASH_CACHE_PATH, HASH_CACHE_MAX_AGE_SECONDS)
    if use_hash_cache:
        age_hours = (time.time() - HASH_CACHE_PATH.stat().st_mtime) / 3600.0
        print(
            f"Using cached {HASH_CACHE_PATH} ({age_hours:.1f}h old); "
            "skipping meta download and hash regeneration."
        )
    else:
        meta_archive = Path("./meta.7z")
        remove_path(meta_archive)
        mark_in_progress(meta_archive)
        try:
            run_checked(
                [
                    rclone_cmd,
                    "copy",
                    ":http:/meta.7z",
                    "./",
                    "--http-url",
                    base_url,
                    "--http-no-head",
                    "--progress",
                ]
            )

            run_checked(
                [
                    seven_zip_cmd,
                    "x",
                    "./meta.7z",
                    "-aoa",
                    f"-p{password}",
                    "-o./meta",
                ]
            )
        finally:
            remove_path(meta_archive)
            unmark_in_progress(meta_archive)

        gl = VRPGameListHasher(args.input)

        data = {
            r.release_name: {
                "display_name": r.display_name,
                "game_name_hash": r.game_name_hash,
            }
            for r in gl.records
        }

        out = Path(args.output)
        out.parent.mkdir(parents=True, exist_ok=True)
        rendered = json.dumps(data, indent=2, ensure_ascii=False, sort_keys=True)
        out.write_text(rendered, encoding="utf-8")

        if HASH_CACHE_PATH.resolve() != out.resolve():
            HASH_CACHE_PATH.write_text(rendered, encoding="utf-8")
            print(f"Wrote {out} ({len(data)} entries)")
            print(f"Updated cache {HASH_CACHE_PATH} ({len(data)} entries)")
        else:
            print(f"Wrote {out} ({len(data)} entries)")

        move_meta_to_downloads(Path(args.dest))

    hashes = json.loads(HASH_CACHE_PATH.read_text(encoding="utf-8"))
    items = list(hashes.items())
    failed_state = load_failed_state(FAILED_STATE_PATH)
    if args.clear_failures:
        failed_state = {}
        save_failed_state(FAILED_STATE_PATH, failed_state)
        print(f"Cleared all recorded failures in {FAILED_STATE_PATH}.")
    known_hashes = {
        str(info.get("game_name_hash"))
        for _, info in items
        if isinstance(info, dict) and info.get("game_name_hash")
    }
    for old_hash in list(failed_state.keys()):
        if old_hash not in known_hashes:
            failed_state.pop(old_hash, None)

    items.sort(
        key=lambda item: (
            was_failed_before(failed_state, item[1].get("game_name_hash")),
            not is_mr_fix_name(item[0]),
        )
    )
    dest_root = Path(args.dest)
    dest_root.mkdir(parents=True, exist_ok=True)
    valid_dir_names = {safe_folder_name(full_game_name) for full_game_name, _ in items}
    valid_dir_names.add(".meta")
    valid_dir_names.add("bcl_app")
    removed_count = prune_unknown_download_dirs(dest_root, valid_dir_names)
    if removed_count:
        print(f"Removed {removed_count} folder(s) not present in {HASH_CACHE_PATH}.")
    empty_removed_count = prune_empty_dirs(dest_root)
    if empty_removed_count:
        print(f"Removed {empty_removed_count} empty folder(s) from {dest_root}.")

    ok = 0
    failed = 0
    skipped = 0
    skipped_max_failures = 0
    to_process = 0

    for i, (full_game_name, info) in enumerate(items, 1):
        if to_process >= args.limit:
            break

        game_hash = info.get("game_name_hash")
        if not game_hash:
            print(f"[{i}] Missing hash, skipping: {full_game_name}")
            append_failure_log(
                FAILED_LOG_PATH,
                full_game_name,
                "missing",
                "missing_hash",
                "Entry missing game_name_hash in hashes JSON",
            )
            failed += 1
            continue

        target_dir = dest_root / safe_folder_name(full_game_name)

        if target_dir.exists():
            print(f"[{i}/{len(items)}] Exists, skipping download: {target_dir}")
            clear_failed(failed_state, str(game_hash))
            skipped += 1
            continue

        if reached_failure_limit(failed_state, str(game_hash), args.max_failures):
            fail_count = int(failed_state[str(game_hash)].get("fail_count", 0))
            print(
                f"[{i}/{len(items)}] Reached failure limit "
                f"({fail_count}/{args.max_failures}), skipping forever: {full_game_name}"
            )
            skipped += 1
            skipped_max_failures += 1
            continue

        to_process += 1
        target_dir.mkdir(parents=True, exist_ok=True)
        mark_in_progress(target_dir)
        target_complete = False

        try:
            cmd = [
                rclone_cmd,
                "copy",
                f":http:/{game_hash}/",
                str(target_dir),
                "--http-url",
                base_url,
                "--ignore-existing",
                "--progress",
            ]
            if args.dry_run:
                cmd.append("--dry-run")


            print(f"\n[{i}/{len(items)}] {full_game_name}")
            print(f"  Hash: {game_hash}")
            print(f"  CMD: {shlex.join(cmd)}")


            rc, out_text, err_text = run_cmd(cmd)
            if rc != 0:
                print(f"  rclone failed ({rc})")
                if err_text.strip():
                    print(f"  {err_text.strip()}")
                mark_failed(
                    failed_state,
                    full_game_name,
                    str(game_hash),
                    "rclone_download_failed",
                    err_text or out_text,
                )
                print(f"  Cleaning failed folder: {target_dir}")
                remove_path(target_dir)
                failed += 1
                target_complete = True
                continue

            if args.dry_run:
                ok += 1
                clear_failed(failed_state, str(game_hash))
                target_complete = True
                continue

            try:
                extract_and_replace(target_dir, password, seven_zip_cmd)
                ok += 1
                clear_failed(failed_state, str(game_hash))
            except Exception as ex:
                print(f"  Post-process failed: {ex}")
                mark_failed(
                    failed_state,
                    full_game_name,
                    str(game_hash),
                    "post_process_failed",
                    str(ex),
                )
                print(f"  Cleaning failed folder: {target_dir}")
                remove_path(target_dir)
                failed += 1
            target_complete = True
        finally:
            if target_complete:
                unmark_in_progress(target_dir)

    save_failed_state(FAILED_STATE_PATH, failed_state)
    print(
        f"\nDone. Success={ok}, Failed={failed}, Skipped={skipped}, "
        f"SkippedMaxFailures={skipped_max_failures}"
    )


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\nInterrupted. Cleaning up in-progress work...")
        terminate_active_processes()
        cleanup_in_progress_paths()
        raise SystemExit(130)
