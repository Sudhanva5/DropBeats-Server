#!/usr/bin/env python3
"""Smoke test for the Railway-served endpoints.

Boots main.py with uvicorn on a free port and asserts that the ytmusicapi-backed
endpoints still parse YouTube's current response shape. This is the canary that
caught nothing in August 2026: /watch-playlist had been returning HTTP 500 with
KeyError: 'endpoint' for an unknown length of time, which silently killed
autoplay in the macOS app.

Only ytmusicapi endpoints are covered. yt-dlp is deliberately excluded: it is
blocked from datacenter IPs, so it cannot be exercised from CI, and Railway no
longer serves those endpoints anyway.

Usage: python smoke_test.py
Exit code 0 = healthy, 1 = broken.
"""

import json
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

# A stable, long-lived track used as the probe. Michael Jackson, Smooth Criminal.
PROBE_VIDEO_ID = "XzNWRmqibNE"
PROBE_QUERY = "coldplay"
BOOT_TIMEOUT_SECONDS = 60
REQUEST_TIMEOUT_SECONDS = 60


def find_free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def get_json(url: str) -> dict:
    with urllib.request.urlopen(url, timeout=REQUEST_TIMEOUT_SECONDS) as response:
        return json.loads(response.read())


def wait_for_health(base: str, proc: subprocess.Popen) -> None:
    deadline = time.time() + BOOT_TIMEOUT_SECONDS
    while time.time() < deadline:
        if proc.poll() is not None:
            raise RuntimeError(f"server exited during boot with code {proc.returncode}")
        try:
            if get_json(f"{base}/health").get("status") == "healthy":
                return
        except (urllib.error.URLError, ConnectionError, json.JSONDecodeError, TimeoutError):
            pass
        time.sleep(1)
    raise RuntimeError(f"server did not become healthy within {BOOT_TIMEOUT_SECONDS}s")


def check_search(base: str) -> None:
    payload = get_json(f"{base}/search/{PROBE_QUERY}?limit=5")
    songs = payload.get("categories", {}).get("songs", [])
    if not songs:
        raise AssertionError(f"/search returned no songs: {json.dumps(payload)[:400]}")
    missing = [s for s in songs if not s.get("id") or not s.get("title")]
    if missing:
        raise AssertionError(f"/search returned {len(missing)} songs without id/title")
    print(f"  /search               OK  ({len(songs)} songs)")


def check_watch_playlist(base: str) -> None:
    payload = get_json(f"{base}/watch-playlist/{PROBE_VIDEO_ID}?limit=25")
    tracks = payload.get("tracks", [])
    if not tracks:
        raise AssertionError(f"/watch-playlist returned no tracks: {json.dumps(payload)[:400]}")

    # Guards against the silent-parse-failure class of bug: the endpoint answers
    # 200 but every field the app needs comes back empty.
    no_duration = [t for t in tracks if not t.get("duration")]
    no_art = [t for t in tracks if not t.get("albumArt")]
    if len(no_duration) > len(tracks) // 2:
        raise AssertionError(
            f"/watch-playlist: {len(no_duration)}/{len(tracks)} tracks have no duration "
            "- ytmusicapi likely renamed the length field"
        )
    if len(no_art) > len(tracks) // 2:
        raise AssertionError(
            f"/watch-playlist: {len(no_art)}/{len(tracks)} tracks have no albumArt "
            "- ytmusicapi likely renamed the thumbnail field"
        )
    print(
        f"  /watch-playlist       OK  ({len(tracks)} tracks, "
        f"{len(tracks) - len(no_duration)} with duration, {len(tracks) - len(no_art)} with art)"
    )


def main() -> int:
    import ytmusicapi

    print(f"ytmusicapi {ytmusicapi.__version__}")

    port = find_free_port()
    base = f"http://127.0.0.1:{port}"
    api_dir = Path(__file__).resolve().parent

    proc = subprocess.Popen(
        [sys.executable, "-m", "uvicorn", "main:app", "--host", "127.0.0.1", "--port", str(port)],
        cwd=api_dir,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
    )
    try:
        wait_for_health(base, proc)
        print("  /health               OK")
        check_search(base)
        check_watch_playlist(base)
    except Exception as exc:
        print(f"\nSMOKE TEST FAILED: {type(exc).__name__}: {exc}", file=sys.stderr)
        proc.terminate()
        try:
            output = proc.communicate(timeout=10)[0]
        except subprocess.TimeoutExpired:
            proc.kill()
            output = proc.communicate()[0]
        if output:
            print("\n--- server output (last 40 lines) ---", file=sys.stderr)
            print("\n".join(output.splitlines()[-40:]), file=sys.stderr)
        return 1
    finally:
        if proc.poll() is None:
            proc.terminate()
            try:
                proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                proc.kill()

    print("\nSMOKE TEST PASSED")
    return 0


if __name__ == "__main__":
    sys.exit(main())
