#!/usr/bin/env bash
# SimpleJellyMus - build the style dataset (steps 1-4, 6, 9 and 8).
#
# Read-only towards your music files and your Jellyfin server: everything lands
# in catalog.sqlite. Re-running is safe: each step only does the missing work.
#
#   ./prepare_styles.sh                  full run (includes the metal-api.dev lookup)
#   ./prepare_styles.sh --quick          skip the Jellyfin link and Metal-API lookup
#   ./prepare_styles.sh --link           also link Jellyfin item ids (needed by the player)
#   ./prepare_styles.sh --no-metal-api   skip the Metal-API lookup only

set -euo pipefail
cd "$(dirname "$0")"

quick=0
link=0
metal_api=1
for arg in "$@"; do
    case "$arg" in
        --quick) quick=1; metal_api=0 ;;
        --link)  link=1 ;;
        --no-metal-api) metal_api=0 ;;
        -h|--help) sed -n '2,11p' "$0"; exit 0 ;;
        *) echo "unknown option: $arg" >&2; exit 2 ;;
    esac
done

echo "== 1/7  reading the music files (ffprobe) =="
python3 step1_scan.py

if [ "$link" = "1" ] && [ "$quick" = "0" ]; then
    echo
    echo "== 2/7  linking Jellyfin item ids (slow on a Raspberry Pi) =="
    python3 step2_link.py
else
    echo
    echo "== 2/7  skipped (use --link to fill the Jellyfin item ids) =="
fi

echo
echo "== 3/7  vocabulary + coverage =="
python3 step3_vocabulary.py --all

echo
echo "== 4/7  mapping the raw genre tags onto styles =="
python3 step4_normalize.py

echo
if [ "$metal_api" = "1" ]; then
    echo "== 5/7  Metal-API lookup (metal + unknown, album-confirmed) =="
    python3 step6_metal_api.py --fill
else
    echo "== 5/7  skipped (run step6_metal_api.py alone anytime) =="
fi

echo
echo "== 6/7  propagation + consensus over every source =="
python3 step9_consensus.py

echo
echo "== 7/7  report =="
python3 step8_report.py

echo
echo "done.  next:"
echo "  python3 step7_classify.py --check          # the acoustic classifier"
echo "  python3 step7_classify.py --sanity"
echo "  python3 step7_classify.py --calibration    # accuracy + gate"
echo "  python3 step7_classify.py --fill           # label what is left"
echo "  python3 step5_llm.py --estimate            # DeepSeek, cost first"
echo "  python3 step6_metal_api.py --estimate      # Metal-API cache/status, no network"
echo "  python3 step6_metal_api.py --all-candidates # also query known non-metal artists"
echo "  python3 step9_consensus.py                 # re-vote once more sources exist"
