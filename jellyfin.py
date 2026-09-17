#!/usr/bin/env python3
"""Jellyfin REST client, configuration storage and download helpers.

SimpleJellyMus is a music-only player: the library query asks Jellyfin for
``IncludeItemTypes=Audio`` and every item is additionally validated by
:meth:`JellyfinClient.is_music`, so video files (music videos, concerts, ...)
can never reach the player or the preload cache.
"""

from __future__ import annotations

import json
import os
import platform
import re
import uuid
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional
from urllib import error as urllib_error
from urllib import parse as urllib_parse
from urllib import request as urllib_request

CLIENT_NAME = "SimpleJellyMus"
CLIENT_VERSION = "1.0.0"
DEVICE_NAME = platform.node() or "desktop"

# Only audio-related fields are requested. MediaSources/MediaStreams are used by
# is_music() to reject anything that carries no audio stream at all.
ITEM_FIELDS = (
    "RunTimeTicks,Album,AlbumArtist,Artists,AlbumId,AlbumPrimaryImageTag,"
    "ImageTags,MediaSources,MediaStreams,ProductionYear,IndexNumber,Container,"
    "Genres,ParentId,MediaType"
)

_CONFIG_DIR = Path(os.environ.get("XDG_CONFIG_HOME") or (Path.home() / ".config")) / "simplejellymus"
CONFIG_FILE = _CONFIG_DIR / "config.json"
_CACHE_DIR = Path(os.environ.get("XDG_CACHE_HOME") or (Path.home() / ".cache")) / "simplejellymus"
AUDIO_CACHE_DIR = _CACHE_DIR / "audio"
COVER_CACHE_DIR = _CACHE_DIR / "covers"

DEFAULT_TIMEOUT = 15
DOWNLOAD_TIMEOUT = 60
DOWNLOAD_CHUNK = 128 * 1024

_SAFE_NAME = re.compile(r"[^a-z0-9]+")


class JellyfinError(Exception):
    """Base class for every Jellyfin interaction error."""


class AuthError(JellyfinError):
    """The server rejected our credentials or access token (HTTP 401/403)."""


class NetworkError(JellyfinError):
    """The Jellyfin server could not be reached."""


class DownloadCancelled(JellyfinError):
    """Raised internally when a preload download is cancelled."""


# --------------------------------------------------------------------------- #
# paths and configuration
# --------------------------------------------------------------------------- #

def ensure_dirs() -> None:
    """Create the configuration and cache directories (idempotent)."""
    for directory in (_CONFIG_DIR, AUDIO_CACHE_DIR, COVER_CACHE_DIR):
        directory.mkdir(parents=True, exist_ok=True)


def normalize_server_url(url: str) -> str:
    """Return *url* with a scheme and without a trailing slash."""
    url = (url or "").strip()
    if not url:
        raise JellyfinError("The server URL is empty")
    if not re.match(r"^https?://", url, re.IGNORECASE):
        url = "http://" + url
    return url.rstrip("/")


def load_config() -> Optional[Dict[str, Any]]:
    """Return the saved configuration, or ``None`` when there is none."""
    try:
        raw = CONFIG_FILE.read_text(encoding="utf-8")
        config = json.loads(raw)
    except (OSError, ValueError):
        return None
    return config if isinstance(config, dict) else None


def save_config(config: Dict[str, Any]) -> None:
    """Persist the configuration with owner-only permissions."""
    ensure_dirs()
    tmp = CONFIG_FILE.with_name(CONFIG_FILE.name + ".tmp")
    tmp.write_text(json.dumps(config, indent=2), encoding="utf-8")
    os.chmod(tmp, 0o600)
    tmp.replace(CONFIG_FILE)


def clear_config() -> None:
    """Forget the saved login."""
    try:
        CONFIG_FILE.unlink()
    except OSError:
        pass


def stored_device_id() -> str:
    """Return the device id Jellyfin already knows us by, if any."""
    config = load_config() or {}
    return str(config.get("device_id") or "")


def audio_extension(item: Dict[str, Any]) -> str:
    """Best-effort file extension for a Jellyfin audio item."""
    container = str(item.get("Container") or "").split(",")[0].strip().lower()
    container = _SAFE_NAME.sub("", container)
    if not container:
        return "audio"
    if container in {"mov", "mp4", "m4a", "aac"}:
        return "m4a"
    if len(container) > 4:
        return "audio"
    return container

_STORED_TOKEN = object()


class JellyfinClient:
    """Minimal Jellyfin client: login, random audio batches, streams, images."""

    def __init__(self, server_url: str, *, access_token: str = "", user_id: str = "",
                 username: str = "", device_id: str = "", timeout: int = DEFAULT_TIMEOUT,
                 debug: bool = False) -> None:
        self.server_url = normalize_server_url(server_url)
        self.access_token = access_token or ""
        self.user_id = user_id or ""
        self.username = username or ""
        self.device_id = device_id or stored_device_id() or str(uuid.uuid4())
        self.timeout = timeout
        self.debug = debug

    # ------------------------------------------------------------- configuration
    @classmethod
    def from_config(cls, config: Dict[str, Any], **kwargs: Any) -> "JellyfinClient":
        return cls(
            config.get("server_url", ""),
            access_token=config.get("access_token", ""),
            user_id=config.get("user_id", ""),
            username=config.get("username", ""),
            device_id=config.get("device_id", ""),
            **kwargs,
        )

    def to_config(self, extra: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        config: Dict[str, Any] = {
            "server_url": self.server_url,
            "username": self.username,
            "user_id": self.user_id,
            "access_token": self.access_token,
            "device_id": self.device_id,
        }
        if extra:
            config.update(extra)
        return config

    # --------------------------------------------------------------- HTTP core
    def _log(self, *parts: Any) -> None:
        if self.debug:
            print("[jellyfin]", *parts, flush=True)

    def _url(self, path: str, params: Optional[Dict[str, Any]] = None) -> str:
        url = path if path.startswith("http") else self.server_url + path
        if params:
            url += ("&" if "?" in url else "?") + urllib_parse.urlencode(params)
        return url

    def _headers(self, token: Any = _STORED_TOKEN, accept: str = "application/json") -> Dict[str, str]:
        value = (
            f'MediaBrowser Client="{CLIENT_NAME}", Device="{DEVICE_NAME}", '
            f'DeviceId="{self.device_id}", Version="{CLIENT_VERSION}"'
        )
        active = self.access_token if token is _STORED_TOKEN else token
        if active:
            value += f', Token="{active}"'
        headers = {"X-Emby-Authorization": value}
        if accept:
            headers["Accept"] = accept
        return headers

    def _request(self, path: str, *, method: str = "GET", params: Optional[Dict[str, Any]] = None,
                 payload: Optional[Dict[str, Any]] = None, token: Any = _STORED_TOKEN,
                 timeout: Optional[float] = None, accept: str = "application/json"):
        """Perform one HTTP request, translating errors into JellyfinError."""
        url = self._url(path, params)
        body = None
        headers = self._headers(token, accept)
        if payload is not None:
            body = json.dumps(payload).encode("utf-8")
            headers["Content-Type"] = "application/json"
        self._log(method, url)
        request = urllib_request.Request(url, data=body, headers=headers, method=method)
        try:
            return urllib_request.urlopen(request, timeout=timeout or self.timeout)
        except urllib_error.HTTPError as exc:
            detail = ""
            try:
                detail = " ".join(exc.read().decode("utf-8", "replace").split())[:200]
            except Exception:
                detail = ""
            if exc.code in (401, 403):
                raise AuthError(f"Jellyfin refused the login (HTTP {exc.code})") from exc
            message = f"Jellyfin returned HTTP {exc.code}"
            if detail:
                message += f" - {detail}"
            raise JellyfinError(message) from exc
        except urllib_error.URLError as exc:
            raise NetworkError(f"Cannot reach {self.server_url} ({exc.reason})") from exc
        except OSError as exc:
            raise NetworkError(f"Cannot reach {self.server_url} ({exc})") from exc

    def _json(self, path: str, **kwargs: Any) -> Dict[str, Any]:
        with self._request(path, **kwargs) as response:
            raw = response.read()
        if not raw:
            return {}
        try:
            data = json.loads(raw.decode("utf-8"))
        except ValueError as exc:
            raise JellyfinError("Jellyfin sent an invalid JSON response") from exc
        if not isinstance(data, dict):
            raise JellyfinError("Unexpected response from Jellyfin")
        return data

    # ------------------------------------------------------------- public API
    def test_connection(self) -> Dict[str, Any]:
        """Return ``/System/Info/Public`` (works without a token)."""
        return self._json("/System/Info/Public", token="")

    def authenticate(self, username: str, password: str) -> Dict[str, Any]:
        """Log in and remember the access token and user id."""
        result = self._json(
            "/Users/authenticatebyname",
            method="POST",
            payload={"Username": username, "Pw": password or ""},
            token="",
        )
        token = result.get("AccessToken")
        user = result.get("User") or {}
        if not token or not user.get("Id"):
            raise AuthError("The server answered but returned no access token")
        self.access_token = str(token)
        self.user_id = str(user["Id"])
        self.username = str(user.get("Name") or username)
        self._log("authenticated as", self.username)
        return result

    def validate_token(self) -> Dict[str, Any]:
        """Check the stored token (raises :class:`AuthError` when it is stale)."""
        user = self._json("/Users/Me")
        if user.get("Id"):
            self.user_id = str(user["Id"])
            self.username = str(user.get("Name") or self.username)
        return user

    def random_batch(self, limit: int = 200) -> List[Dict[str, Any]]:
        """Return a server-side random batch of audio items only."""
        if not self.user_id:
            raise JellyfinError("Not logged in")
        data = self._json(
            f"/Users/{self.user_id}/Items",
            params={
                "Recursive": "true",
                "IncludeItemTypes": "Audio",
                "MediaTypes": "Audio",
                "SortBy": "Random",
                "Limit": str(int(limit)),
                "Fields": ITEM_FIELDS,
            },
        )
        items = data.get("Items") or []
        music = [item for item in items if self.is_music(item)]
        self._log(f"random batch: {len(items)} items, {len(music)} audio")
        return music

    def stream_url(self, item: Dict[str, Any], *, static: bool = True) -> str:
        """Direct (``static=true``) or transcoded - always audio-only - URL."""
        item_id = item.get("Id")
        if static:
            return self._url(f"/Audio/{item_id}/stream",
                             {"static": "true", "api_key": self.access_token})
        params = {
            "UserId": self.user_id,
            "DeviceId": self.device_id,
            "api_key": self.access_token,
            "MaxStreamingBitrate": "320000",
            "AudioCodec": "mp3",
            "Container": "mp3",
            "TranscodingContainer": "mp3",
            "TranscodingProtocol": "http",
        }
        return self._url(f"/Audio/{item_id}/universal", params)

    def image_url(self, item: Dict[str, Any]) -> Optional[str]:
        """Cover art URL for the item, falling back to its album artwork."""
        api_key = urllib_parse.quote(self.access_token, safe="")
        tags = item.get("ImageTags") or {}
        if tags.get("Primary") and item.get("Id"):
            return self._url(
                f"/Items/{item['Id']}/Images/Primary",
                {"fillHeight": "800", "quality": "90", "tag": tags["Primary"], "api_key": api_key},
            )
        album_id = item.get("AlbumId")
        album_tag = item.get("AlbumPrimaryImageTag")
        if album_id and album_tag:
            return self._url(
                f"/Items/{album_id}/Images/Primary",
                {"fillHeight": "800", "quality": "90", "tag": album_tag, "api_key": api_key},
            )
        return None

    def image_cache_path(self, item: Dict[str, Any]) -> Path:
        """Local file used to cache the cover art of *item*."""
        tag = ((item.get("ImageTags") or {}).get("Primary")
               or item.get("AlbumPrimaryImageTag") or "none")
        key = f"{item.get('AlbumId') or item.get('Id')}_{str(tag)[:8]}"
        return COVER_CACHE_DIR / f"{_SAFE_NAME.sub('', key.lower()) or 'cover'}.jpg"

    def audio_cache_path(self, item: Dict[str, Any]) -> Path:
        """Local file used to cache the audio of *item*."""
        return AUDIO_CACHE_DIR / f"{item.get('Id')}.{audio_extension(item)}"

    def download_image(self, item: Dict[str, Any]) -> Optional[Path]:
        """Download (or reuse) the cover art and return the cached file."""
        destination = self.image_cache_path(item)
        try:
            if destination.exists() and destination.stat().st_size > 0:
                return destination
        except OSError:
            pass
        url = self.image_url(item)
        if not url:
            return None
        try:
            with self._request(url, timeout=30, accept="image/*") as response:
                data = response.read()
        except JellyfinError as exc:
            self._log("cover download failed:", exc)
            return None
        if not data:
            return None
        ensure_dirs()
        try:
            destination.write_bytes(data)
        except OSError as exc:
            self._log("cover could not be stored:", exc)
            return None
        return destination

    def download_audio(self, item: Dict[str, Any], destination: Path,
                       is_cancelled: Optional[Callable[[], bool]] = None) -> Path:
        """Stream an audio item to *destination* (atomic and cancellable)."""
        destination = Path(destination)
        ensure_dirs()
        try:
            destination.parent.mkdir(parents=True, exist_ok=True)
        except OSError:
            pass
        part = destination.with_name(destination.name + ".part")
        downloaded = 0
        try:
            with self._request(self.stream_url(item, static=True),
                               timeout=DOWNLOAD_TIMEOUT) as response, open(part, "wb") as handle:
                while True:
                    if is_cancelled is not None and is_cancelled():
                        raise DownloadCancelled("preload cancelled")
                    chunk = response.read(DOWNLOAD_CHUNK)
                    if not chunk:
                        break
                    handle.write(chunk)
                    downloaded += len(chunk)
        except BaseException:
            try:
                part.unlink()
            except OSError:
                pass
            raise
        if downloaded == 0:
            try:
                part.unlink()
            except OSError:
                pass
            raise JellyfinError("The server sent an empty audio stream")
        part.replace(destination)
        self._log(f"preloaded {item.get('Name')!r} -> {destination.name} "
                  f"({downloaded / 1e6:.1f} MB)")
        return destination

    # ---------------------------------------------------------------- helpers
    @staticmethod
    def is_music(item: Any) -> bool:
        """True only for pure audio items - never videos or music videos."""
        if not isinstance(item, dict) or not item.get("Id"):
            return False
        if item.get("Type") != "Audio":
            return False
        media_type = item.get("MediaType")
        if media_type and str(media_type).lower() != "audio":
            return False
        if item.get("VideoType"):
            return False
        for source in item.get("MediaSources") or []:
            streams = source.get("MediaStreams")
            if streams and not any(str(s.get("Type", "")).lower() == "audio" for s in streams):
                return False
        return True

    @staticmethod
    def duration_seconds(item: Dict[str, Any]) -> float:
        """Duration of an item in seconds (0.0 when unknown)."""
        try:
            return max(0.0, float(item.get("RunTimeTicks") or 0.0) / 10_000_000.0)
        except (TypeError, ValueError):
            return 0.0

    @staticmethod
    def display_title(item: Dict[str, Any]) -> str:
        return str(item.get("Name") or "Unknown title")

    @staticmethod
    def display_artist(item: Dict[str, Any]) -> str:
        artists = item.get("Artists") or []
        if artists:
            return ", ".join(str(artist) for artist in artists)
        artist = item.get("AlbumArtist") or item.get("Album") or ""
        return str(artist) if artist else "Unknown artist"

    @staticmethod
    def artist_key(item: Dict[str, Any]) -> str:
        artists = item.get("Artists") or []
        return str(item.get("AlbumArtist") or (artists[0] if artists else "") or "")



