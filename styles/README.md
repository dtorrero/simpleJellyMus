# styles/ - building the style dataset for SimpleJellyMus

This folder is **independent from the player**. It reads your music files and
your Jellyfin server, decides what style each song is, and writes the result
into one SQLite database. The player only ever *reads* that database.

> **Nothing here ever writes to a music file, to Jellyfin, or to the player's
> own configuration.** Every step is read-only towards the outside world and
> writes only into `catalog.sqlite` and the `out/` folder. Metadata is never
> "fixed" - the mapping lives in the database.

## The idea

```
 music files ──ffprobe──► tracks + raw genre tag            (step1)
      │                        │
      │                        ├── vocabulary + rules ──────► canonical styles (step3/4)
      │                        │
      │                        ├── DeepSeek (optional) ─────► artist knowledge (step5)
      │                        ├── Wikidata / MusicBrainz ──► external styles (step6, not built)
      └── audio excerpt ──────►► acoustic classifier ──────► objective styles (step7)
                                                        (fills what is missing AND
                                                         audits everything else)
                                   ▼
              all of the above ──► propagation + consensus (step9):
                                   the sources vote; agreement is kept,
                                   a disagreement goes to out/review.csv
                                   ▼
                       catalog.sqlite  ──► the player: pick or type a style,
                                           instant random playback
```

Seven layers, strongest first. A later layer never overwrites a stronger one for
the same track+style, so the pipeline is safe to re-run at any time:

| source       | rank | what it is |
|--------------|------|------------|
| `human`      | 100  | your verdicts in `data/overrides.json` |
| `tag`        |  80  | the genre tag, when it is already a canonical style name |
| `rules`      |  70  | that same tag cleaned, split, translated (`data/styles.json`, `data/rules.json`) |
| `audio`      |  60  | the acoustic classifier listening to the track |
| `consensus`  |  55  | step 9: several independent sources agreed (it can *promote* that agreement) |
| `external`   |  50  | Wikipedia / Wikidata / MusicBrainz |
| `llm`        |  40  | DeepSeek's knowledge about the artist |
| `propagated` |  30  | step 9: inferred from the album/artist of the track |

## Run it

```bash
cd styles

python3 step1_scan.py                 # 1. files -> tracks + raw tags   (~4 min, 41k files)
python3 step2_link.py --dry-run       # 2. check the Jellyfin path mapping
python3 step2_link.py                 #    then link item ids            (~10 min on a Pi)
python3 step3_vocabulary.py --all     # 3. vocabulary + coverage report
python3 step4_normalize.py            # 4. raw tags -> canonical styles (seconds)
python3 step9_consensus.py            # 9. propagate + vote over every source

python3 step7_classify.py --setup     # 7a. models + venv (once, ~700 MB)
python3 step7_classify.py --check     # 7b. is it wired correctly?
python3 step7_classify.py --sanity    # 7c. 20 tracks: what does it hear?
python3 step7_classify.py --calibration   # 7d. accuracy + pass/fail gate
python3 step7_classify.py --fill      # 7e. label what nothing else could
python3 step7_classify.py --audit     # 7f. where does the audio disagree?

python3 step5_llm.py --estimate       # 5. DeepSeek: cost first, then --run

python3 step9_consensus.py            # 9. re-vote now that audio/LLM answered
python3 step8_report.py               # 8. health report + picker preview
python3 styles_db.py                  # state of the dataset, any time
```

> **Step 6 (Wikipedia / Wikidata / MusicBrainz) is not implemented.** It was in
> the original plan, but DeepSeek covers the same question with far better
> coverage on obscure bands (MusicBrainz and Wikidata resolved only about 6 of
> 10 sample artists each, and no single free source knew the long tail). The
> database already reserves the `external` source rank and the
> `artists.external_json` column, so it can be added later without any change to
> the schema.

`prepare_styles.sh` runs steps 1-4, 9 and 8 in the right order.

Every step is idempotent and resumable: re-running only does the work that is
missing, and nothing is ever lost (`tracks.raw_genre` keeps the original tag
forever, so any layer can be recomputed).

## The dataset (`catalog.sqlite`)

By default `~/.local/share/simplejellymus/catalog.sqlite` (set `db_path` in
`config.json`). One row per song, plus a many-to-many table, because a song can
have several styles:

| table | what it holds |
|---|---|
| `tracks` | one row per song: path, tags, title/album/artist/year/duration, the verbatim `raw_genre`, the Jellyfin `id` + cover tags, and the denormalised `primary_style` / `family` / `label_source` |
| `track_styles` | every style of a track: `weight` (1.0 = main, lower = secondary), `source`, `confidence`, `evidence` |
| `styles` | the vocabulary: `id`, `name`, `family`, `parent`, `aliases`, and materialised `tracks`/`artists` counts |
| `raw_genres` | audit trail: each raw tag, how many tracks use it, what it became, what is still ununderstood |
| `artists` | artist roll-up + cache for the Wikidata/MusicBrainz/LLM answers (so nothing is ever asked twice) |
| `api_calls` | token/cost accounting for the LLM step |
| `meta` | schema version and timestamps |

Queries the player will run (measured on the real dataset, 41k tracks: 0.2-2 ms):

```sql
-- 200 random tracks of one style, never repeating the last 60
SELECT ts.rel_path FROM track_styles ts
 WHERE ts.style_id = ? AND ts.rel_path NOT IN (...)
 ORDER BY RANDOM() LIMIT 200;

-- a whole family
SELECT ts.rel_path FROM track_styles ts JOIN styles s ON s.id = ts.style_id
 WHERE s.family = 'Metal' ORDER BY RANDOM() LIMIT 200;
```

## The classifier: prove it before you trust it

Step 7 is deliberately a *measurement* first. The order matters:

1. `--sanity` (20 tracks you can check by ear). Each line shows the tag and the
   model's top four labels, marked `OK` (same style), `~` (same family) or
   nothing (disagreement). If `OK`/`~` are rare, stop here.
2. `--calibration` (~300 tracks whose tag is *exactly* a canonical style name -
   the cleanest ground truth available without hand-labelling). It prints
   top-1 / top-3 / family accuracy and compares them with the gate
   (`classifier.gate` in `config.json`, default 45 % / 70 % / 85 %) and writes
   `out/calibration.csv`. **The gate is the decision:** pass means the audio can
   be used to fill and to audit; fail means try `--tier maest` (347 MB, 519
   Discogs styles) before using it at all.
3. `--fill` labels only the tracks that have no specific style (no tag, or a
   tag that only states a family), with `source='audio'`, the probability as
   confidence, and the model's label as evidence.
4. `--audit` samples already-labelled tracks and lists where the audio
   disagrees with the tag - `out/audit_disagreements.csv`. This is the report
   that tells you whether your *tags* or the *classifier* need attention.

The excerpts come straight from the mount (`ffmpeg` decodes the first
`classifier.feed_seconds`, 40 s by default, at 16 kHz mono), several workers in
parallel, and every result is cached in `out/classify_cache.jsonl` so a re-run
costs nothing.

Two caveats, stated plainly:

* The models (Essentia / MTG-UPF: `discogs-effnet` + `genre_discogs400`) are
  trained on Discogs styles, which match this library's vocabulary closely but
  not perfectly. Unmapped labels are reported by `--calibration`.
* The calibration measures agreement *with the tags*, which are usually right
  for exact tag matches but are not guaranteed. For a hand-checked set, put your
  own rows in a CSV and use `--calibration --random` plus `out/calibration.csv`
  as the review sheet.

## DeepSeek fills the gaps (step 5)

The rules layer resolves **99 % of the tracks that have a genre tag**; what is
left is the ~23 % with no tag at all, plus the tracks whose tag only states a
family ("Metal"). They belong to a few hundred artists, and `step5_llm.py` asks
DeepSeek about *those artists* - not about 41 000 songs.

```bash
echo 'DEEPSEEK_API_KEY=sk-...' > .env      # git-ignored; or config.local.json

python3 step5_llm.py --estimate          # no key, no network: artists, requests, $
python3 step5_llm.py --probe             # one tiny request: does the key work?
python3 step5_llm.py --sample 20         # 20 artists: the verdict next to your tags
python3 step5_llm.py --run               # the fill (cached, budget-guarded)
python3 step5_llm.py --audit --min-tracks 5   # second opinion on the tagged artists
```

Measured on this library: **696 artists, 28 requests, ≈ $0.06**, covering the
10 682 tracks that had no specific style.

How it behaves:

* **Closed vocabulary.** The prompt carries the 137 style names from
  `data/styles.json`; an answer that invents a style is dropped, and its aliases
  are accepted (so "Trash Metal" still lands on Thrash Metal).
* **It only fills.** Labels are written with `source='llm'`, which ranks below a
  real tag, the rules and the audio classifier: it can fill a hole but never
  overwrite evidence. Only the tracks that had no specific style are touched.
* **Cached forever** in `artists.llm_json` (keyed by model + prompt version +
  vocabulary), so re-runs and the `--sample` you already paid for cost nothing.
  Every request is logged in `api_calls` with its tokens and cost.
* **Budget guard:** `--run` refuses to start if the projection is over
  `--max-cost` (or `llm.max_cost_usd`, default $3).
* **`--audit` never writes:** it compares the model's opinion with the
  tag-derived styles and writes `out/llm_disagreements.csv`, which is the
  shortest possible list of things worth a human look.

## Propagation and consensus (step 9)

The earlier steps each answer alone. Step 9 is the referee: it looks at the whole
picture of a track, and it is completely offline and free.

```bash
python3 step9_consensus.py              # propagation + the vote + the reports
python3 step9_consensus.py --propagate  # only fill the gaps
python3 step9_consensus.py --dry-run    # write no style, just show the picture
python3 step9_consensus.py --limit 500  # a quick look on a few hundred tracks
```

**Propagation** - what an artist (or an album) already is, its own tracks say.
If nine of a band's ten tracks are "Death Metal", the tenth one is too. The
label is written with `source='propagated'` (rank 30, the weakest there is), only
on tracks that had no *specific* style, and only when at least
`consensus.propagate_min_tracks` tracks (default 2) *and*
`consensus.propagate_min_ratio` of the album/artist agree - a lone tag never
spreads. A `Various Artists` compilation is treated more strictly: it is never
pooled into the single `various artists` bucket (it is not one artist), and its
own album only speaks when it is near-unanimous
(`consensus.propagate_compilation_min_ratio`, default 0.9, over
`consensus.propagate_compilation_min_tracks`, default 3) - so a punk sampler is
not relabelled from one track. The winner is chosen deterministically (most
votes, ties broken by style id), so every run gives the same answer. Measured
here: 562 tracks filled, 562 labels.

**Consensus** - the four *independent* voters of a track are asked to vote:
`metadata` (your tag/rules - one voter, not two), `external`, `audio`, `llm`.
Propagation is not a voter (it only echoes). Then:

* unanimous specific voters, with at least `consensus.min_independent_sources`
  (default 2) of them, get one `consensus` label. It ranks *below* the audio
  classifier but *above* a lone automatic guess, so it can promote an agreement
  (for example when the audio and the LLM both say "Melodic Death Metal" and the
  tag only said "Metal") - it never overwrites real evidence.
* any real disagreement is **never written**. The track goes to
  `out/review.csv`, most useful first: `disputed` (your own tag says one family
  and the automatic layers heard another - the tag is the suspect), `conflict`
  (the sources split across families), `mismatch` (same family, different style)
  and low-confidence agreements. `out/consensus_report.txt` summarises the run.

On the current dataset (tags + rules + LLM only, no audio/external yet) the vote
finds 39 conflicts and 3 mismatches out of 41 139 tracks - the short, honest list
worth a human look. Once step 6/7 add their evidence, `step9_consensus.py` is the
step that turns an agreement among them into labels.

## Editing the vocabulary

`data/styles.json` is the vocabulary (styles, families, spelling variants) and
`data/rules.json` is the behaviour (what to drop, what to split, typos,
fallbacks). Both are plain text, versioned in git, and yours to edit.

```bash
python3 step3_vocabulary.py --uncovered   # what the vocabulary does not know yet
#   -> out/uncovered_parts.txt, sorted by how many tracks it affects
# add those spellings as aliases (or as rules), then:
python3 step3_vocabulary.py --all && python3 step4_normalize.py
```

`data/overrides.json` always wins over every automatic layer - per artist, per
album (`"Artist :: Album"`) or per file path. An empty style list means "no
style at all", so the track is never picked by a style filter.

## How the player uses it

`catalog.py` in the app (stdlib `sqlite3` only) exposes the same queries and
opens this file read-only: **no style or tag is ever written back**. In the
player, `G` opens the style panel (fuzzy search over names and aliases with
track counts, family chips, `any of` / `all of` / `not`), `Play these` applies
the selection, and the choice is remembered in the player's config. The queue
then refills from the catalog in a few milliseconds instead of asking Jellyfin
for random songs.

```bash
python3 main.py --style "Ska Punk"                # from the command line
python3 main.py --style "family:Metal" --style-mode any
```

The player keeps its "standard library + Pillow" promise: no TensorFlow, no
LLM, no extra network - only the database file.

## Troubleshooting

| symptom | fix |
|---|---|
| `music root does not exist` | set `music_root` in `config.json` |
| `no Jellyfin credentials` | run the player once (`python3 ../main.py`) or fill `jellyfin.*` in `config.json` |
| many tracks `without_id` after step 2 | run `python3 step2_link.py --detect-only`: it prints the server path next to a local one. Jellyfin usually reports the path **inside its container** (`/musicMedia/...`), not the one you mounted (`/mnt/...`) - set `jellyfin.path_prefix` accordingly |
| `no API key` from step 5 | `echo 'DEEPSEEK_API_KEY=...' > styles/.env` (git-ignored) |
| step 5 says "REFUSING to run" | the projection is over the budget: raise `--max-cost` or `llm.max_cost_usd` |
| `missing model files` | `python3 step7_classify.py --setup` |
| the classifier cannot start | `python3 step7_classify.py --check` - it prints the real error |
| a style shows fewer tracks than expected | check `out/normalize_report.txt` and `out/uncovered_parts.txt` |
