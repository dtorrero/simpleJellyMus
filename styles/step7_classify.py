#!/usr/bin/env python3
"""Step 7 - the acoustic classifier: does the audio agree with the tags?

This is the only *objective* source in the pipeline. It listens to a short
excerpt of a track and asks a pre-trained model what it sounds like, so it can
both fill the gaps and audit everything the other layers claim.

It never modifies a music file, and it only writes into the dataset when you ask
it to (``--fill``). Everything else is a read-only measurement.

    python3 step7_classify.py --setup         # create the venv + download models (once)
    python3 step7_classify.py --check          # is the environment ready?
    python3 step7_classify.py --sanity         # 20 tracks, print what the model hears
    python3 step7_classify.py --calibration    # measure accuracy against clear tags
    python3 step7_classify.py --fill           # label the tracks nothing else could
    python3 step7_classify.py --audit          # sample the library, list disagreements

The heavy lifting happens in a *separate* interpreter (``--worker``), configured
by ``classifier.python`` in config.json, so the player and the other steps keep
their "standard library only" promise.

Models (Essentia / MTG-UPF, CC-BY-NC-SA, downloaded into ``models/``):

    discogs-effnet-bs64-1.pb                 18 MB   audio -> embeddings (discogs-effnet tier)
    genre_discogs400-discogs-effnet-1.pb    2.1 MB   400 Discogs styles
    discogs-maest-30s-pw-2.pb               347 MB   accurate tier (optional)
"""

from __future__ import annotations

import argparse
import csv
import json
import os
import random
import shutil
import subprocess
import sys
import time
import urllib.request
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

sys.path.insert(0, str(Path(__file__).resolve().parent))

import styles_db as S  # noqa: E402
from step3_vocabulary import load_rules, load_vocabulary, normalize_raw  # noqa: E402
from step4_normalize import Normalizer  # noqa: E402

EFFNET_BASE = "https://essentia.upf.edu/models"
TIERS: Dict[str, Dict[str, List[str]]] = {
    "effnet": {
        "files": [
            f"{EFFNET_BASE}/feature-extractors/discogs-effnet/discogs-effnet-bs64-1.pb",
            f"{EFFNET_BASE}/classification-heads/genre_discogs400/genre_discogs400-discogs-effnet-1.pb",
            f"{EFFNET_BASE}/classification-heads/genre_discogs400/genre_discogs400-discogs-effnet-1.json",
        ],
    },
    "maest": {
        "files": [
            f"{EFFNET_BASE}/feature-extractors/maest/discogs-maest-30s-pw-2.pb",
            f"{EFFNET_BASE}/classification-heads/genre_discogs400/genre_discogs400-discogs-maest-30s-pw-1.pb",
            f"{EFFNET_BASE}/classification-heads/genre_discogs400/genre_discogs400-discogs-maest-30s-pw-1.json",
        ],
    },
}
VENV_DIRNAME = ".venv-classify"
BASE_PYTHON = "python3.11"          # essentia-tensorflow ships stable wheels for 3.11


def models_dir(config: Dict[str, Any]) -> Path:
    path = S.resolve(config.get("models_dir") or "models")
    path.mkdir(parents=True, exist_ok=True)
    return path


def venv_python(config: Dict[str, Any]) -> Path:
    return models_dir(config) / VENV_DIRNAME / "bin" / "python"


def interpreter(config: Dict[str, Any]) -> Path:
    """The interpreter that has essentia-tensorflow (venv first, then config)."""
    candidate = venv_python(config)
    if candidate.exists():
        return candidate
    configured = str(S.setting(config, "classifier.python", "python3") or "python3")
    return Path(configured)


# --------------------------------------------------------------------------- #
# setup: venv + models
# --------------------------------------------------------------------------- #

def _download(url: str, target: Path, *, label: str = "") -> Path:
    """Download *url* to *target* (skipped when it is already there)."""
    if target.exists() and target.stat().st_size > 0:
        S.log(f"already there: {target.name}")
        return target
    target.parent.mkdir(parents=True, exist_ok=True)
    part = target.with_name(target.name + ".part")
    S.log(f"downloading {label or target.name} …")
    try:
        with urllib.request.urlopen(url, timeout=120) as response, part.open("wb") as handle:
            total = int(response.headers.get("Content-Length") or 0)
            done = 0
            last = 0.0
            while True:
                chunk = response.read(1 << 20)
                if not chunk:
                    break
                handle.write(chunk)
                done += len(chunk)
                moment = time.time()
                if moment - last > 1.0 and total:
                    sys.stdout.write(f"\r  {done/1e6:7.1f} / {total/1e6:7.1f} MB"
                                     f" ({100.0*done/total:5.1f} %)   ")
                    sys.stdout.flush()
                    last = moment
    except Exception as exc:
        part.unlink(missing_ok=True)
        raise SystemExit(f"download failed: {exc}")
    sys.stdout.write("\n")
    part.replace(target)
    return target


def setup(config: Dict[str, Any], tier: str) -> int:
    """Create the classification venv and fetch the models (run once)."""
    directory = models_dir(config)
    S.log("models directory:", str(directory))

    target_python = venv_python(config)
    if not target_python.exists():
        base = shutil.which(BASE_PYTHON) or shutil.which("python3")
        if not base:
            raise SystemExit("no python3 interpreter found")
        S.log(f"creating the classification venv with {base}")
        S.log("this downloads about 700 MB (essentia-tensorflow + tensorflow) - be patient")
        subprocess.run([base, "-m", "venv", str(directory / VENV_DIRNAME)], check=True)
        subprocess.run([str(target_python), "-m", "pip", "install", "--upgrade", "pip"],
                       check=False, stdout=subprocess.DEVNULL)
        result = subprocess.run(
            [str(target_python), "-m", "pip", "install", "essentia-tensorflow", "numpy"],
            check=False)
        if result.returncode != 0:
            raise SystemExit("pip install essentia-tensorflow failed - see the output above")
    else:
        S.log("venv already present:", str(target_python))

    for url in TIERS[tier]["files"]:
        _download(url, directory / url.split("/")[-1], label=url.split("/")[-1])
    S.log(f"tier {tier!r} is ready in", str(directory))
    S.log("now run:  python3 step7_classify.py --check")
    return 0


# --------------------------------------------------------------------------- #
# the worker: runs under the classification venv, one process per CPU slot
# --------------------------------------------------------------------------- #

WORKER_SOURCE = r'''
import json, os, subprocess, sys, tempfile
os.environ.setdefault("OMP_NUM_THREADS", "2")
os.environ.setdefault("TF_CPP_MIN_LOG_LEVEL", "3")

import numpy as np
import essentia.standard as es


def decode(path, seconds, rate):
    """First *seconds* of *path* as mono float32 PCM, decoded with ffmpeg."""
    command = ["ffmpeg", "-v", "error", "-i", path, "-t", str(seconds),
               "-ac", "1", "-ar", str(rate), "-f", "wav", "-"]
    completed = subprocess.run(command, capture_output=True, timeout=180)
    if completed.returncode != 0 or len(completed.stdout) < 1024:
        raise RuntimeError("ffmpeg could not decode the file")
    handle = tempfile.NamedTemporaryFile(suffix=".wav", delete=False)
    try:
        handle.write(completed.stdout)
        handle.close()
        return es.MonoLoader(filename=handle.name, sampleRate=rate)()
    finally:
        try:
            os.unlink(handle.name)
        except OSError:
            pass


def load_labels(path):
    data = json.load(open(path))
    if isinstance(data, dict):
        for key in ("classes", "labels", "genres"):
            if key in data:
                return [str(value) for value in data[key]]
    if isinstance(data, list):
        return [str(value) for value in data]
    raise RuntimeError("no labels in the model json")


def build(models, tier):
    if tier == "effnet":
        extractor = es.TensorflowPredictEffnetDiscogs(graphFilename=models["extractor"],
                                                      output="PartitionedCall:1")
    else:
        extractor = es.TensorflowPredictMAEST(graphFilename=models["extractor"],
                                              output="PartitionedCall:4")
    errors = []
    for input_name in ("serving_default_model_Placeholder", "model/Placeholder",
                       "serving_default_input_1:0", "input_1"):
        for output_name in ("PartitionedCall:0", "StatefulPartitionedCall:0", "Identity:0"):
            try:
                head = es.TensorflowPredict2D(graphFilename=models["head"], input=input_name,
                                              output=output_name)
                return extractor, head, {"input": input_name, "output": output_name}
            except Exception as exc:
                errors.append(f"{input_name}/{output_name}: {str(exc)[:80]}")
    raise RuntimeError("could not wire the genre head: " + " | ".join(errors[-3:]))


def main():
    job = json.loads(sys.argv[1])
    models = job["models"]
    tier = job.get("tier", "effnet")
    seconds = float(job.get("seconds", 40))
    rate = int(job.get("rate", 16000))
    names = load_labels(models["labels"])
    extractor, head, wiring = build(models, tier)
    print(json.dumps({"event": "ready", "wiring": wiring, "labels": len(names)}), flush=True)

    with open(job["jobs"], "r", encoding="utf-8") as handle:
        paths = [line.rstrip("\n") for line in handle if line.strip()]
    with open(job["results"], "w", encoding="utf-8") as out:
        for index, path in enumerate(paths):
            try:
                embeddings = extractor(decode(path, seconds, rate))
                predictions = np.asarray(head(embeddings))
                scores = predictions.mean(axis=0) if predictions.ndim > 1 else predictions
                order = np.argsort(scores)[::-1]
                record = {
                    "path": path,
                    "ok": True,
                    "patches": int(predictions.shape[0]) if predictions.ndim > 1 else 1,
                    "top": [{"label": str(names[i]), "probability": float(scores[i])}
                            for i in order[:8]],
                }
            except Exception as exc:
                record = {"path": path, "ok": False, "error": str(exc)[:200]}
            out.write(json.dumps(record) + "\n")
            out.flush()
            print(json.dumps({"event": "progress", "done": index + 1, "total": len(paths)}),
                  flush=True)


main()
'''


# --------------------------------------------------------------------------- #
# running the workers
# --------------------------------------------------------------------------- #

def write_worker(config: Dict[str, Any]) -> Path:
    """Materialise the worker source so the venv interpreter can run it."""
    path = models_dir(config) / "classifier_worker.py"
    path.write_text(WORKER_SOURCE, encoding="utf-8")
    return path


def model_files(config: Dict[str, Any], tier: str) -> Dict[str, str]:
    directory = models_dir(config)
    names = [url.split("/")[-1] for url in TIERS[tier]["files"]]
    files = {"extractor": str(directory / names[0]), "head": str(directory / names[1]),
             "labels": str(directory / names[2])}
    missing = [name for name, path in files.items() if not Path(path).exists()]
    if missing:
        raise SystemExit(
            f"missing model files ({', '.join(missing)}) - run: "
            f"python3 step7_classify.py --setup --tier {tier}")
    return files


def _job(config: Dict[str, Any], tier: str, jobs_file: Path, results_file: Path) -> str:
    return json.dumps({
        "models": model_files(config, tier),
        "tier": tier,
        "seconds": float(S.setting(config, "classifier.feed_seconds", 40) or 40),
        "rate": int(S.setting(config, "classifier.sample_rate", 16000) or 16000),
        "jobs": str(jobs_file),
        "results": str(results_file),
    })


def probe(config: Dict[str, Any], tier: str) -> Dict[str, Any]:
    """Load the models once and report how they were wired (fails loudly)."""
    python = interpreter(config)
    worker = write_worker(config)
    jobs_file = S.out_dir(config) / "classify_probe_jobs.txt"
    results_file = S.out_dir(config) / "classify_probe_results.jsonl"
    jobs_file.write_text("", encoding="utf-8")
    results_file.unlink(missing_ok=True)
    S.log(f"probing the classifier with {python}")
    try:
        completed = subprocess.run(
            [str(python), str(worker), _job(config, tier, jobs_file, results_file)],
            capture_output=True, text=True, timeout=600)
    except subprocess.TimeoutExpired:
        raise SystemExit("the classifier did not start within 10 minutes")
    ready: Dict[str, Any] = {}
    for line in (completed.stdout or "").splitlines():
        try:
            payload = json.loads(line)
        except ValueError:
            continue
        if payload.get("event") == "ready":
            ready = payload
    if not ready:
        tail = (completed.stderr or completed.stdout or "").strip().splitlines()[-12:]
        raise SystemExit("the classifier could not start:\n      " + "\n      ".join(tail))
    S.log(f"classifier ready: {ready.get('labels')} labels,"
          f" head wired as {ready.get('wiring')}")
    return ready


def run_workers(config: Dict[str, Any], tier: str, files: Sequence[Path],
                *, workers: int = 4) -> Dict[str, Dict[str, Any]]:
    """Classify *files* with several worker processes; returns path -> record."""
    if not files:
        return {}
    python = interpreter(config)
    worker = write_worker(config)
    out = S.out_dir(config)
    workers = max(1, min(int(workers), len(files)))
    chunks: List[List[Path]] = [[] for _ in range(workers)]
    for index, path in enumerate(files):
        chunks[index % workers].append(path)

    processes: List[Tuple[subprocess.Popen, Path]] = []
    for index, chunk in enumerate(chunks):
        if not chunk:
            continue
        jobs_file = out / f"classify_jobs_{index}.txt"
        results_file = out / f"classify_results_{index}.jsonl"
        jobs_file.write_text("\n".join(str(path) for path in chunk) + "\n", encoding="utf-8")
        results_file.unlink(missing_ok=True)
        process = subprocess.Popen(
            [str(python), str(worker), _job(config, tier, jobs_file, results_file)],
            stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, text=True)
        processes.append((process, results_file))

    progress = S.Progress(len(files), label="classifying")
    finished: Dict[str, Dict[str, Any]] = {}
    try:
        while any(process.poll() is None for process, _ in processes):
            done = 0
            for _, results_file in processes:
                if results_file.exists():
                    done += sum(1 for _ in results_file.open("r", encoding="utf-8"))
            progress.step(max(0, done - progress.done))
            time.sleep(1.0)
        progress.step(max(0, len(files) - progress.done))
    except KeyboardInterrupt:
        for process, _ in processes:
            process.terminate()
        S.log("interrupted - partial results are kept")
    finally:
        progress.finish()

    for process, results_file in processes:
        if results_file.exists():
            for line in results_file.open("r", encoding="utf-8"):
                line = line.strip()
                if not line:
                    continue
                try:
                    record = json.loads(line)
                except ValueError:
                    continue
                finished[record.get("path", "")] = record
        if process.returncode not in (0, None) and process.stderr is not None:
            tail = (process.stderr.read() or "").strip().splitlines()[-3:]
            if tail:
                S.log("worker stderr:", " ".join(tail))
    failed = [path for path, record in finished.items() if not record.get("ok")]
    if failed:
        S.log(f"{S.human(len(failed))} files could not be classified")
    return finished


# --------------------------------------------------------------------------- #
# results cache and label mapping
# --------------------------------------------------------------------------- #

def cache_file(config: Dict[str, Any]) -> Path:
    return S.out_dir(config) / "classify_cache.jsonl"


def load_cache(config: Dict[str, Any]) -> Dict[str, Dict[str, Any]]:
    path = cache_file(config)
    records: Dict[str, Dict[str, Any]] = {}
    if not path.exists():
        return records
    for line in path.open("r", encoding="utf-8"):
        line = line.strip()
        if not line:
            continue
        try:
            record = json.loads(line)
        except ValueError:
            continue
        records[record.get("path", "")] = record
    return records


def append_cache(config: Dict[str, Any], records: Iterable[Dict[str, Any]]) -> int:
    written = 0
    with cache_file(config).open("a", encoding="utf-8") as handle:
        for record in records:
            if record.get("ok"):
                handle.write(json.dumps(record) + "\n")
                written += 1
    return written


def classify(config: Dict[str, Any], tier: str, files: Sequence[Path], *,
             workers: int, use_cache: bool = True) -> Dict[str, Dict[str, Any]]:
    """Classify files, reusing everything already in the cache."""
    cached = load_cache(config) if use_cache else {}
    todo = [path for path in files if str(path) not in cached]
    if cached:
        S.log(f"already classified: {S.human(len(files) - len(todo))}"
              f" of {S.human(len(files))} (cache)")
    if todo:
        fresh = run_workers(config, tier, todo, workers=workers)
        append_cache(config, fresh.values())
        cached.update(fresh)
    return {str(path): cached.get(str(path), {}) for path in files}


def resolve_label(normalizer: Normalizer, label: str) -> Optional[str]:
    """Which of our styles does a model label correspond to (or ``None``)."""
    key = normalize_raw(label)
    if not key:
        return None
    if key in normalizer.keys:
        return normalizer.keys[key]
    style_id, how, _confidence = normalizer.lookup(key)
    if style_id and not how.startswith("keyword"):
        return style_id
    return None


# --------------------------------------------------------------------------- #
# calibration: does the model agree with the tags we trust?
# --------------------------------------------------------------------------- #

def calibration_set(connection, *, limit: int, per_style: int, seed: int = 7,
                    random_pick: bool = False) -> List[Dict[str, Any]]:
    """Tracks whose tag is already a canonical style name (clean ground truth)."""
    rows = list(connection.execute(
        "SELECT rel_path, raw_genre, primary_style, family FROM tracks"
        " WHERE label_source = 'tag' AND primary_style IS NOT NULL"
        f"   AND NOT ({S.FAMILY_STYLE_SQL})"))
    by_style: Dict[str, List[Dict[str, Any]]] = defaultdict(list)
    for row in rows:
        by_style[row["primary_style"]].append({
            "path": row["rel_path"], "tag": row["raw_genre"],
            "style_id": row["primary_style"], "family": row["family"],
        })
    rng = random.Random(seed)
    if random_pick:
        pool = list(rows)
        rng.shuffle(pool)
        picked = [{"path": row["rel_path"], "tag": row["raw_genre"],
                   "style_id": row["primary_style"], "family": row["family"]}
                  for row in pool[:limit]]
    else:
        picked = []
        for style_id in sorted(by_style):
            items = by_style[style_id]
            rng.shuffle(items)
            picked.extend(items[:per_style])
        rng.shuffle(picked)
        picked = picked[:limit]
    return picked


def family_map(connection) -> Dict[str, str]:
    return {row["id"]: (row["family"] or "") for row in
            connection.execute("SELECT id, family FROM styles")}


def evaluate(normalizer: Normalizer, records: Dict[str, Dict[str, Any]],
             expected: Sequence[Dict[str, Any]], families: Dict[str, str]) -> Dict[str, Any]:
    """top-1 / top-3 / family accuracy of the model against the trusted tags."""
    counts: Counter = Counter()
    misses: List[Dict[str, Any]] = []
    label_hits: Counter = Counter()
    unmapped: Counter = Counter()
    for item in expected:
        record = records.get(item["path"]) or {}
        if not record or not record.get("ok"):
            counts["failed"] += 1
            continue
        counts["total"] += 1
        mapped: List[Tuple[str, str, float]] = []
        for entry in record.get("top") or []:
            style_id = resolve_label(normalizer, entry["label"])
            if style_id is None:
                unmapped[entry["label"]] += 1
                continue
            mapped.append((style_id, entry["label"], float(entry.get("probability") or 0.0)))
        want = item["style_id"]
        want_family = item["family"]
        if mapped:
            label_hits[mapped[0][1]] += 1
        if mapped and mapped[0][0] == want:
            counts["top1"] += 1
            counts["agreed"] += 1
            continue
        if any(style_id == want for style_id, _, _ in mapped[:3]):
            counts["top3"] += 1
            counts["agreed"] += 1
        if any(families.get(style_id) == want_family for style_id, _, _ in mapped[:3]):
            counts["family"] += 1
        else:
            counts["family_miss"] += 1
        misses.append({"path": item["path"], "tag": item["tag"], "style": want,
                       "heard": [(sid, label, round(prob, 3)) for sid, label, prob in mapped[:3]]})
    total = max(1, counts["total"])
    result = {
        "total": counts["total"],
        "failed": counts["failed"],
        "top1": counts["top1"] / total,
        "top3": counts["top3"] / total,
        "family": counts["family"] / total,
        "top3_count": counts["top3"],
        "top1_count": counts["top1"],
        "family_count": counts["family"],
        "misses": misses,
        "unmapped_labels": unmapped,
        "top_labels": label_hits,
    }
    return result


def print_metrics(result: Dict[str, Any], gate: Dict[str, float]) -> bool:
    """Print the accuracy table and return whether the gate was passed."""
    S.log(f"calibration set: {S.human(result['total'])} tracks"
          f" ({S.human(result['failed'])} could not be classified)")
    checks = [
        ("top-1 accuracy ", result["top1"], gate.get("top1", 0.45)),
        ("top-3 accuracy ", result["top3"], gate.get("top3", 0.70)),
        ("family accuracy", result["family"], gate.get("family", 0.85)),
    ]
    passed = True
    for label, value, threshold in checks:
        ok = value >= threshold
        passed = passed and ok
        print(f"      {label} {value * 100:5.1f} %   (gate {threshold * 100:.0f} %)"
              f"   {'PASS' if ok else 'FAIL'}")
    if result["unmapped_labels"]:
        worst = ", ".join(f"{label}({count})"
                          for label, count in result["unmapped_labels"].most_common(8))
        S.log("model labels with no home in the vocabulary:", worst)
    if result["misses"]:
        S.log("where it disagreed (tag -> what it heard):")
        for miss in result["misses"][:15]:
            heard = ", ".join(f"{label} {prob:.2f}" for _, label, prob in miss["heard"])
            print(f"      {miss['tag']!r}  ->  {heard or 'nothing mapped'}")
    return passed


# --------------------------------------------------------------------------- #
# commands
# --------------------------------------------------------------------------- #

def music_root(config: Dict[str, Any]) -> Path:
    return S.resolve(config.get("music_root"))


def cmd_check(config: Dict[str, Any], args: argparse.Namespace) -> int:
    S.log("interpreter:", str(interpreter(config)))
    directory = models_dir(config)
    for tier in (args.tier,):
        for url in TIERS[tier]["files"]:
            path = directory / url.split("/")[-1]
            state = f"{path.stat().st_size / 1e6:.1f} MB" if path.exists() else "MISSING"
            print(f"      {path.name:52s} {state}")
    cache = load_cache(config)
    S.log(f"cache: {S.human(len(cache))} tracks already classified")
    probe(config, args.tier)
    return 0


def cmd_sanity(config: Dict[str, Any], args: argparse.Namespace,
               connection, normalizer: Normalizer) -> int:
    """Twenty tracks: what does the model actually hear?"""
    items = calibration_set(connection, limit=args.limit, per_style=1, seed=args.seed)
    if not items:
        S.log("no tracks with a clean tag yet - run step4_normalize.py first")
        return 1
    root = music_root(config)
    files = [root / item["path"] for item in items]
    records = classify(config, args.tier, files, workers=args.workers)
    families = family_map(connection)
    print()
    for item in items:
        record = records.get(str(root / item["path"])) or {}
        tag = (item["tag"] or "?")[:42]
        if not record.get("ok"):
            print(f"      {tag:44s} FAILED: {record.get('error', 'no result')[:40]}")
            continue
        heard = []
        for entry in (record.get("top") or [])[:4]:
            style_id = resolve_label(normalizer, entry["label"])
            mark = ""
            if style_id == item["style_id"]:
                mark = " OK"
            elif style_id and families.get(style_id) == item["family"]:
                mark = " ~"
            heard.append(f"{entry['label']} {entry['probability']:.2f}{mark}")
        print(f"      {tag:44s} -> " + " | ".join(heard))
    print()
    S.log("'OK' = same style as the tag, '~' = same family, nothing = disagreement")
    S.log("if the audio never lands near the tag, do not use --fill")
    return 0


def cmd_calibration(config: Dict[str, Any], args: argparse.Namespace,
                    connection, normalizer: Normalizer) -> int:
    items = calibration_set(connection, limit=args.limit, per_style=args.per_style,
                            seed=args.seed, random_pick=args.random)
    if not items:
        S.log("no tracks with a clean tag yet - run step4_normalize.py first")
        return 1
    styles = {row["id"]: row["name"] for row in
              connection.execute("SELECT id, name FROM styles")}
    S.log(f"calibration: {S.human(len(items))} tracks across"
          f" {S.human(len({item['style_id'] for item in items}))} styles")
    root = music_root(config)
    files = [root / item["path"] for item in items]
    records = classify(config, args.tier, files, workers=args.workers)
    result = evaluate(normalizer, {str(root / key): value
                                   for key, value in records.items()},
                      [dict(item, path=str(root / item["path"])) for item in items],
                      family_map(connection))
    passed = print_metrics(result, gate_thresholds(config))

    listing = S.out_dir(config) / "calibration.csv"
    with listing.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.writer(handle)
        writer.writerow(["path", "tag", "expected_style", "heard_1", "heard_2", "heard_3"])
        for item in items:
            record = records.get(str(root / item["path"])) or {}
            heard = [entry["label"] for entry in (record.get("top") or [])[:3]]
            writer.writerow([item["path"], item["tag"], styles.get(item["style_id"], ""),
                             *heard[:3]])
    S.log("detail written to", str(listing))
    S.log("VERDICT: " + ("usable - the gate was passed" if passed else
                         "not usable as-is - try --tier maest or do not --fill"))
    return 0 if passed else 2


def gate_thresholds(config: Dict[str, Any]) -> Dict[str, float]:
    gate = S.setting(config, "classifier.gate", {}) or {}
    return {
        "top1": float(gate.get("top1", 0.45)),
        "top3": float(gate.get("top3", 0.70)),
        "family": float(gate.get("family", 0.85)),
    }


def cmd_audit(config: Dict[str, Any], args: argparse.Namespace,
              connection, normalizer: Normalizer) -> int:
    """Sample already-labelled tracks and list where the audio disagrees."""
    rows = list(connection.execute(
        "SELECT rel_path, raw_genre, primary_style, family, label_source FROM tracks"
        " WHERE primary_style IS NOT NULL AND label_source <> 'audio'"
        f"   AND NOT ({S.FAMILY_STYLE_SQL})"))
    rng = random.Random(args.seed)
    rng.shuffle(rows)
    rows = rows[:args.limit]
    if not rows:
        S.log("nothing labelled yet to audit")
        return 1
    root = music_root(config)
    records = classify(config, args.tier, [root / row["rel_path"] for row in rows],
                       workers=args.workers)
    families = family_map(connection)
    agree = family_agree = 0
    disputes = []
    for row in rows:
        record = records.get(str(root / row["rel_path"])) or {}
        if not record.get("ok"):
            continue
        mapped = [(resolve_label(normalizer, entry["label"]), entry["label"],
                   float(entry["probability"])) for entry in (record.get("top") or [])[:3]]
        mapped = [item for item in mapped if item[0]]
        if not mapped:
            continue
        if mapped[0][0] == row["primary_style"]:
            agree += 1
        elif any(families.get(style_id) == row["family"] for style_id, _, _ in mapped):
            family_agree += 1
        else:
            disputes.append({
                "path": row["rel_path"], "tag": row["raw_genre"],
                "tag_says": row["primary_style"], "source": row["label_source"],
                "audio_says": [label for _, label, _ in mapped],
                "probability": round(mapped[0][2], 3),
            })
    total = max(1, len(rows))
    S.log(f"audited {S.human(len(rows))} tracks:"
          f" {agree / total * 100:.1f} % agree exactly,"
          f" {family_agree / total * 100:.1f} % agree on the family,"
          f" {len(disputes) / total * 100:.1f} % disagree")
    listing = S.out_dir(config) / "audit_disagreements.csv"
    with listing.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.writer(handle)
        writer.writerow(["path", "tag_says", "tag", "source", "audio_1", "audio_2", "audio_3",
                         "probability"])
        for dispute in disputes:
            writer.writerow([dispute["path"], dispute["tag_says"], dispute["tag"],
                             dispute["source"], *dispute["audio_says"][:3],
                             dispute["probability"]])
    if disputes:
        S.log("disagreements written to", str(listing))
        for dispute in disputes[:12]:
            print(f"      {dispute['tag']!r} ({dispute['tag_says']})"
                  f"  ->  audio says {', '.join(dispute['audio_says'][:3])}")
    return 0


def cmd_fill(config: Dict[str, Any], args: argparse.Namespace,
             connection, normalizer: Normalizer) -> int:
    """Label the tracks nothing else could, from what they sound like."""
    sql = ("SELECT rel_path, name, album_artist, raw_genre FROM tracks"
           f" WHERE (primary_style IS NULL OR {S.FAMILY_STYLE_SQL} OR {S.VAGUE_STYLE_SQL})")
    params: List[Any] = []
    if args.style:
        sql += " AND primary_style = ?"
        params.append(S.style_slug(args.style))
    if args.limit:
        sql += " LIMIT ?"
        params.append(int(args.limit))
    rows = list(connection.execute(sql, params))
    if not rows:
        S.log("nothing left to fill - every track already has a specific style")
        return 0
    S.log(f"filling {S.human(len(rows))} tracks that have no specific style")
    root = music_root(config)
    records = classify(config, args.tier, [root / row["rel_path"] for row in rows],
                       workers=args.workers, use_cache=not args.no_cache)
    minimum = float(args.min_probability)
    top_k = int(S.setting(config, "classifier.top_k", 5) or 5)

    progress = S.Progress(len(rows), label="writing ")
    filled = skipped = 0
    for row in rows:
        record = records.get(str(root / row["rel_path"])) or {}
        progress.step()
        if not record.get("ok"):
            skipped += 1
            continue
        entries: List[S.StyleEntry] = []
        for entry in (record.get("top") or [])[:top_k]:
            style_id = resolve_label(normalizer, entry["label"])
            probability = float(entry.get("probability") or 0.0)
            if style_id is None or probability < minimum:
                continue
            weight = probability if not entries else probability * 0.7
            entries.append((style_id, min(1.0, weight), "audio", probability,
                            f"{entry['label']}={probability:.2f}"))
        if not entries:
            skipped += 1
            continue
        S.clear_track_styles(connection, ["audio"], [row["rel_path"]])
        S.add_track_styles(connection, row["rel_path"], entries)
        filled += 1
    progress.finish()
    connection.commit()
    S.refresh_style_counts(connection)
    S.refresh_track_primary(connection)
    connection.commit()
    S.log(f"filled {S.human(filled)} tracks from the audio"
          f" ({S.human(skipped)} stayed unlabelled: the model was not sure enough)")
    S.print_status(connection)
    return 0


def parse_args(argv: Optional[Sequence[str]] = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Step 7 - the acoustic classifier: fill and audit the styles",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--db", default="", help="dataset path (default: config.json)")
    parser.add_argument("--tier", default="", choices=["", "effnet", "maest"],
                        help="effnet (fast, 18 MB) or maest (accurate, 347 MB)")
    parser.add_argument("--workers", type=int, default=0, help="worker processes")
    parser.add_argument("--limit", type=int, default=0, help="how many tracks to look at")
    parser.add_argument("--per-style", type=int, default=2,
                        help="calibration: tracks per style (default 2)")
    parser.add_argument("--seed", type=int, default=7, help="sampling seed")
    parser.add_argument("--random", action="store_true",
                        help="calibration: random sample instead of per style")
    parser.add_argument("--min-probability", type=float, default=0.05,
                        help="fill: ignore labels below this probability")
    parser.add_argument("--style", default="", help="fill: only tracks with this style")
    parser.add_argument("--no-cache", action="store_true", help="ignore the results cache")
    parser.add_argument("--setup", action="store_true", help="venv + models (run once)")
    parser.add_argument("--check", action="store_true", help="is everything ready?")
    parser.add_argument("--sanity", action="store_true", help="20 tracks, look at the output")
    parser.add_argument("--calibration", action="store_true", help="measure the accuracy")
    parser.add_argument("--audit", action="store_true", help="sample and find disagreements")
    parser.add_argument("--fill", action="store_true", help="label the unknown tracks")
    return parser.parse_args(list(argv) if argv is not None else None)


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = parse_args(argv)
    config = S.load_config()
    if not args.tier:
        args.tier = str(S.setting(config, "classifier.tier", "effnet") or "effnet")
    if not args.workers:
        args.workers = int(S.setting(config, "classifier.workers", 4) or 4)
    if args.sanity and not args.limit:
        args.limit = 20
    if args.calibration and not args.limit:
        args.limit = 300
    if args.audit and not args.limit:
        args.limit = 200

    if args.setup:
        return setup(config, args.tier)
    if not any((args.check, args.sanity, args.calibration, args.audit, args.fill)):
        args.check = True
    if args.check:
        return cmd_check(config, args)

    path = S.resolve(args.db) if args.db else S.db_path(config)
    connection = S.connect(path, create=False)
    try:
        normalizer = Normalizer(connection, config)
        if args.sanity:
            return cmd_sanity(config, args, connection, normalizer)
        if args.calibration:
            return cmd_calibration(config, args, connection, normalizer)
        if args.audit:
            return cmd_audit(config, args, connection, normalizer)
        return cmd_fill(config, args, connection, normalizer)
    finally:
        connection.close()


if __name__ == "__main__":
    raise SystemExit(main())
