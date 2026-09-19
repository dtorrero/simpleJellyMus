#!/usr/bin/env bash
# SimpleJellyMus - build the style dataset (steps 1-4 + 8).
#
# Read-only towards your music files and your Jellyfin server: everything lands
# in catalog.sqlite. Re-running is safe: each step only does the missing work.
#
#   ./prepare_styles.sh              full run
#   ./prepare_styles.sh --quick      skip the (slow) Jellyfin id linking
#   ./prepare_styles.sh --link       also link Jellyfin item ids (needed by the player)

set -euo pipefail
cd "$(dirname "$0")"

quick=0
link=0
for arg in "$@"; do
    case "$arg" in
        --quick) quick=1 ;;
        --link)  link=1 ;;
        -h|--help) sed -n '2,10p' "$0"; exit 0 ;;
        *) echo "unknown option: $arg" >&2; exit 2 ;;
    esac
done

echo "== 1/5  reading the music files (ffprobe) =="
python3 step1_scan.py

if [ "$link" = "1" ] && [ "$quick" = "0" ]; then
    echo
    echo "== 2/5  linking Jellyfin item ids (slow on a Raspberry Pi) =="
    python3 step2_link.py
else
    echo
    echo "== 2/5  skipped (use --link to fill the Jellyfin item ids) =="
fi

echo
echo "== 3/5  vocabulary + coverage =="
python3 step3_vocabulary.py --all

echo
echo "== 4/5  mapping the raw genre tags onto styles =="
python3 step4_normalize.py

echo
echo "== 5/5  report =="
python3 step8_report.py

echo
echo "done.  next:"
echo "  python3 step7_classify.py --check          # the acoustic classifier"
echo "  python3 step7_classify.py --sanity"
echo "  python3 step7_classify.py --calibration    # accuracy + gate"
echo "  python3 step7_classify.py --fill           # label what is left"
echo "  python3 step6_external.py                  # Wikidata / MusicBrainz (free)"
echo "  python3 step5_llm.py --estimate            # DeepSeek, cost first"
