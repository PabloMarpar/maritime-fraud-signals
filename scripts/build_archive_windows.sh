#!/usr/bin/env bash
# Build every monthly window the DMA archive serves (2024-03-01..2025-02-26; March is only April's
# lead-in). Windows are built strictly one at a time. Per window: pipeline.window, then detect.gaps
# (not part of pipeline.window). After all windows: sanctions matches over every window's
# identity, then one panel per window. Every step is idempotent, so re-running resumes where it
# stopped.
#
# Downloading is bandwidth-bound (~9 MB/s, one ~500 MB zip per day) and the detectors are
# CPU-bound, so two background backfills download+clean later months while earlier windows run
# their detectors. They never touch the same days as a running window: each window waits for the
# backfill that covers its days before starting. data/manifest.json is lock-protected for exactly
# this (pipeline.manifest). If a backfill fails, the window's own backfill re-fetches what's missing.
set -uo pipefail
cd "$(dirname "$0")/.."

LOG_DIR=outputs/logs
mkdir -p "$LOG_DIR"
STATUS="$LOG_DIR/build_archive_windows.status"
: > "$STATUS"

# start end gaps_end_exclusive
WINDOWS=(
  "2024-04-01 2024-04-30 2024-05-01"
  "2024-05-01 2024-05-31 2024-06-01"
  "2024-06-01 2024-06-30 2024-07-01"
  "2024-07-01 2024-07-31 2024-08-01"
  "2024-08-01 2024-08-31 2024-09-01"
  "2024-09-01 2024-09-30 2024-10-01"
  "2024-10-01 2024-10-31 2024-11-01"
  "2024-11-01 2024-11-30 2024-12-01"
  "2024-12-01 2024-12-31 2025-01-01"
  "2025-01-01 2025-01-31 2025-02-01"
  "2025-02-01 2025-02-26 2025-02-27"
)

step() {
  local name=$1; shift
  local log="$LOG_DIR/$name.log"
  echo "$(date '+%F %T') START $name" >> "$STATUS"
  if "$@" >> "$log" 2>&1; then
    echo "$(date '+%F %T') OK    $name" >> "$STATUS"
  else
    echo "$(date '+%F %T') FAIL  $name (see $log)" >> "$STATUS"
    return 1
  fi
}

step prefetch_2024-07 python -m pipeline.backfill --start 2024-07-01 --end 2024-10-01 &
PREFETCH_SUMMER=$!
step prefetch_2024-12 python -m pipeline.backfill --start 2024-12-01 --end 2025-02-26 &
PREFETCH_WINTER=$!

for w in "${WINDOWS[@]}"; do
  read -r start end gaps_end <<< "$w"
  win="window=${start}_${end}"
  case "$start" in
    2024-07-01) wait "$PREFETCH_SUMMER" ;;
    2024-12-01) wait "$PREFETCH_WINTER" ;;
  esac
  # June's pipeline.window build predates P3-4/A4's orchestrator; its artifacts already exist
  # (legacy gaps copied into its partition), so pipeline.window only verifies/skips there.
  step "window_${start}" python -m pipeline.window --start "$start" --end "$end" || continue
  if [ ! -f "data/detect/gaps/$win/part-0.parquet" ]; then
    step "gaps_${start}" python -m detect.gaps --start "$start" --end "$gaps_end" \
      --voyages-path "data/tracks/voyages/$win/part-0.parquet" \
      --out-path "data/detect/gaps/$win/part-0.parquet" || continue
  fi
done

step sanctions_match python -m process.sanctions_match --force

for w in "${WINDOWS[@]}"; do
  read -r start end _ <<< "$w"
  step "panel_${start}" python -m features.panel --start "$start" --end "$end" --force \
    --out-path "data/processed/panel/window=${start}_${end}/part-0.parquet"
done

echo "$(date '+%F %T') DONE" >> "$STATUS"
