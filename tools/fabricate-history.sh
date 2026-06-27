#!/usr/bin/env bash
# fabricate-history.sh — TEST-ONLY. Create / list / cleanup backdated REAL (empty)
# Btrfs subvolumes in a receive area, to exercise di-snapsend retention EXECUTION
# on the VM (the real `btrfs subvolume delete` + wrapper `rmdir` + `.latest`).
#
# WHY REAL SUBVOLUMES: retention runs `btrfs subvolume delete`, which FAILS on a
# plain mkdir'd directory ("not a subvolume"). So execution testing needs real
# subvolumes — `btrfs subvolume create` is near-instant and ~zero space, so a
# few hundred fabricated "snapshots" are cheap. (The *decision* logic is covered
# exhaustively by the pure-logic unit tests; this only proves execution.)
#
# Run ON THE SERVER (it calls btrfs locally). It writes under a DEDICATED fake
# host segment (default 'fabricate-test') so it never touches real backups; the
# folder names carry backdated source timestamps exactly as di-snapsend writes
# them (<localdate>-<offset>-<num>-<short_uuid>). Pair with the laptop-side
# tools/retention-vm-check.py, then `cleanup`.
set -euo pipefail

RECV_BASE="${RECV_BASE:-/srv/snapshots-recv}"
HOST="${HOST:-fabricate-test}"          # fake host dir — isolates from real data
NAMETZ="${NAMETZ:-Australia/Brisbane}"  # render names in the sender's local zone

usage() {
    cat >&2 <<EOF
fabricate-history.sh — TEST-ONLY backdated empty-subvolume fabricator.

  sudo $0 fabricate <subvol> <hourly|daily|weekly> <count>
  sudo $0 list      <subvol>
  sudo $0 cleanup   [<subvol>]      # omit subvol to remove the whole HOST tree

Env (current): RECV_BASE=$RECV_BASE  HOST=$HOST  NAMETZ=$NAMETZ
Layout: \$RECV_BASE/\$HOST/<subvol>/<localdate>-<offset>-<num>-<short_uuid>/snapshot
EOF
    exit "${1:-1}"
}

step_seconds() {
    case "$1" in
        hourly) echo 3600 ;;
        daily)  echo 86400 ;;
        weekly) echo 604800 ;;
        *) echo "unknown cadence '$1' (use hourly|daily|weekly)" >&2; exit 1 ;;
    esac
}

cmd_fabricate() {
    local subvol="$1" cadence="$2" count="$3"
    command -v btrfs >/dev/null || { echo "btrfs-progs required" >&2; exit 1; }
    [[ "$count" =~ ^[0-9]+$ && "$count" -gt 0 ]] || { echo "count must be a positive integer" >&2; exit 1; }
    local s; s="$(step_seconds "$cadence")"
    local base; base="$(date +%s)"; base=$(( base - base % s ))   # align to cadence
    local dir="$RECV_BASE/$HOST/$subvol"
    mkdir -p "$dir"
    local i num epoch name wd made=0
    for (( i = count - 1; i >= 0; i-- )); do
        num=$(( count - i ))
        epoch=$(( base - i * s ))
        name="$(TZ="$NAMETZ" date -d "@$epoch" +%Y%m%d-%H%M%z)-${num}-$(printf 'fab%05x' "$num")"
        wd="$dir/$name"
        mkdir -p "$wd"
        [[ -e "$wd/snapshot" ]] || btrfs subvolume create "$wd/snapshot" >/dev/null
        made=$(( made + 1 ))
    done
    echo "fabricated ${made} subvols under ${dir} (cadence=${cadence}, names in ${NAMETZ})" >&2
}

cmd_list() {
    local subvol="$1"
    ls -1 "$RECV_BASE/$HOST/$subvol" 2>/dev/null || true
}

cmd_cleanup() {
    local subvol="${1:-}"
    local root="$RECV_BASE/$HOST"
    [[ -n "$subvol" ]] && root="$RECV_BASE/$HOST/$subvol"
    [[ -d "$root" ]] || { echo "nothing to clean at $root" >&2; return 0; }
    # delete every 'snapshot' subvolume under root, then remove the wrapper dirs
    while IFS= read -r d; do
        btrfs subvolume delete "$d" >/dev/null 2>&1 || true
    done < <(find "$root" -mindepth 1 -maxdepth 4 -type d -name snapshot 2>/dev/null)
    rm -rf "$root"
    echo "cleaned ${root}" >&2
}

[[ $# -ge 1 ]] || usage 1
case "$1" in
    fabricate) shift; [[ $# -eq 3 ]] || usage 1; cmd_fabricate "$@" ;;
    list)      shift; [[ $# -eq 1 ]] || usage 1; cmd_list "$@" ;;
    cleanup)   shift; cmd_cleanup "${1:-}" ;;
    -h|--help) usage 0 ;;
    *) echo "unknown command: $1" >&2; usage 1 ;;
esac
