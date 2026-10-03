#!/usr/bin/env python3
"""Cover art for the Discord presence: one local file becomes a public URL.

Discord cannot be handed a file when a song starts. A rich-presence image is
either an asset key uploaded to the application in the Discord Developer Portal
(which is what the static ``logo``/``playing``/``paused`` images are) or a URL
that Discord fetches itself. This module produces such a URL for the cover that
is already in the local cache.

It uploads to `litterbox <https://litterbox.catbox.moe/>`_ - anonymous, no
account, and the file is deleted again after the chosen lifetime (three days by
default) - and falls back to `catbox <https://catbox.moe/>` if that fails. Both
are third parties, which is exactly why this is opt-in (``--discord-cover``):
what leaves the machine is a downscaled copy of the album art and nothing else,
no account, no name, no listening history. Nothing is uploaded unless you ask
for it, nothing is uploaded twice (the URL is remembered per cover file in
``~/.cache/simplejellymus/discord_covers.json``), and any failure simply leaves
the static image in place.
"""

from __future__ import annotations

import json
import os
import re
import tempfile
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

# The two anonymous image hosts and their upload endpoints. Litterbox deletes
# the file by itself, catbox keeps it (which is why litterbox is the default).
LITTERBOX_API = "https://litterbox.catbox.moe/resources/internals/api.php"
CATBOX_API = "https://catbox.moe/user/api.php"

# Where the URLs are remembered, next to the other caches (the same path
# jellyfin.py uses, spelled out to keep this module independent of it).
URL_CACHE_FILE = (Path(os.environ.get("XDG_CACHE_HOME") or (Path.home() / ".cache"))
                  / "simplejellymus" / "discord_covers.json")

USER_AGENT = "SimpleJellyMus"
# Litterbox deletes the file after this long (its only supported values are
# 1h, 12h, 24h and 72h).
DEFAULT_EXPIRY = "72h"
# Discord only ever draws a thumbnail; half a megapixel upscaled would just be
# a slower upload. 512 px keeps the average cover well below 100 kB.
DEFAULT_MAX_SIZE = 512
DEFAULT_QUALITY = 85
# How long one upload may take, and the least time between two of them.
DEFAULT_TIMEOUT = 20.0
DEFAULT_SPACING = 5.0
# The URL cache is a convenience, not an archive: the oldest entries are
# dropped once it grows past this.
MAX_CACHE_ENTRIES = 200

_EXPIRY_UNITS = {"s": 1, "m": 60, "h": 3600, "d": 86400}
# Anything in here is not something Discord could fetch from the internet.
_PRIVATE_HOST = re.compile(
    r"^(localhost|0\.0\.0\.0|127\.|10\.|169\.254\.|192\.168\.|"
    r"172\.(1[6-9]|2[0-9]|3[01])\.|\[?::1\]?$)", re.IGNORECASE)


def _multipart(fields: Dict[str, str], file_field: str, path: Path) -> Tuple[bytes, str]:
    """A multipart/form-data body for one file and a few fields (no requests)."""
    boundary = "----SimpleJellyMus" + uuid.uuid4().hex
    chunks: List[bytes] = []
    for name, value in fields.items():
        chunks.append(
            f'--{boundary}\r\nContent-Disposition: form-data; name="{name}"\r\n\r\n'
            f"{value}\r\n".encode("utf-8"))
    chunks.append(
        f'--{boundary}\r\nContent-Disposition: form-data; name="{file_field}"; '
        f'filename="{path.name}"\r\nContent-Type: application/octet-stream\r\n\r\n'
        .encode("utf-8"))
    chunks.append(path.read_bytes())
    chunks.append(f"\r\n--{boundary}--\r\n".encode("utf-8"))
    return b"".join(chunks), f"multipart/form-data; boundary={boundary}"


def _public_url(value: Any) -> bool:
    """True only for a plain public http(s) URL Discord could download.

    A host that answers with something else - an error page, a private address,
    a wall of HTML - is treated as a failed upload: a broken image in the
    profile would be worse than the static one.
    """
    text = str(value or "").strip()
    if not text.startswith(("http://", "https://")) or len(text) > 300 or " " in text:
        return False
    host = urllib.parse.urlsplit(text).hostname or ""
    return bool(host) and not _PRIVATE_HOST.match(host)


def _lifetime(value: Any) -> Optional[float]:
    """``"72h"`` as seconds - ``None`` for anything that is not a lifetime."""
    match = re.fullmatch(r"\s*(\d+)\s*([smhd])\s*", str(value or ""), re.IGNORECASE)
    if not match:
        return None
    return float(match.group(1)) * _EXPIRY_UNITS[match.group(2).lower()]


class CoverUploader:
    """Turns a local cover file into a public URL, once per file.

    Blocking by design: only the presence worker calls it, never the UI and
    never the engine, so an unreachable image host cannot hold up playback.
    """

    def __init__(self, *, host: str = "litterbox", expiry: str = DEFAULT_EXPIRY,
                 endpoint: Optional[str] = None, cache_file: Optional[Any] = None,
                 max_size: int = DEFAULT_MAX_SIZE, quality: int = DEFAULT_QUALITY,
                 timeout: float = DEFAULT_TIMEOUT, spacing: float = DEFAULT_SPACING,
                 fallback: bool = True, debug: bool = False) -> None:
        self.host = "catbox" if str(host).lower() == "catbox" else "litterbox"
        self.expiry = str(expiry or DEFAULT_EXPIRY)
        # An own endpoint (the self-test uses one) replaces both hosts.
        self.endpoint = str(endpoint) if endpoint else None
        self.fallback = bool(fallback) and self.endpoint is None
        self.cache_file = Path(cache_file) if cache_file else URL_CACHE_FILE
        self.max_size = max(64, int(max_size))
        self.quality = max(1, min(95, int(quality)))
        self.timeout = float(timeout)
        self.spacing = max(0.0, float(spacing))
        self.debug = bool(debug)
        # Published covers so far (the self-test reads it).
        self.uploads = 0
        self._lock = threading.RLock()
        self._next_upload = 0.0
        self._urls = self._load()

    def _log(self, message: str) -> None:
        if self.debug:
            print(f"[discord] {message}", flush=True)

    def url_for(self, path: Optional[Any],
                is_cancelled: Optional[Callable[[], bool]] = None) -> Optional[str]:
        """The public URL of *path*, or ``None`` when it cannot be published."""
        source = self._local_file(path)
        if source is None:
            return None
        key = self._key(source)
        known = self._cached(key)
        if known:
            return known
        if is_cancelled is not None and is_cancelled():
            return None
        prepared = self._prepare(source)
        if prepared is None:
            return None
        try:
            url = self._upload(prepared, is_cancelled)
        finally:
            if prepared != source:
                try:
                    prepared.unlink()
                except OSError:
                    pass
        if url:
            self._remember(key, url)
        return url

    # ------------------------------------------------------------------- helpers
    @staticmethod
    def _local_file(path: Any) -> Optional[Path]:
        """*path* as an existing, non-empty file - or ``None``."""
        if not path:
            return None
        try:
            candidate = Path(os.fspath(path))
            if candidate.is_file() and candidate.stat().st_size > 0:
                return candidate
        except (OSError, TypeError, ValueError):
            pass
        return None

    @staticmethod
    def _key(path: Path) -> str:
        """Identifies the cover *file*, not just its name: artwork replaced for
        the same album has to be uploaded again."""
        try:
            info = path.stat()
            return f"{path.name}:{info.st_size}:{int(info.st_mtime)}"
        except OSError:
            return path.name

    def _prepare(self, path: Path) -> Optional[Path]:
        """A small JPEG copy of *path* (Discord only draws a thumbnail anyway).

        Pillow is already a dependency; when it cannot decode the file (or is not
        installed at all) the original is uploaded unchanged instead - an odd
        format is still better than no artwork.
        """
        try:
            from PIL import Image
        except ImportError:                      # pragma: no cover - Pillow is required
            return path
        temporary: Optional[Path] = None
        try:
            with Image.open(path) as image:
                if max(image.size) <= self.max_size and path.suffix.lower() in (".jpg", ".jpeg"):
                    return path
                small = image.convert("RGB")
                small.thumbnail((self.max_size, self.max_size), Image.LANCZOS)
                with tempfile.NamedTemporaryFile(suffix=".jpg", delete=False) as handle:
                    temporary = Path(handle.name)
                small.save(temporary, "JPEG", quality=self.quality)
            return temporary
        except (OSError, ValueError) as exc:
            self._log(f"cover could not be prepared ({exc}) - uploading it as it is")
            if temporary is not None:
                try:
                    temporary.unlink()
                except OSError:
                    pass
            return path

    # -------------------------------------------------------------------- upload
    def _upload(self, path: Path, is_cancelled: Optional[Callable[[], bool]]) -> Optional[str]:
        """Try the configured host, then the fallback; the first URL wins."""
        for name, endpoint in self._attempts():
            if is_cancelled is not None and is_cancelled():
                return None
            self._wait_for_slot()
            fields = {"reqtype": "fileupload"}
            if name == "litterbox":
                fields["time"] = self.expiry
            url = self._post(endpoint, fields, path, name)
            if url:
                return url
        return None

    def _attempts(self) -> List[Tuple[str, str]]:
        """``(host name, endpoint)`` pairs to try, in order."""
        if self.endpoint is not None:
            return [(self.host, self.endpoint)]
        if self.host == "catbox":
            return [("catbox", CATBOX_API)]
        attempts = [("litterbox", LITTERBOX_API)]
        if self.fallback:
            attempts.append(("catbox", CATBOX_API))
        return attempts

    def _post(self, endpoint: str, fields: Dict[str, str], path: Path, host: str) -> Optional[str]:
        """One multipart upload; returns the public URL the host reports."""
        try:
            body, content_type = _multipart(fields, "fileToUpload", path)
        except OSError as exc:
            self._log(f"cannot read {path.name}: {exc}")
            return None
        request = urllib.request.Request(
            endpoint, data=body, method="POST",
            headers={"Content-Type": content_type, "User-Agent": USER_AGENT,
                     "Accept": "text/plain"})
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                answer = response.read().decode("utf-8", "replace").strip()
        except (urllib.error.URLError, OSError, ValueError) as exc:
            self._log(f"{host} upload failed: {exc}")
            return None
        if not _public_url(answer):
            self._log(f"{host} answered with something unexpected: {answer[:120]!r}")
            return None
        self.uploads += 1
        self._log(f"artwork published on {host}: {answer}")
        return answer

    def _wait_for_slot(self) -> None:
        """Keep at least *spacing* seconds between two uploads to the same host."""
        with self._lock:
            now = time.monotonic()
            wait = self._next_upload - now
            self._next_upload = max(now, self._next_upload) + self.spacing
        if wait > 0:
            time.sleep(wait)

    # --------------------------------------------------------------------- cache
    def _load(self) -> Dict[str, Any]:
        """The remembered URLs (a damaged or missing file is simply empty)."""
        try:
            stored = json.loads(self.cache_file.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}
        return stored if isinstance(stored, dict) else {}

    def _cached(self, key: str) -> Optional[str]:
        """The URL already published for that cover file, if it is still alive."""
        entry = self._urls.get(key)
        if not isinstance(entry, dict) or not _public_url(entry.get("url")):
            return None
        if entry.get("host") != "catbox" and self._expired(entry):
            # Litterbox deletes the file after its lifetime, and a URL that no
            # longer resolves would put a broken image into the profile.
            with self._lock:
                self._urls.pop(key, None)
            self._persist()
            return None
        return str(entry["url"])

    def _expired(self, entry: Dict[str, Any]) -> bool:
        lifetime = _lifetime(entry.get("expiry"))
        if not lifetime:
            return False
        try:
            uploaded = float(entry.get("uploaded") or 0)
        except (TypeError, ValueError):
            return False
        # Half the lifetime is enough to stop trusting it: uploads happen when a
        # track starts, and the file may already be gone a little earlier.
        return time.time() > uploaded + lifetime * 0.5

    def _remember(self, key: str, url: str) -> None:
        with self._lock:
            self._urls[key] = {"url": url, "host": self.host, "expiry": self.expiry,
                               "uploaded": int(time.time())}
            self._prune()
            self._persist()

    def _prune(self) -> None:
        """Keep the cache small - it is a convenience, not an archive."""
        if len(self._urls) <= MAX_CACHE_ENTRIES:
            return
        def uploaded(entry: Any) -> float:
            try:
                return float((entry or {}).get("uploaded") or 0)
            except (TypeError, ValueError, AttributeError):
                return 0.0
        for key in sorted(self._urls, key=uploaded)[:len(self._urls) - MAX_CACHE_ENTRIES]:
            self._urls.pop(key, None)

    def _persist(self) -> None:
        """Write the URL cache (owner only), never letting a failure escape."""
        try:
            self.cache_file.parent.mkdir(parents=True, exist_ok=True)
            temporary = self.cache_file.with_name(self.cache_file.name + ".tmp")
            temporary.write_text(json.dumps(self._urls, indent=2), encoding="utf-8")
            os.chmod(temporary, 0o600)
            temporary.replace(self.cache_file)
        except OSError as exc:
            self._log(f"the cover cache could not be written: {exc}")
