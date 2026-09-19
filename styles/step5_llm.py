#!/usr/bin/env python3
"""Step 5 - DeepSeek fills in the styles the tags never had.

This layer knows bands, not files: it is asked about *artists* (with the albums
and the raw tags from your library as context) and may only answer with terms
from ``data/styles.json``. Its labels are stored with ``source='llm'``, which
ranks *below* a real tag, the rules and the audio classifier - so it can fill a
gap but never overwrite evidence.

    python3 step5_llm.py --estimate             # no key, no network: artists, tokens, cost
    python3 step5_llm.py --probe                # one tiny call: is the key valid?
    python3 step5_llm.py --sample 20            # 20 artists through the real prompt: review
    python3 step5_llm.py --run                  # the fill (cached, budget-guarded)
    python3 step5_llm.py --run --max-cost 0.50  # refuse to start above this projection
    python3 step5_llm.py --audit --min-tracks 5 # second opinion on the tagged artists

The API key is read from ``DEEPSEEK_API_KEY``, from a ``styles/.env`` file
(``DEEPSEEK_API_KEY=...``, git-ignored) or from ``llm.api_key`` in
``config.local.json``. It is never printed and never written anywhere.
"""

from __future__ import annotations

import argparse
import csv
import json
import os
import re
import sys
import threading
import time
import urllib.error
import urllib.request
from collections import Counter, defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

sys.path.insert(0, str(Path(__file__).resolve().parent))

import styles_db as S  # noqa: E402
from step4_normalize import Normalizer  # noqa: E402

PROMPT_VERSION = "2026-02-vocabulary-1"
CHARS_PER_TOKEN = 4.0          # rough, used only for --estimate (real usage is billed)

# Artist names that are really a label, a distributor or a compilation: asking
# the model about them would only produce noise.
NOT_AN_ARTIST = re.compile(
    r"(records|recordings|music|media|label|spin|vision|elite|distribution|"
    r"promotions|entertainment|productions|studios?|various artists|va)\s*$",
    re.IGNORECASE)


def load_env_file(path: Path) -> int:
    """Read ``KEY=VALUE`` lines into the environment (never overwriting)."""
    if not path.exists():
        return 0
    loaded = 0
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key, value = key.strip(), value.strip().strip("'\"")
        if key and value and key not in os.environ:
            os.environ[key] = value
            loaded += 1
    return loaded


def api_key(config: Dict[str, Any]) -> str:
    """The DeepSeek key: environment first, then config.local.json."""
    name = str(S.setting(config, "llm.api_key_env", "DEEPSEEK_API_KEY") or "DEEPSEEK_API_KEY")
    key = os.environ.get(name, "") or str(S.setting(config, "llm.api_key", "") or "")
    if not key:
        raise SystemExit(
            f"no API key: put {name}=... in styles/.env (or config.local.json -> llm.api_key)")
    return key


class DeepSeek:
    """A very small OpenAI-compatible client (stdlib only, no SDK)."""

    def __init__(self, config: Dict[str, Any], *, model: str = "", debug: bool = False) -> None:
        self.base = str(S.setting(config, "llm.base_url", "https://api.deepseek.com")).rstrip("/")
        self.key = api_key(config)
        self.model = model or str(S.setting(config, "llm.model", "deepseek-flash"))
        self.timeout = float(S.setting(config, "llm.timeout", 300) or 300)
        self.thinking = bool(S.setting(config, "llm.thinking", False))
        self.temperature = float(S.setting(config, "llm.temperature", 0) or 0)
        self.max_tokens = int(S.setting(config, "llm.max_tokens", 4000) or 4000)
        self.debug = debug
        self.price_hit = float(S.setting(config, "llm.input_price_per_mtok_cache_hit", 0.006) or 0)
        self.price_miss = float(
            S.setting(config, "llm.input_price_per_mtok_cache_miss", 0.30) or 0.30)
        self.price_out = float(S.setting(config, "llm.output_price_per_mtok", 1.20) or 1.20)
        self.calls = 0
        self.tokens_in = 0
        self.tokens_out = 0
        self.cached_in = 0
        self.cost = 0.0

    def price(self, tokens_in: int, tokens_out: int, cached_in: int = 0) -> float:
        """What a request with these token counts costs (cache hits are cheaper)."""
        misses = max(0, int(tokens_in) - int(cached_in))
        return (int(cached_in) / 1e6) * self.price_hit + \
               (misses / 1e6) * self.price_miss + (int(tokens_out) / 1e6) * self.price_out

    def chat(self, messages: List[Dict[str, str]], *, model: str = "",
             max_tokens: int = 0, json_mode: bool = True) -> Dict[str, Any]:
        """One chat completion; returns ``{content, usage, cost, ...}``.

        DeepSeek's current models reason by default, which used to eat the whole
        token budget before any JSON was emitted - so the mode is always stated
        explicitly. If an account rejects one of the optional fields, it is
        dropped and the request retried instead of failing.
        """
        base: Dict[str, Any] = {
            "model": model or self.model,
            "messages": messages,
            "temperature": self.temperature,
            "max_tokens": int(max_tokens or self.max_tokens),
            "stream": False,
            "thinking": {"type": "enabled" if self.thinking else "disabled"},
        }
        if json_mode:
            base["response_format"] = {"type": "json_object"}
        dropped: set = set()
        last_error = ""
        for _attempt in range(5):
            payload = {key: value for key, value in base.items() if key not in dropped}
            body = json.dumps(payload).encode("utf-8")
            request = urllib.request.Request(
                f"{self.base}/chat/completions", data=body, method="POST",
                headers={"Content-Type": "application/json",
                         "Authorization": f"Bearer {self.key}",
                         "Accept": "application/json"})
            try:
                with urllib.request.urlopen(request, timeout=self.timeout) as response:
                    data = json.loads(response.read().decode("utf-8", "replace"))
                break
            except urllib.error.HTTPError as exc:
                detail = ""
                try:
                    detail = exc.read().decode("utf-8", "replace")[:300]
                except Exception:
                    pass
                last_error = f"HTTP {exc.code}: {detail}"
                if exc.code in (400, 422):
                    for optional in ("response_format", "thinking"):
                        if optional in payload and optional not in dropped:
                            dropped.add(optional)
                            break
                    else:
                        raise SystemExit(f"DeepSeek refused the request ({last_error})")
                    continue
                if exc.code in (429, 500, 502, 503, 504):
                    time.sleep(2.0 * (_attempt + 1))
                    continue
                raise SystemExit(f"DeepSeek refused the request ({last_error})")
            except (urllib.error.URLError, TimeoutError, OSError) as exc:
                last_error = str(exc)
                time.sleep(2.0 * (_attempt + 1))
        else:
            raise SystemExit(f"DeepSeek unreachable: {last_error}")

        usage = data.get("usage") or {}
        tokens_in = int(usage.get("prompt_tokens") or 0)
        tokens_out = int(usage.get("completion_tokens") or 0)
        cached = int(usage.get("prompt_cache_hit_tokens") or 0)
        reasoning = int((usage.get("completion_tokens_details") or {})
                        .get("reasoning_tokens") or 0)
        cost = self.price(tokens_in, tokens_out, cached)
        self.calls += 1
        self.tokens_in += tokens_in
        self.tokens_out += tokens_out
        self.cached_in += cached
        self.cost += cost
        content = ""
        finish_reason = ""
        choices = data.get("choices") or []
        if choices:
            choice = choices[0]
            message = choice.get("message") or {}
            content = str(message.get("content") or "").strip()
            finish_reason = str(choice.get("finish_reason") or "")
        if self.debug:
            S.log(f"call {self.calls}: in={tokens_in} (cached {cached}) out={tokens_out}"
                  f" reasoning={reasoning} finish={finish_reason or '?'} ${cost:.5f}"
                  f" dropped={sorted(dropped) or 'none'}")
        return {"content": content, "usage": usage, "cost": cost, "tokens_in": tokens_in,
                "tokens_out": tokens_out, "cached_in": cached, "reasoning_tokens": reasoning,
                "finish_reason": finish_reason, "dropped": sorted(dropped),
                "model": str(data.get("model") or payload["model"])}


def parse_json_block(text: str) -> Dict[str, Any]:
    """Best-effort JSON from a model answer (it may wrap it in prose or a fence)."""
    text = (text or "").strip()
    if text.startswith("```"):
        text = re.sub(r"^```[a-zA-Z]*\n?", "", text)
        text = re.sub(r"\n?```$", "", text).strip()
    try:
        data = json.loads(text)
        return data if isinstance(data, dict) else {"artists": data}
    except ValueError:
        pass
    start, end = text.find("{"), text.rfind("}")
    if start >= 0 and end > start:
        try:
            data = json.loads(text[start:end + 1])
            return data if isinstance(data, dict) else {}
        except ValueError:
            return {}
    return {}


# --------------------------------------------------------------------------- #
# what the model will be asked about
# --------------------------------------------------------------------------- #

def vocabulary_names(connection) -> List[str]:
    """The closed vocabulary: real styles only (no family placeholders)."""
    return [row["name"] for row in connection.execute(
        "SELECT name FROM styles WHERE name NOT LIKE '%(unspecified)'"
        " ORDER BY family, name")]


def prompt_fingerprint(config: Dict[str, Any], vocabulary: Sequence[str]) -> str:
    """Identifies a cached answer: same model + same prompt + same vocabulary."""
    import hashlib
    digest = hashlib.sha1("\n".join(vocabulary).encode("utf-8")).hexdigest()[:10]
    model = str(S.setting(config, "llm.model", "deepseek-flash"))
    return f"{model}|{PROMPT_VERSION}|{digest}"


def artist_key_for(artists: Any, album_artist: Any, title: Any = "") -> Tuple[str, str]:
    """The artist this track belongs to, and the name it was found under.

    The track-level ``artists`` tag wins: folders are often named after the
    label ("20BuckSpin/…") while the file itself knows the band. Two traps are
    handled: a tag that only repeats the track title (a real tagging mistake,
    e.g. "5 Steps Of Freedom" as the artist of "5 Steps Of Freedom"), and an
    empty tag - both fall back to the album artist.
    """
    first = re.split(r",| & | and | feat\.? | vs\.? |/", str(artists or ""), maxsplit=1)[0]
    name = first.strip()
    if name and title and S.norm_key(name) == S.norm_key(title):
        name = ""
    if not name:
        name = str(album_artist or "").strip()
    return S.norm_key(name), name


def build_artist_index(connection) -> Dict[str, Dict[str, Any]]:
    """One record per artist: which tracks lack a style, and what we already know."""
    index: Dict[str, Dict[str, Any]] = {}
    rows = connection.execute(
        "SELECT rel_path, artists, album_artist, name, album, year, raw_genre, primary_style,"
        f"       ({S.FAMILY_STYLE_SQL}) AS family_only, ({S.VAGUE_STYLE_SQL}) AS vague"
        " FROM tracks")
    for row in rows:
        key, name = artist_key_for(row["artists"], row["album_artist"], row["name"])
        if not key:
            continue
        record = index.get(key)
        if record is None:
            record = index[key] = {
                "key": key, "name": name, "tracks": [], "needs": [], "albums": [],
                "tags": Counter(), "styles": Counter(), "years": [],
                # "Looks like a label" must be judged on the name we would send
                # to the model, not on the folder: label samplers are full of
                # real bands whose album_artist happens to be the label.
                "labelish": bool(NOT_AN_ARTIST.search(name)),
            }
        record["tracks"].append(row["rel_path"])
        if row["primary_style"] is None or row["family_only"] or row["vague"]:
            record["needs"].append(row["rel_path"])
        if row["primary_style"]:
            record["styles"][row["primary_style"]] += 1
        if row["raw_genre"]:
            record["tags"][row["raw_genre"]] += 1
        album = str(row["album"] or "").strip()
        if album and album not in record["albums"]:
            record["albums"].append(album)
        if row["year"]:
            record["years"].append(int(row["year"]))
    for record in index.values():
        record["albums"] = record["albums"][:3]
        record["years"] = [min(record["years"]), max(record["years"])] if record["years"] else []
        record["n"] = len(record["tracks"])
        record["n_needs"] = len(record["needs"])
    return index


def fill_candidates(index: Dict[str, Dict[str, Any]], *, min_tracks: int = 1) -> List[Dict[str, Any]]:
    """Artists with at least one track that has no specific style."""
    picked = [record for record in index.values()
              if record["n_needs"] and record["n"] >= min_tracks and not record["labelish"]]
    picked.sort(key=lambda record: (-record["n_needs"], record["name"].lower()))
    return picked


def audit_candidates(index: Dict[str, Dict[str, Any]], *, min_tracks: int = 5) -> List[Dict[str, Any]]:
    """Artists that already have styles: a second opinion on the tag-derived labels."""
    picked = [record for record in index.values()
              if record["n"] >= min_tracks and record["styles"] and not record["labelish"]]
    picked.sort(key=lambda record: (-record["n"], record["name"].lower()))
    return picked


def system_prompt(vocabulary: Sequence[str]) -> str:
    """Fixed instructions + the closed vocabulary (stable => prefix cache hits)."""
    return (
        "You classify music artists into styles.\n"
        "Answer ONLY with names taken verbatim from the VOCABULARY below.\n"
        "Never invent a style, never translate, never add anything else.\n"
        "If you do not know the artist, return an empty list and confidence 0.1.\n"
        "At most 3 styles per artist, ordered from the most defining to the least.\n"
        "Answer with a single JSON object, no prose, in this exact shape:\n"
        '{"artists": [{"name": "<artist as given>", "styles": ["<vocabulary name>"],\n'
        '              "confidence": 0.0}]}\n'
        "VOCABULARY:\n" + "\n".join(vocabulary)
    )


def user_prompt(batch: Sequence[Dict[str, Any]]) -> str:
    """One batch of artists, with the context the library already has."""
    payload = []
    for record in batch:
        entry: Dict[str, Any] = {"name": record["name"], "tracks_owned": record["n"]}
        if record["albums"]:
            entry["albums"] = record["albums"]
        if record["years"]:
            entry["years"] = record["years"]
        if record["tags"]:
            entry["existing_file_tags"] = [tag for tag, _count in record["tags"].most_common(3)]
        payload.append(entry)
    return ("Classify these artists. The context comes from the music files themselves.\n"
            + json.dumps({"artists_to_classify": payload}, ensure_ascii=False))


# --------------------------------------------------------------------------- #
# answers: validate, cache, write
# --------------------------------------------------------------------------- #

def style_lookup(connection) -> Dict[str, str]:
    """Closed-vocabulary check: normalised style name *or alias* -> style id.

    Names are what the prompt asks for; the aliases of the vocabulary are
    accepted as well, because they are our own curated spellings ("Trash Metal"
    is listed as an alias of Thrash Metal). Anything else is refused, so a model
    can never introduce a style of its own invention.
    """
    from step3_vocabulary import normalize_raw
    lookup: Dict[str, str] = {}
    for row in connection.execute(
            "SELECT id, name, aliases FROM styles WHERE name NOT LIKE '%(unspecified)'"):
        lookup[normalize_raw(row["name"])] = row["id"]
        for alias in json.loads(row["aliases"] or "[]"):
            key = normalize_raw(alias)
            if key:
                lookup.setdefault(key, row["id"])
    return lookup


def validate_answers(parsed: Dict[str, Any], batch: Sequence[Dict[str, Any]],
                     lookup: Dict[str, str], max_styles: int = 3) -> Dict[str, Dict[str, Any]]:
    """Keep only vocabulary names, and only answers about artists we asked about."""
    from step3_vocabulary import normalize_raw
    by_norm = {S.norm_key(record["name"]): record["key"] for record in batch}
    answers: Dict[str, Dict[str, Any]] = {}
    entries = parsed.get("artists") if isinstance(parsed, dict) else None
    if entries is None and isinstance(parsed, dict):
        # Some models answer with {artist: {...}} instead of {"artists": [...]}.
        entries = [{"name": name, **(value if isinstance(value, dict) else {"styles": value})}
                   for name, value in parsed.items()]
    for entry in entries or []:
        if not isinstance(entry, dict):
            continue
        name = str(entry.get("name") or entry.get("artist") or "").strip()
        key = by_norm.get(S.norm_key(name))
        if key is None:
            continue
        styles = entry.get("styles") or entry.get("style") or []
        if isinstance(styles, str):
            styles = [styles]
        ids: List[str] = []
        names: List[str] = []
        for style in styles:
            style_id = lookup.get(normalize_raw(str(style)))
            if style_id and style_id not in ids:
                ids.append(style_id)
                names.append(str(style).strip())
            if len(ids) >= max_styles:
                break
        try:
            confidence = float(entry.get("confidence") or 0.0)
        except (TypeError, ValueError):
            confidence = 0.0
        answers[key] = {
            "styles": ids, "names": names,
            "confidence": max(0.0, min(1.0, confidence)),
            "basis": str(entry.get("basis") or entry.get("reason") or "")[:160],
        }
    return answers


def cached_answers(connection, fingerprint: str) -> Dict[str, Dict[str, Any]]:
    """Answers from an earlier identical run (so a re-run costs nothing)."""
    cache: Dict[str, Dict[str, Any]] = {}
    for row in connection.execute(
            "SELECT key, llm_json FROM artists WHERE llm_prompt = ? AND llm_json IS NOT NULL",
            (fingerprint,)):
        try:
            cache[row["key"]] = json.loads(row["llm_json"])
        except ValueError:
            continue
    return cache


def save_artist_answer(connection, record: Dict[str, Any], answer: Dict[str, Any],
                       fingerprint: str) -> None:
    """Remember the verdict (and its provenance) on the artist row."""
    connection.execute(
        "INSERT INTO artists(key, name, tracks, albums, styles, family, label_source,"
        " confidence, llm_json, llm_prompt, llm_at, updated_at)"
        " VALUES(?,?,?,?,?,?,?,?,?,?,?,?)"
        " ON CONFLICT(key) DO UPDATE SET name=excluded.name, tracks=excluded.tracks,"
        " albums=excluded.albums, styles=excluded.styles, family=excluded.family,"
        " label_source=excluded.label_source, confidence=excluded.confidence,"
        " llm_json=excluded.llm_json, llm_prompt=excluded.llm_prompt,"
        " llm_at=excluded.llm_at, updated_at=excluded.updated_at",
        (record["key"], record["name"], record["n"], len(record["albums"]),
         json.dumps(answer.get("styles") or []), None, "llm", answer.get("confidence"),
         json.dumps(answer, ensure_ascii=False), fingerprint, S.now(), S.now()))


def log_api_call(connection, client: "DeepSeek", model: str, purpose: str, cache_key: str,
                 batch_size: int, result: Optional[Dict[str, Any]] = None,
                 detail: str = "") -> None:
    """One row per request: what it cost, in tokens and in money."""
    usage = (result or {}).get("usage") or {}
    connection.execute(
        "INSERT INTO api_calls(ts, provider, model, purpose, cache_key, batch_size,"
        " tokens_in, tokens_out, cached_in, cost_usd, ok, detail)"
        " VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
        (S.now(), "deepseek", model, purpose, cache_key, batch_size,
         int(usage.get("prompt_tokens") or 0), int(usage.get("completion_tokens") or 0),
         int(usage.get("prompt_cache_hit_tokens") or 0),
         float((result or {}).get("cost") or 0.0), 1 if result else 0, detail[:300]))


def ask_artist_batches(client: "DeepSeek", connection, config: Dict[str, Any],
                       vocabulary: Sequence[str], records: Sequence[Dict[str, Any]], *,
                       fingerprint: str, batch_size: int, workers: int, purpose: str,
                       dry_run: bool = False, force: bool = False, show: bool = False,
                       max_tokens: int = 0) -> Dict[str, Dict[str, Any]]:
    """Ask about every artist that is not cached yet; returns key -> answer.

    The requests run in parallel, but *only the main thread writes to SQLite*:
    worker threads just hand their answer back (SQLite connections are not
    shareable, and a lock does not change that).
    """
    lookup = style_lookup(connection)
    system = system_prompt(vocabulary)
    answers = {} if force else cached_answers(connection, fingerprint)
    todo = [record for record in records if record["key"] not in answers]
    if answers:
        S.log(f"already answered and cached: {S.human(len(answers))} artists (free)")
    if not todo:
        S.log("nothing new to ask - use --force to ask again")
        return answers
    if dry_run:
        S.log(f"dry run: {S.human(len(todo))} artists would be asked, nothing was sent")
        return answers
    batches = [todo[start:start + batch_size] for start in range(0, len(todo), batch_size)]
    S.log(f"asking {client.model} about {S.human(len(todo))} artists"
          f" in {S.human(len(batches))} requests")

    lock = threading.Lock()
    progress = S.Progress(len(batches), label="asking ")

    def work(batch: List[Dict[str, Any]]):
        """Runs in a worker thread: no database access at all."""
        budget = max_tokens or min(8000, 400 + 140 * len(batch))
        try:
            result = client.chat([{"role": "system", "content": system},
                                  {"role": "user", "content": user_prompt(batch)}],
                                 max_tokens=budget)
        except Exception as exc:               # one bad batch must never stop the run
            return batch, {}, None, str(exc)
        parsed = parse_json_block(result.get("content", ""))
        return batch, validate_answers(parsed, batch, lookup), result, ""

    with ThreadPoolExecutor(max_workers=max(1, workers)) as pool:
        futures = [pool.submit(work, batch) for batch in batches]
        for future in as_completed(futures):
            batch, found, result, error = future.result()
            progress.step()
            if error:
                S.log("request failed:", error)
            with lock:
                if result is not None:
                    log_api_call(connection, client, result.get("model") or client.model,
                                 purpose, fingerprint, len(batch), result)
                for record in batch:
                    answer = found.get(record["key"]) or {
                        "styles": [], "names": [], "confidence": 0.0, "basis": "no answer"}
                    answers[record["key"]] = answer
                    save_artist_answer(connection, record, answer, fingerprint)
                connection.commit()
            if show:
                for record in batch:
                    answer = answers.get(record["key"]) or {}
                    names = ", ".join(answer.get("names") or []) or "(unknown)"
                    print(f"      {record['name'][:36]:36s} {record['n_needs']:>4} to fill ->  "
                          f"{names}   [{answer.get('confidence', 0):.2f}]")
    progress.finish()
    return answers


def apply_answers(connection, index: Dict[str, Dict[str, Any]],
                  answers: Dict[str, Dict[str, Any]], *, dry_run: bool = False,
                  min_confidence: float = 0.0) -> Dict[str, Any]:
    """Write the LLM labels onto the tracks that had no specific style."""
    written = labels = skipped = 0
    pending: Dict[str, List[S.StyleEntry]] = {}
    for key, answer in answers.items():
        record = index.get(key)
        if record is None or not record["needs"]:
            continue
        ids = answer.get("styles") or []
        confidence = float(answer.get("confidence") or 0.0)
        if not ids or confidence < min_confidence:
            skipped += len(record["needs"])
            continue
        entries: List[S.StyleEntry] = []
        names = answer.get("names") or []
        for position, style_id in enumerate(ids):
            label = names[position] if position < len(names) else ""
            entries.append((style_id, 1.0 if position == 0 else 0.7, "llm", confidence,
                            f"deepseek:{label}"))
        for rel_path in record["needs"]:
            pending[rel_path] = entries
            written += 1
        labels += len(entries)
    if dry_run or not pending:
        return {"tracks": written, "labels": labels, "skipped": skipped}
    for start in range(0, len(pending), 800):
        chunk = {path: pending[path] for path in list(pending)[start:start + 800]}
        S.add_track_styles_bulk(connection, chunk)
    connection.commit()
    return {"tracks": written, "labels": labels, "skipped": skipped}


def price_of(config: Dict[str, Any], tokens_in: float, tokens_out: float,
             cached_in: float = 0.0) -> float:
    """Money for a token count, using the prices in config.json."""
    hit = float(S.setting(config, "llm.input_price_per_mtok_cache_hit", 0.006) or 0)
    miss = float(S.setting(config, "llm.input_price_per_mtok_cache_miss", 0.30) or 0.30)
    out = float(S.setting(config, "llm.output_price_per_mtok", 1.20) or 1.20)
    return (max(0.0, cached_in) / 1e6) * hit + \
           (max(0.0, tokens_in - cached_in) / 1e6) * miss + (max(0.0, tokens_out) / 1e6) * out


def project_cost(config: Dict[str, Any], vocabulary: Sequence[str], records: Sequence[Dict[str, Any]],
                 batch_size: int, fingerprint: str, cached: int = 0) -> Dict[str, Any]:
    """What the run would cost, before a single token is spent."""
    system = system_prompt(vocabulary)
    system_tokens = len(system) / CHARS_PER_TOKEN
    todo = [record for record in records]
    batches = [todo[start:start + batch_size] for start in range(0, len(todo), batch_size)]
    input_tokens = cached_tokens = output_tokens = 0.0
    for index, batch in enumerate(batches):
        input_tokens += len(user_prompt(batch)) / CHARS_PER_TOKEN
        input_tokens += system_tokens
        cached_tokens += system_tokens if index else 0.0     # prefix caching from batch 2 on
        output_tokens += 60.0 * len(batch)                   # ~3 styles + name + confidence
    total_in = input_tokens + system_tokens * 0        # already counted per batch
    return {
        "artists": len(todo), "batches": len(batches), "system_tokens": int(system_tokens),
        "input_tokens": int(total_in), "cached_tokens": int(cached_tokens),
        "output_tokens": int(output_tokens),
        "cost": price_of(config, total_in, output_tokens, cached_tokens),
    }


def cmd_estimate(config: Dict[str, Any], connection, args: argparse.Namespace) -> int:
    """No key, no network: exactly what the run would look like."""
    vocabulary = vocabulary_names(connection)
    fingerprint = prompt_fingerprint(config, vocabulary)
    records = list(fill_candidates(build_artist_index(connection),
                                   min_tracks=args.min_tracks))
    if args.audit:
        records = audit_candidates(build_artist_index(connection), min_tracks=args.min_tracks)
    cached = len(cached_answers(connection, fingerprint))
    if args.limit:
        records = records[:args.limit]
    known = cached_answers(connection, fingerprint)
    pending = [record for record in records if record["key"] not in known]
    free_upgrade = [record for record in records
                    if record["key"] in known and (known[record["key"]].get("styles") or [])]
    unknown = [record for record in records
               if record["key"] in known and not (known[record["key"]].get("styles") or [])]
    projection = project_cost(config, vocabulary, pending, args.batch_size, fingerprint, 0)
    S.log(f"vocabulary          {S.human(len(vocabulary))} styles"
          f" ({projection['system_tokens']} tokens of instructions)")
    S.log(f"artists to ask      {S.human(projection['artists'])}"
          f"  (covering {S.human(sum(record['n_needs'] for record in pending))} tracks)")
    if free_upgrade:
        S.log(f"already answered and reusable for free: {S.human(len(free_upgrade))} artists,"
              f" {S.human(sum(record['n_needs'] for record in free_upgrade))} tracks"
              " - their cached styles can be applied without paying again")
    if unknown:
        S.log(f"answered 'unknown' already: {S.human(len(unknown))} artists,"
              f" {S.human(sum(record['n_needs'] for record in unknown))} tracks"
              " - re-asking costs money and changes nothing (use --force to try)")
    S.log(f"requests            {S.human(projection['batches'])}"
          f"  ({args.batch_size} artists each)")
    S.log(f"tokens (estimated)  in {S.human(projection['input_tokens'])}"
          f" (cached {S.human(projection['cached_tokens'])}),"
          f" out {S.human(projection['output_tokens'])}")
    S.log(f"projected cost      ${projection['cost']:.4f}   model {args.model or 'from config'}")
    spent = connection.execute(
        "SELECT COALESCE(SUM(cost_usd), 0) AS c, COUNT(*) AS n FROM api_calls").fetchone()
    if spent["n"]:
        S.log(f"already spent on the LLM: ${spent['c']:.4f} over {S.human(spent['n'])} requests")
    S.log("run it with --probe (check the key), --sample 20 (review), then --run")
    return 0


def diagnose_answer(parsed: Dict[str, Any], result: Dict[str, Any]) -> str:
    """Say *why* an answer produced no usable styles - no guessing needed."""
    content = str(result.get("content") or "")
    if not content:
        if result.get("reasoning_tokens"):
            return (f"the model spent all {result['reasoning_tokens']} output tokens on"
                    " reasoning and never wrote JSON - raise --max-tokens")
        return "the model returned an empty answer"
    if str(result.get("finish_reason") or "") == "length":
        return "the answer was cut off at the token limit - raise --max-tokens"
    if not parsed:
        return "the answer was not valid JSON (see the raw answer above)"
    return ("the JSON was fine, but no style name matched the vocabulary"
            " (see the raw answer above)")


def cmd_probe(config: Dict[str, Any], connection, args: argparse.Namespace) -> int:
    """One tiny request: is the key valid, and does the model answer in vocabulary?"""
    client = DeepSeek(config, model=args.model, debug=True)
    vocabulary = vocabulary_names(connection)
    lookup = style_lookup(connection)
    sample = [{"key": "kamelot", "name": "Kamelot", "n": 3, "n_needs": 1,
               "albums": ["The Awakening"], "years": [2015, 2023],
               "tags": Counter({"Power Metal": 1})},
              {"key": "vulvodynia", "name": "Vulvodynia", "n": 4, "n_needs": 4,
               "albums": ["Cognizant Castigation"], "years": [2016, 2021], "tags": Counter()}]
    S.log(f"probing {client.model} at {client.base}")
    result = client.chat([{"role": "system", "content": system_prompt(vocabulary)},
                          {"role": "user", "content": user_prompt(sample)}],
                         max_tokens=args.max_tokens or 2000)
    answers = validate_answers(parse_json_block(result["content"]), sample, lookup)
    for key, answer in answers.items():
        names = ", ".join(answer["names"]) or "(nothing recognised)"
        print(f"      {key:16s} -> {names}  [{answer['confidence']:.2f}]")
    S.log(f"the key works: {client.calls} request,"
          f" in {S.human(result['tokens_in'])} tokens (cached {S.human(result['cached_in'])}),"
          f" out {S.human(result['tokens_out'])}"
          f" (reasoning: {S.human(result.get('reasoning_tokens') or 0)}),"
          f" finish={result.get('finish_reason') or '?'}, cost ${result['cost']:.5f}")
    if result.get("dropped"):
        S.log("the account rejected these optional fields, dropped:",
              ", ".join(result["dropped"]))
    log_api_call(connection, client, result.get("model") or client.model, "probe",
                 prompt_fingerprint(config, vocabulary), len(sample), result)
    connection.commit()
    if not answers:
        print()
        print("      raw answer from the model (first 600 characters):")
        print("      " + (result.get("content") or "(empty)")[:600].replace("\n", "\n      "))
        print()
        S.log("VERDICT:", diagnose_answer(parse_json_block(result["content"]), result))
        return 2
    return 0


def cmd_sample(config: Dict[str, Any], connection, args: argparse.Namespace) -> int:
    """Put the model's verdict next to what your files already say. Nothing is written
    onto the tracks - only the answer cache, so paying twice is impossible."""
    vocabulary = vocabulary_names(connection)
    fingerprint = prompt_fingerprint(config, vocabulary)
    index = build_artist_index(connection)
    records = fill_candidates(index, min_tracks=args.min_tracks)[:max(1, args.limit)]
    S.log(f"sample of {S.human(len(records))} artists from"
          f" {S.human(len(fill_candidates(index, min_tracks=args.min_tracks)))} candidates")
    client = DeepSeek(config, model=args.model, debug=args.debug)
    answers = ask_artist_batches(client, connection, config, vocabulary, records,
                                 fingerprint=fingerprint,
                                 batch_size=min(args.batch_size, len(records)),
                                 workers=min(args.workers, 2), purpose="sample",
                                 force=args.force, max_tokens=args.max_tokens)
    print()
    print(f"      {'artist':32s} {'fills':>5}  {'your tags':34s} {'model says'}")
    print(f"      {'-'*32} {'-'*5}  {'-'*34} {'-'*38}")
    for record in records:
        answer = answers.get(record["key"]) or {}
        tags = "; ".join(tag for tag, _count in record["tags"].most_common(2)) or "(none)"
        current = ", ".join(style for style, _count in record["styles"].most_common(2))
        says = ", ".join(answer.get("names") or []) or "(unknown)"
        mark = " " if not current or any(
            style in (answer.get("styles") or []) for style in record["styles"]) else "!"
        print(f"    {mark} {record['name'][:32]:32s} {record['n_needs']:>5}  {tags[:34]:34s} {says}")
        if current:
            print(f"      {'':32s} {'':>5}  {'(already: ' + current + ')':34s}"
                  f"  confidence {answer.get('confidence', 0):.2f}")
    print()
    S.log("'!' marks where the model disagrees with a style that comes from your tags")
    S.log(f"spent so far: {client.calls} requests, ${client.cost:.4f}"
          f" (in {S.human(client.tokens_in)}, out {S.human(client.tokens_out)} tokens)")
    S.log("if this looks right, run:  python3 step5_llm.py --run"
          "   (the sample is cached, so it costs nothing again)")
    return 0


def cmd_run(config: Dict[str, Any], connection, args: argparse.Namespace) -> int:
    """The real fill: only the tracks that have no specific style, only gaps."""
    vocabulary = vocabulary_names(connection)
    fingerprint = prompt_fingerprint(config, vocabulary)
    index = build_artist_index(connection)
    records = fill_candidates(index, min_tracks=args.min_tracks)
    if args.limit:
        records = records[:args.limit]
    if not records:
        S.log("nothing to fill - every artist already has a style")
        return 0
    cached = cached_answers(connection, fingerprint)
    pending = [record for record in records if record["key"] not in cached]
    projection = project_cost(config, vocabulary, pending, args.batch_size, fingerprint)
    budget = args.max_cost if args.max_cost else float(
        S.setting(config, "llm.max_cost_usd", 3.0) or 3.0)
    S.log(f"{S.human(len(records))} candidate artists,"
          f" {S.human(len(records) - len(pending))} already answered (free),"
          f" {S.human(len(pending))} to ask")
    S.log(f"projected cost ${projection['cost']:.4f} (budget ${budget:.2f})")
    if projection["cost"] > budget:
        S.log("REFUSING to run: the projection is over the budget."
              " Raise --max-cost or the llm.max_cost_usd setting")
        return 2
    client = DeepSeek(config, model=args.model, debug=args.debug)
    answers = ask_artist_batches(client, connection, config, vocabulary, records,
                                 fingerprint=fingerprint, batch_size=args.batch_size,
                                 workers=args.workers, purpose="fill", force=args.force,
                                 dry_run=args.dry_run, max_tokens=args.max_tokens)
    result = apply_answers(connection, index, answers, dry_run=args.dry_run,
                           min_confidence=args.min_confidence)
    if not args.dry_run:
        S.refresh_style_counts(connection)
        S.refresh_track_primary(connection)
        S.rebuild_artist_rollup(connection)
        S.meta_set(connection, "last_llm", S.now())
        connection.commit()
    spent = connection.execute(
        "SELECT COALESCE(SUM(cost_usd), 0) AS c, COUNT(*) AS n FROM api_calls").fetchone()
    print()
    S.log(f"tracks that gained a style: {S.human(result['tracks'])}"
          f"  ({S.human(result['skipped'])} left alone: unknown or too unsure)")
    S.log(f"labels written             {S.human(result['labels'])}")
    S.log(f"this run cost ${client.cost:.4f} ({client.calls} requests);"
          f" lifetime ${spent['c']:.4f} over {S.human(spent['n'])} requests")
    if not args.dry_run:
        S.print_status(connection)
    return 0


def cmd_audit(config: Dict[str, Any], connection, args: argparse.Namespace) -> int:
    """Second opinion on artists that already have styles: where do we disagree?"""
    vocabulary = vocabulary_names(connection)
    fingerprint = prompt_fingerprint(config, vocabulary)
    index = build_artist_index(connection)
    records = audit_candidates(index, min_tracks=args.min_tracks)
    if args.limit:
        records = records[:args.limit]
    if not records:
        S.log("nothing to audit yet")
        return 0
    S.log(f"auditing {S.human(len(records))} artists that already have styles"
          f" ({S.human(sum(record['n'] for record in records))} tracks)")
    client = DeepSeek(config, model=args.model, debug=args.debug)
    answers = ask_artist_batches(client, connection, config, vocabulary, records,
                                 fingerprint=fingerprint, batch_size=args.batch_size,
                                 workers=args.workers, purpose="audit", force=args.force,
                                 dry_run=args.dry_run, max_tokens=args.max_tokens)
    agree = partial = 0
    disputes: List[Dict[str, Any]] = []
    style_names = {row["id"]: row["name"] for row in connection.execute("SELECT id, name FROM styles")}
    for record in records:
        answer = answers.get(record["key"]) or {}
        said = answer.get("styles") or []
        if not said:
            continue
        mine = [style for style, _count in record["styles"].most_common(3)]
        if mine and mine[0] in said:
            agree += 1
        elif set(mine) & set(said):
            partial += 1
        else:
            disputes.append({
                "artist": record["name"], "tracks": record["n"],
                "tags_say": ", ".join(style_names.get(style, style) for style in mine),
                "llm_says": ", ".join(answer.get("names") or []),
                "confidence": round(float(answer.get("confidence") or 0.0), 2),
                "file_tags": "; ".join(tag for tag, _count in record["tags"].most_common(2)),
            })
    total = max(1, len(records))
    S.log(f"the model agrees with the tags on {agree / total * 100:.1f} % of the artists,"
          f" partially on {partial / total * 100:.1f} %,"
          f" disagrees on {len(disputes) / total * 100:.1f} %")
    listing = S.out_dir(config) / "llm_disagreements.csv"
    with listing.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.writer(handle)
        writer.writerow(["artist", "tracks", "tags_say", "llm_says", "confidence", "file_tags"])
        for dispute in disputes:
            writer.writerow([dispute["artist"], dispute["tracks"], dispute["tags_say"],
                             dispute["llm_says"], dispute["confidence"], dispute["file_tags"]])
    if disputes:
        S.log("disagreements written to", str(listing))
        for dispute in disputes[:15]:
            print(f"      {dispute['artist'][:30]:30s} tags: {dispute['tags_say'][:26]:26s}"
                  f" model: {dispute['llm_says'][:30]}")
    S.log(f"audit cost ${client.cost:.4f} ({client.calls} requests);"
          " nothing was written onto the tracks")
    return 0


def parse_args(argv: Optional[Sequence[str]] = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Step 5 - DeepSeek fills and audits the styles",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--db", default="", help="dataset path (default: config.json)")
    parser.add_argument("--model", default="", help="override llm.model (e.g. deepseek-v4-pro)")
    parser.add_argument("--estimate", action="store_true",
                        help="no key, no network: what the run would cost")
    parser.add_argument("--probe", action="store_true", help="one tiny request: is the key ok?")
    parser.add_argument("--sample", type=int, default=0, metavar="N",
                        help="ask about N artists and show the verdicts (no writes)")
    parser.add_argument("--run", action="store_true", help="the real fill")
    parser.add_argument("--audit", action="store_true",
                        help="second opinion on the artists that already have styles")
    parser.add_argument("--limit", type=int, default=0, help="only the first N artists")
    parser.add_argument("--min-tracks", type=int, default=1,
                        help="ignore artists with fewer tracks (audit uses 5)")
    parser.add_argument("--batch-size", type=int, default=0,
                        help="artists per request (default 25)")
    parser.add_argument("--max-tokens", type=int, default=0,
                        help="output token ceiling per request (default: scaled to the batch)")
    parser.add_argument("--workers", type=int, default=0, help="parallel requests (default 4)")
    parser.add_argument("--min-confidence", type=float, default=0.0,
                        help="ignore answers below this confidence")
    parser.add_argument("--max-cost", type=float, default=0.0,
                        help="refuse to run if the projection is above this (USD)")
    parser.add_argument("--force", action="store_true", help="ask again even if cached")
    parser.add_argument("--dry-run", action="store_true", help="ask nothing, write nothing")
    parser.add_argument("--debug", action="store_true", help="log every request and its cost")
    return parser.parse_args(list(argv) if argv is not None else None)


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = parse_args(argv)
    load_env_file(S.STYLES_DIR / ".env")
    config = S.load_config()
    if not args.batch_size:
        args.batch_size = int(S.setting(config, "llm.batch_size", 25) or 25)
    args.batch_size = max(1, min(int(args.batch_size), 60))
    if not args.workers:
        args.workers = int(S.setting(config, "llm.workers", 4) or 4)
    if args.audit and args.min_tracks <= 1:
        args.min_tracks = 5

    path = S.resolve(args.db) if args.db else S.db_path(config)
    connection = S.connect(path, create=False)
    try:
        if args.estimate:
            return cmd_estimate(config, connection, args)
        if args.probe:
            return cmd_probe(config, connection, args)
        if args.sample:
            args.limit = args.sample
            return cmd_sample(config, connection, args)
        if args.audit:
            return cmd_audit(config, connection, args)
        if args.run:
            return cmd_run(config, connection, args)
        S.log("nothing to do - use --estimate, --probe, --sample 20, --run or --audit")
        return 1
    finally:
        connection.close()


if __name__ == "__main__":
    raise SystemExit(main())
