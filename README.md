# rookie-downloader

## Status

This project no longer works as intended because the Rookie project is dead and the upstream service it depends on is no longer active.

This repository is kept for educational and reference purposes only. Do not expect the downloader to function against the original source.

`grab.py` is a single-file Python CLI that:

1. Downloads `meta.7z` from an HTTP-backed `rclone` remote.
2. Extracts `VRP-GameList.txt`.
3. Builds a JSON map of release names to Rookie-style MD5 hashes.
4. Downloads the first `N` game payloads by hash.
5. Extracts the first `*.001` archive found in each downloaded game folder.
6. Tracks failures and skips entries that exceed a retry limit.

This repository currently contains one executable script: [grab.py](grab.py).

## Requirements

- Python 3.10 or newer
- `rclone` installed and available on `PATH`, or passed via `--rclone`
- `7z` or `7zz` installed and available on `PATH`, or passed via `--sevenzip`

## What the Script Does

At a high level, each run works like this:

1. Switch into `--base-dir` and do all file I/O there.
2. Use `./VRP-GameList-hashes.json` as a 24-hour cache.
3. If the cache is stale or missing:
   - download `meta.7z`
   - extract it into `./meta`
   - parse `./meta/VRP-GameList.txt`
   - compute `MD5(release_name + "\n")` for each record
   - write the JSON output
   - move `./meta/.meta` into the download destination as `DEST/.meta`
4. Load the hash cache and sort the entries:
   - entries without prior failures first
   - names containing `mr-fix` first within the same failure group
5. Clean the destination directory:
   - remove folders not present in the current game list
   - remove empty folders
6. Download up to `--limit` missing games.
7. Extract the first `*.001` archive found in each game folder, replacing the downloaded archive contents with the extracted files.
8. Record failures in JSON and append structured failure logs.

## Important Safety Notes

- Use a dedicated download directory. The script removes directories inside `--dest` that are not in the current hash list.
- Cleanup has a hard stop if more than 25 unknown folders are found. In that case it exits without deleting anything.
- Existing destination folders are treated as already-downloaded games and are skipped.
- `--dry-run` is not side-effect free. The script still creates target directories before invoking `rclone --dry-run`.
- The archive password is also used for extraction. If you rely on a different authentication model for HTTP access, provide that through the URL or your `rclone` setup; the script does not separately pass HTTP credentials to `rclone`.

## Installation

Clone the repository and make sure the external tools are installed:

```bash
git clone <your-fork-or-repo-url>
cd rookie-downloader
python3 --version
rclone version
7z || 7zz
```

No Python package installation is required. The script uses only the standard library.

## Usage

Basic invocation:

```bash
python3 grab.py --base-dir ./work --dest ./work/downloads --limit 2
```

Common pattern with explicit configuration:

```bash
python3 grab.py \
  --base-url "https://example.invalid/path" \
  --password "<archive-password>" \
  --base-dir ./work \
  --dest ./work/downloads \
  --limit 5
```

Metadata refresh only:

```bash
python3 grab.py --base-dir ./work --dest ./work/downloads --limit 0
```

Preview download commands without extracting payloads:

```bash
python3 grab.py --base-dir ./work --dest ./work/downloads --limit 2 --dry-run
```

Clear retry history and try failed items again:

```bash
python3 grab.py --base-dir ./work --dest ./work/downloads --clear-failures
```

## CLI Reference

```text
usage: grab.py [-h] [--base-url BASE_URL] [--password PASSWORD]
               [--input INPUT] [--output OUTPUT] [--base-dir BASE_DIR]
               [--dest DEST] [--rclone RCLONE] [--sevenzip SEVENZIP]
               [--limit LIMIT] [--max-failures MAX_FAILURES]
               [--clear-failures] [--dry-run]
               [password_arg]
```

### Options

| Option | Default | Behavior |
| --- | --- | --- |
| `--base-url` | source default or `BASE_URL` env var | Base URL passed to `rclone --http-url`. Trailing `/` is removed. |
| `--password` | source default or `PASSWORD` env var | Password used when extracting `meta.7z` and game archives. |
| `password_arg` | none | Legacy positional password argument. Used if `--password` is not provided. |
| `--input` | `./meta/VRP-GameList.txt` | Input file parsed after extracting `meta.7z`. Relative to `--base-dir`. |
| `--output` | `./meta/VRP-GameList-hashes.json` | Secondary output location for the generated hash JSON. |
| `--base-dir` | `.` | Working directory for all relative paths and state files. The script `chdir`s here. |
| `--dest` | `./downloads` | Root directory for downloaded games. Relative to `--base-dir` unless absolute. |
| `--rclone` | `rclone` | `rclone` executable path. |
| `--sevenzip` | auto-detect `7z`, then `7zz` | Archive extractor executable. |
| `--limit` | `2` | Maximum number of new games to process in this run. Existing folders do not count toward the limit. |
| `--max-failures` | `3` | Number of recorded failures after which a game is skipped on future runs. |
| `--clear-failures` | off | Resets `failed-games.json` before processing. |
| `--dry-run` | off | Passes `--dry-run` to the game download `rclone copy` step. Metadata download still happens when the cache is stale. |

## Environment Variables

The script reads:

- `BASE_URL`
- `PASSWORD`

CLI arguments win over environment variables.

## Files Created at Runtime

All of these paths are relative to `--base-dir` unless you pass absolute paths.

| Path | Purpose |
| --- | --- |
| `./VRP-GameList-hashes.json` | Canonical 24-hour cache used for future runs. |
| `./meta/VRP-GameList-hashes.json` | Default secondary output copy of the generated hashes. |
| `./failed-games.json` | Failure state keyed by game hash, including fail counts and timestamps. |
| `./failed-games.log` | Append-only JSON-lines failure log. |
| `./meta.7z` | Temporary metadata archive, removed after extraction. |
| `./meta/` | Temporary extracted metadata directory, later removed. |
| `DEST/.meta` | `.meta` folder moved from `./meta/.meta` after metadata extraction. |
| `DEST/<sanitized-release-name>/` | Per-game download and extraction directory. |

### Cache Behavior

`./VRP-GameList-hashes.json` is the file that controls whether metadata is refreshed. If it is less than 24 hours old, the script skips downloading `meta.7z` and skips regenerating hashes.

Even if you set `--output` to some other file, the downloader still reads from `./VRP-GameList-hashes.json` for actual processing. In practice, `--output` is a generated copy, while the cache file in the base directory is the authoritative input for downloads.

## Input Format

`VRP-GameList.txt` is parsed as semicolon-delimited CSV.

- Column `0`: display name
- Column `1`: release name

Only rows with a non-empty release name are used.

For each release name, the script computes:

```text
MD5(UTF-8(release_name + "\n"))
```

The generated JSON looks like this:

```json
{
  "Release.Name.Example": {
    "display_name": "User Visible Name",
    "game_name_hash": "0123456789abcdef0123456789abcdef"
  }
}
```

## Download and Extraction Details

Game downloads use `rclone copy` with an HTTP backend path in this form:

```text
:http:/<game_hash>/
```

Metadata download uses:

```text
:http:/meta.7z
```

After a game is downloaded, the script looks for the first file ending in `001` anywhere under that game directory. If found, it extracts that archive into a temporary directory and then replaces the original downloaded contents with the extracted contents.

If no `*.001` file is found, the folder is left as downloaded.

## Failure Handling

Failures are tracked by game hash in `failed-games.json`. Each entry stores:

- `release_name`

- `last_reason`
- `last_details`
- `last_failed_at`
- `fail_count`

Failure reasons currently used by the script:

- `missing_hash`
- `rclone_download_failed`
- `post_process_failed`

Once `fail_count >= --max-failures`, that game is skipped on later runs until you clear failure state.

## Folder Naming Rules

Destination folders are derived from the release name, not the display name.

The script sanitizes names by:

- replacing invalid Windows-style path characters with `_`
- collapsing repeated whitespace
- trimming trailing dots and spaces
- falling back to `unknown_game` if the result is empty

## Interrupt and Cleanup Behavior

On `SIGINT` or `SIGTERM`, the script:

1. terminates child processes
2. raises `KeyboardInterrupt`
3. removes in-progress paths on exit
4. exits with status `130`

Temporary extraction folders and failed download targets are removed automatically when cleanup paths are still marked as in progress.

## Known Operational Caveats

- The script assumes `rclone` can access the target HTTP resources using the provided `--http-url`.
- The help text for `--password` says "HTTP auth", but the current implementation uses that value for archive extraction.
- A dry run can leave empty game directories behind, and later runs will treat those directories as completed downloads.
- The downloader only processes games from the hash cache file in the base directory, even if you customize `--output`.

## Verification

Current local verification for this repository:

```bash
python3 -m py_compile grab.py
python3 grab.py --help
```

## License

This project is licensed under the terms in [LICENSE](LICENSE).
