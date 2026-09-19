#!/usr/bin/env python3
"""Step 2 - link every track to its Jellyfin item id.

The dataset is built from the files; the *player* streams from Jellyfin. This
step bridges the two: it asks Jellyfin for every audio item, converts the
server-side path into a path relative to ``music_root`` and stores the item id,
the cover tags and the duration on the matching track row.

Read-only towards Jellyfin (GET only) and towards the files.

    python3 step2_link.py                 # one pass (a few minutes on a Pi)
    python3 step2_link.py --page-size 2000
    python3 step2_link.py --dry-run       # report the matches, write nothing

The server path and the local path usually differ (the mount point here is not
the mount point on the server), so ``jellyfin.path_prefix`` in config.json is
stripped from the server path before it is matched against ``music_root``.
"""

from __future__ import annotations

import argparse
import json
import sys
import time
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple

sys.path.insert(0, str(Path(__file__).resolve().parent))

import styles_db as S  # noqa: E402

FIELDS = "Path,RunTimeTicks,ImageTags,AlbumPrimaryImageTag,AlbumId,Container,MediaSources"


class Client:
    """The smallest possible Jellyfin reader (GET only, no writes, no login)."""

    def __init__(self, config: Dict[str, Any], debug: bool = False) -> None:
        player = S.resolve(S.setting(config, "jellyfin_config",
                                     "~/.config/simplejellymus/config.json"))
        stored: Dict[str, Any] = {}
        if player.exists():
            try:
                stored = json.loads(player.read_text(encoding="utf-8"))
            except ValueError:
                stored = {}
        server = S.setting(config, "jellyfin.server_url", "") or stored.get("server_url", "")
        token = S.setting(config, "jellyfin.access_token", "") or stored.get("access_token", "")
        user = S.setting(config, "jellyfin.user_id", "") or stored.get("user_id", "")
        if not server or not token or not user:
            raise SystemExit(
                f"no Jellyfin credentials: expected them in {player} or in config.json")
        self.base = str(server).rstrip("/")
        self.token = str(token)
        self.user = str(user)
        self.timeout = float(S.setting(config, "jellyfin.request_timeout", 120) or 120)
        self.debug = debug
        self.calls = 0

    def page(self, start: int, limit: int) -> Dict[str, Any]:
        params = {
            "Recursive": "true",
            "IncludeItemTypes": "Audio",
            "MediaTypes": "Audio",
            "Fields": FIELDS,
            "StartIndex": str(int(start)),
            "Limit": str(int(limit)),
            "EnableTotalRecordCount": "true",
        }
        url = f"{self.base}/Users/{self.user}/Items?" + urllib.parse.urlencode(params)
        request = urllib.request.Request(url, headers={
            "X-Emby-Authorization": (
                'MediaBrowser Client="SimpleJellyMus", Device="styles-builder",'
                f' DeviceId="styles-builder", Version="0.1", Token="{self.token}"'),
            "Accept": "application/json",
        })
        for attempt in range(3):
            try:
                with urllib.request.urlopen(request, timeout=self.timeout) as response:
                    self.calls += 1
                    return json.loads(response.read().decode("utf-8", "replace"))
            except Exception as exc:
                if attempt == 2:
                    raise SystemExit(f"Jellyfin request failed: {exc}")
                time.sleep(2.0 * (attempt + 1))
        return {}


def relative_path(server_path: str, prefix: str) -> str:
    """Server path -> path relative to the music root (best effort)."""
    text = urllib.parse.unquote(str(server_path or "")).replace("\\", "/")
    if prefix:
        cleaned = prefix.rstrip("/")
        if text.startswith(cleaned):
            text = text[len(cleaned):]
    return text.lstrip("/")


def path_parts(server_path: str) -> List[str]:
    """The components of a server path, URL-decoded."""
    text = urllib.parse.unquote(str(server_path or "")).replace("\\", "/")
    return [part for part in text.split("/") if part]


def strip_components(server_path: str, depth: int) -> str:
    """Drop the first *depth* components of a server path."""
    return "/".join(path_parts(server_path)[depth:])


def report_prefix_mismatch(items: Sequence[Dict[str, Any]], known: set, prefix: str) -> None:
    """Explain - instead of silently matching nothing - that the paths disagree."""
    S.log("PROBLEM: not one Jellyfin path matched a track in the dataset.")
    S.log(f"         jellyfin.path_prefix is {prefix!r}.")
    S.log("         Jellyfin usually runs in a container, so the path it reports is")
    S.log("         the path *inside* the container, not the one you mounted.")
    print("      server paths reported by Jellyfin:")
    for item in items[:3]:
        print(f"        {(item.get('Path') or '')!r}")
    print("      tracks in the dataset:")
    for path in sorted(known)[:3]:
        print(f"        {path!r}")
    S.log("fix: set jellyfin.path_prefix in config.json to the part that must be")
    S.log("     stripped (e.g. '/musicMedia'), or run with --prefix /musicMedia")


def detect_prefix(items: Sequence[Dict[str, Any]], known: set, configured: str,
                  sample: int = 300) -> Tuple[str, int, int]:
    """Find which part of the server path is the library root.

    Jellyfin may live in a container, so the path it reports (``/musicMedia/...``)
    is usually *not* the path you mounted (``/mnt/PenA/Mus/...``). The prefix is
    detected by trying the configured value and then stripping one to four
    leading components, and keeping whatever matches the most known tracks.

    Returns ``(prefix, matched, depth)``; ``depth`` is 0 when the configured
    prefix (or nothing) was used.
    """
    probes = [item.get("Path") or "" for item in items[:sample] if item.get("Path")]
    if not probes:
        return configured, 0, 0

    def score(prefix: str) -> int:
        return sum(1 for path in probes if relative_path(path, prefix) in known)

    best_prefix, best_score, best_depth = configured, score(configured), 0
    for depth in range(1, 5):
        parts = path_parts(probes[0])
        if len(parts) <= depth:
            break
        prefix = "/" + "/".join(parts[:depth])
        value = score(prefix)
        if value > best_score:
            best_prefix, best_score, best_depth = prefix, value, depth
    return best_prefix, best_score, best_depth


def duration_ms(ticks: Any) -> Optional[int]:
    try:
        value = float(ticks or 0)
    except (TypeError, ValueError):
        return None
    return int(round(value / 10_000.0)) or None


def link(client: Client, connection, config: Dict[str, Any], *,
         page_size: int, dry_run: bool) -> Dict[str, Any]:
    """Page through Jellyfin and match every item to a track row."""
    prefix = str(S.setting(config, "jellyfin.path_prefix", "") or "")
    first = client.page(0, min(page_size, 500))
    total = int(first.get("TotalRecordCount") or 0)
    S.log(f"Jellyfin reports {S.human(total)} audio items"
          + (f" (path prefix {prefix!r})" if prefix else ""))
    items: List[Dict[str, Any]] = list(first.get("Items") or [])
    progress = S.Progress(max(1, total), label="reading items")
    progress.step(len(items))
    start = len(items)
    while start < total and items:
        page = client.page(start, page_size)
        chunk = page.get("Items") or []
        if not chunk:
            break
        items.extend(chunk)
        start += len(chunk)
        progress.step(len(chunk))
    progress.finish()

    known = {row["rel_path"] for row in connection.execute("SELECT rel_path FROM tracks")}
    detected, hits, depth = detect_prefix(items, known, prefix)
    if not hits:
        report_prefix_mismatch(items, known, prefix)
        return {"status": "prefix-mismatch", "items": len(items), "matched": 0,
                "without_id": len(known)}
    if detected != prefix:
        S.log(f"detected server path prefix {detected!r} (config.json says {prefix!r})"
              f" - using the detected one, matched {S.human(hits)} sampled items")
    else:
        S.log(f"server path prefix {detected!r} confirmed"
              f" ({S.human(hits)} of the sampled items matched)")
    prefix = detected

    matched = unmatched = skipped = 0
    sample_missing: List[str] = []
    stamp = S.now()
    updates: List[Tuple[Any, ...]] = []
    for item in items:
        if str(item.get("Type") or "Audio") != "Audio":
            skipped += 1
            continue
        rel = relative_path(item.get("Path") or "", prefix)
        if not rel:
            skipped += 1
            continue
        tags = item.get("ImageTags") or {}
        updates.append((
            str(item.get("Id") or "") or None,
            (tags.get("Primary") or None),
            (item.get("AlbumPrimaryImageTag") or None),
            (str(item.get("AlbumId")) if item.get("AlbumId") else None),
            duration_ms(item.get("RunTimeTicks")),
            (str(item.get("Container")) if item.get("Container") else None),
            stamp,
            rel,
        ))
    if dry_run:
        matched = sum(1 for entry in updates if entry[-1] in known)
        unmatched = len(updates) - matched
        missing = [entry[-1] for entry in updates if entry[-1] not in known][:10]
        S.log(f"dry run: {S.human(matched)} items match a track row,"
              f" {S.human(unmatched)} do not")
        for path in missing:
            print(f"      no local row for {path!r}")
        return {"items": len(items), "matched": matched, "unmatched": unmatched}

    cursor = connection.cursor()
    for chunk_start in range(0, len(updates), 500):
        cursor.executemany(
            "UPDATE tracks SET id=COALESCE(?, id), image_tag=?, album_image_tag=?,"
            " album_id=?, duration_ms=COALESCE(?, duration_ms), container=COALESCE(?, container),"
            " linked_at=? WHERE rel_path=?", updates[chunk_start:chunk_start + 500])
        matched += cursor.rowcount if cursor.rowcount and cursor.rowcount > 0 else 0
    connection.commit()
    linked = connection.execute(
        "SELECT COUNT(*) AS n FROM tracks WHERE id IS NOT NULL").fetchone()["n"]
    without = connection.execute(
        "SELECT COUNT(*) AS n FROM tracks WHERE id IS NULL").fetchone()["n"]
    if without:
        rows = connection.execute(
            "SELECT rel_path FROM tracks WHERE id IS NULL LIMIT 10")
        sample_missing = [row["rel_path"] for row in rows]
    return {"items": len(items), "linked": int(linked), "without_id": int(without),
            "sample_missing": sample_missing, "skipped": skipped}


def cmd_detect(client: Client, connection, config: Dict[str, Any]) -> int:
    """One request: which part of the server path is the library root?"""
    items = client.page(0, 300).get("Items") or []
    if not items:
        S.log("Jellyfin returned no audio items")
        return 1
    known = {row["rel_path"] for row in connection.execute("SELECT rel_path FROM tracks")}
    configured = str(S.setting(config, "jellyfin.path_prefix", "") or "")
    prefix, hits, _depth = detect_prefix(items, known, configured)
    S.log(f"configured prefix : {configured!r}")
    S.log(f"detected prefix   : {prefix!r}"
          f"  ({S.human(hits)} of {S.human(len(items))} sampled items matched a track)")
    for item in items[:3]:
        path = item.get("Path") or ""
        print(f"      {path!r}")
        print(f"        -> {relative_path(path, prefix)!r}")
    if not hits:
        report_prefix_mismatch(items, known, configured)
        return 2
    S.log("looks good - run: python3 step2_link.py")
    return 0


def parse_args(argv: Optional[Sequence[str]] = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Step 2 - link the tracks to their Jellyfin item ids",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--db", default="", help="dataset path (default: config.json)")
    parser.add_argument("--page-size", type=int, default=0, help="items per request")
    parser.add_argument("--dry-run", action="store_true", help="write nothing")
    parser.add_argument("--detect-only", action="store_true",
                        help="one request: report the detected server path prefix and stop")
    parser.add_argument("--prefix", default="",
                        help="override jellyfin.path_prefix (server-side path prefix)")
    return parser.parse_args(list(argv) if argv is not None else None)


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = parse_args(argv)
    config = S.load_config()
    if args.prefix:
        config.setdefault("jellyfin", {})["path_prefix"] = args.prefix
    page_size = args.page_size or int(S.setting(config, "jellyfin.page_size", 1000) or 1000)
    path = S.resolve(args.db) if args.db else S.db_path(config)
    client = Client(config)
    S.log("server:", client.base)
    connection = S.connect(path, create=False)
    try:
        if args.detect_only:
            return cmd_detect(client, connection, config)
        result = link(client, connection, config, page_size=page_size, dry_run=args.dry_run)
        print()
        for key, value in result.items():
            if key == "sample_missing":
                continue
            S.log(f"{key:14s} {S.human(value) if isinstance(value, int) else value}")
        if result.get("sample_missing"):
            S.log("some tracks have no Jellyfin item (first 10):")
            for item in result["sample_missing"]:
                print(f"      {item}")
        if not args.dry_run:
            S.meta_set(connection, "last_link", S.now())
            connection.commit()
    finally:
        connection.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
