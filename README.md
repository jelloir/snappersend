# di-snapsend

**di-snapsend is a standalone tool for replicating [Snapper](http://snapper.io/)-managed
Btrfs snapshots from one host to another over SSH.** A **source** host (typically a
workstation or laptop running Snapper) ships its snapshots to a **destination** host
(typically an always-on server) using Btrfs send/receive. It's the on-host
snapshot-replication layer of a 3-2-1 backup strategy: keep recent snapshots locally
for quick rollback, mirror a deep history to another machine for disaster recovery,
and (optionally) let a tool like restic archive the destination offsite.

It is a thin orchestration layer — it does **not** reimplement the Btrfs stream
format or the network. It shells out to the tools that already solve those:

```
btrfs send [-p PARENT] SRC | [mbuffer |] ssh DEST "sudo btrfs receive DST"
```

…and confines itself to the three things a wrapper has to get right: snapshot
**enumeration**, **parent selection**, and partial-transfer **cleanup +
retention**. See `di-snapsend-SPEC.md` for the full design rationale and
`di-snapsend-DESIGN.py` for the annotated structural contract.

## How it works

```
   SOURCE host (e.g. laptop)                    DESTINATION host (e.g. server)
   ┌───────────────────────┐                    ┌────────────────────────────┐
   │  Snapper makes RO     │   btrfs send |     │  btrfs receive stores each │
   │  snapshots on a timer │   ssh ... btrfs    │  snapshot under            │
   │                       │   receive          │  /srv/snapshots-recv/...   │
   │  di-snapsend (hourly  │ ─────────────────► │                            │
   │  timer) sends the new │   (encrypted SSH,  │  least-privilege transport │
   │  ones incrementally   │    least-priv key) │  user, scoped sudoers,     │
   └───────────────────────┘                    │  forced-command filter     │
                                                └─────────────┬──────────────┘
                                                              │ (optional)
                                                     restic / offsite archive
```

- **Snapper** (on the source) creates the snapshots; di-snapsend never makes them.
- **di-snapsend** (on the source, on a timer) finds snapshots the destination
  doesn't have yet and sends them — a **full** send the first time, **incremental**
  thereafter.
- The **destination** just receives and stores them under a per-source-host
  directory, then di-snapsend prunes the destination per its retention policy.
- A separate tool running **on the destination** (e.g. restic) can archive the
  receive area further offsite.

## The three correctness rules (why this isn't a one-liner)

1. **Validity of a received subvolume** — a subvolume *existing* on the destination
   does not mean the transfer completed. A clean receive is `readonly` **and** has
   `received_uuid` set; a garbled one is writable with `received_uuid = -`. Every
   receive is verified; garbled subvols are deleted and the run treated as failed.
2. **Parent eligibility (correlation)** — a source snapshot is a valid `-p` parent
   only if the destination already holds a snapshot of the same Btrfs lineage
   (correlated by **UUID**, never by number). The parent for sending `N` is the
   newest correlated source older than `N`; otherwise a full send.
3. **Retention prune guard** — the newest correlated pair is **pinned** and never
   pruned, so the next incremental's parent always survives.

Because correlation is UUID-based, Snapper churning/gapping the source numbering
under its own (short) retention never breaks matching against the destination's long
retention. See SPEC §4.

## Layout of this repo

| File | Role |
|------|------|
| `di-snapsend` | the Python engine (installs to `/usr/local/bin/di-snapsend`) |
| `di-snapsend.sh` | installer — `--dest` (destination) and `--source` (source) roles |
| `config.example.toml` | annotated config (installs to `/etc/snapsend/config`) |
| `snapsend-ssh-filter` | forced-command wrapper for the transport key (destination) |
| `systemd/` | `snapsend.{service,timer}` + missed-run watchdog units & script |
| `test_di_snapsend.py` | unit tests for the pure correctness logic |
| `tools/` | test-only retention fabricator + VM execution checker |
| `tests/RETENTION-TESTING.md` | how retention is tested (decision + execution) |
| `di-snapsend-SPEC.md`, `di-snapsend-DESIGN.py` | design docs / contract |

## Prerequisites

**Both hosts** need Btrfs and OpenSSH (both in Debian/Ubuntu *main*; any distro with
Btrfs works):

| Need | Package | Where |
|------|---------|-------|
| `btrfs send`/`receive`, `subvolume` | `btrfs-progs` | source **and** destination |
| SSH client/server | `openssh-client` (source), `openssh-server` (destination) | both |
| Python ≥ 3.11 (for `tomllib`) | `python3` | source |
| Snapshots to replicate | `snapper`, configured for the subvolumes you want | source |
| `rsync` (only if using the boot tier) | `rsync` | both |
| `mbuffer` (optional, smooths throughput) | `mbuffer` | source (auto-installed by `--source`) |

The destination's receive area **must be on a Btrfs filesystem** (it stores received
subvolumes). Snapper must already be making snapshots on the source for the
subvolumes you list in the config.

## Install

The installer (`di-snapsend.sh`) has two roles. There's a deliberate chicken-and-egg:
the **destination** wants the **source's** public key, but that key doesn't exist
until the source role runs. So the destination is provisioned in **two passes** —
once to set everything up (printing the `authorized_keys` line to complete later),
then again to authorize the key once the source has generated it. The canonical
sequence:

1. **Destination, first pass** — `sudo ./di-snapsend.sh --dest` (no key yet):
   provisions the transport user, receive area, sudoers, and SSH filter, and **prints
   the `authorized_keys` line format** to complete once the source key exists.
2. **Source** — `sudo ./di-snapsend.sh --source`: generates the transport key, pins
   the destination's host key, installs the timer, and **prints the public key** plus
   the exact command to authorize it on the destination.
3. **Destination, finalize** — either re-run `sudo ./di-snapsend.sh --dest
   /path/to/id_ed25519.pub` with the copied key, **or** paste the printed
   `authorized_keys` line manually.
4. **Edit the config** on the source, then **verify** with `sudo di-snapsend
   --dry-run` and seed.

> **Role flags:** `--dest` (aliases: `--destination`, `--server`) and `--source`
> (alias: `--laptop`). The `--server`/`--laptop` spellings are historical and kept
> for back-compat; the generic names are `--dest`/`--source`.

### 1. Destination host (the receiver) — first pass

```sh
sudo ./di-snapsend.sh --dest                      # no key yet; prints the authorized_keys line to add later
```

This creates the least-privilege `snapsend` transport user, the receive area
`/srv/snapshots-recv` (**must be Btrfs**), a scoped sudoers rule, and the
forced-command SSH filter (so the key can only run the exact send/receive command
set, even if it leaks). With **no** pubkey argument (the first pass, before the source
key exists), it prints the `authorized_keys` line to complete in step 3. If you
already have the source's public key copied over, you can pass it now and skip the
finalize pass:

```sh
sudo ./di-snapsend.sh --dest /path/to/id_ed25519.pub   # authorizes the key directly
```

### 2. Source host (the sender)

```sh
sudo ./di-snapsend.sh --source
```

This installs the engine + watchdog, writes `/etc/snapsend/config` from the example,
generates the transport SSH key at `/etc/snapsend/ssh/id_ed25519`, **pins the
destination's SSH host key**, and enables `snapsend.timer` (hourly) +
`snapsend-watchdog.timer`. It prints the public key and the exact `--dest` command to
register it on the destination.

### 3. Destination host — finalize the key

Copy the source's public key (`/etc/snapsend/ssh/id_ed25519.pub`) to the destination
and authorize it, either by re-running `--dest` with the file:

```sh
sudo ./di-snapsend.sh --dest /path/to/id_ed25519.pub
```

or by pasting the `authorized_keys` line printed in step 1 manually. (Skip this step
if you already passed the key in step 1.)

### 4. Edit the config (`/etc/snapsend/config`)

Open it on the source and set, at minimum:

- **`[server].host`** — the destination's hostname or IP (replace `dest-host`).
- **`[server].ssh_port` / `user` / `ssh_key`** — usually fine as-is.
- **`[subvolumes.*]`** — one table per subvolume to replicate, each with the
  `mountpoint` where its Snapper `.snapshots` lives (e.g. `/` and `/home`). The
  destination path is composed automatically as
  `<recv_base>/<this-host>/<name>/…`.
- **`[retention]`** — `keep_hourly/daily/weekly/monthly` (per-subvol overrides allowed)
  and `timezone` (`local` default). See **Retention** below — set these ≥ your source
  (Snapper) retention so the destination governs the archive depth. `keep_hourly` is
  the finest tier; matching it to Snapper's hourly count quiets the "source holding
  extra" line (below). Omitting `keep_hourly` (or `0`) disables it — the prior behaviour.
- **`[boot]`** — enable if you also want `/boot` + `/boot/efi` mirrored for full DR.

### 5. Verify, then seed

```sh
sudo di-snapsend --dry-run        # reads both sides, changes nothing — check the plan
sudo systemctl start snapsend.service
journalctl -u snapsend.service -f
```

### First-run SSH host-key trust

The source must trust the destination's SSH host key before the first transfer, or
every send/rsync fails with `Host key verification failed` (and rsync with
`kex_exchange_identification: Connection reset`). The installer pins it automatically
via `ssh-keyscan` into **root's** `known_hosts` (the systemd service runs as root, so
the trust must live there — not the invoking user's).

If you provisioned manually, or changed `[server].host` after install, run once as
root on the source:

```sh
sudo ssh-keyscan -t ed25519 dest-host >> /root/.ssh/known_hosts
```

Note: a manual `ssh snapsend@dest-host` test will be **rejected by the forced-command
filter** (expected — the key is locked to the transfer commands), but it still
records the host key. `ssh-keyscan` is cleaner because it fetches only the host key
without running a remote command.

### The seed (first) transfer

The first run does a **full send** per subvolume, which can be large. Do the seed
over a **wired link** if you can; subsequent incrementals are small and fine over
Wi-Fi. The tool is fail-safe and resumable at snapshot granularity — if a transfer is
interrupted, the partial is discarded and re-sent next run, and already-transferred
snapshots are kept.

## Troubleshooting

- **`Host key verification failed` / `Connection reset`** — the destination host key
  isn't trusted; see *First-run SSH host-key trust* above.
- **`btrfs receive` fails / "not a btrfs filesystem"** — the destination's
  `recv_base` isn't on Btrfs. Point it at a Btrfs filesystem and re-run `--dest`.
- **"No snapper snapshots … nothing to do"** — Snapper isn't making snapshots at the
  configured `mountpoint`, or the `[subvolumes.*].mountpoint` is wrong. Check
  `snapper -c <config> list` and that `<mountpoint>/.snapshots/<N>/snapshot` exists.
- **`[WARN] mbuffer not on PATH`** — harmless; install `mbuffer` (Debian main) or set
  `use_mbuffer = false` to silence it.
- **Nothing sends but snapshots exist** — they may already be on the destination
  (correlated). `--dry-run` shows what would send and why.

## Operations

- **Logs:** `/var/log/snapsend.log` (and the journal via systemd). Levels:
  `[STEP]/[INFO]/[OK]/[WARN]/[ERROR]`.
- **Dry run:** `--dry-run` (or `$SNAPSEND_DRY_RUN=1`) — reads remote state but
  performs no sends or prunes.
- **One subvolume:** `--subvol home`.
- **Override destination host:** `--dest other-host` (alias `--server`; or `$SNAPSEND_SERVER`).
- **No mbuffer:** `--no-mbuffer`.
- **Watchdog:** `snapsend-watchdog` warns (syslog) if no successful run is recorded in
  `/var/lib/snapsend/last-success` within `SNAPSEND_MAX_AGE_HOURS` (default 6).
- **Locking:** a `flock` on `/var/lock/snapsend.lock` prevents overlapping runs.

Precedence for operational settings is **CLI flag > env var > config file**.
Retention is policy and lives only in the config file.

## Retention — how source and destination retention interact

This trips people up, so read this once. **There are two independent retention
systems:**

- **Snapper** prunes the **source** on its own schedule (its config, not
  di-snapsend's).
- **di-snapsend** prunes the **destination** per the `[retention]` policy in
  `/etc/snapsend/config`.

They run independently — and the destination keep-rule is a **UNION**:

> A destination snapshot is **kept if EITHER (a)** it falls within di-snapsend's GFS
> policy (`keep_hourly/daily/weekly/monthly`) **OR (b)** its source snapshot still exists.
> Kept once if both — there is **no duplication**, never "two histories". One set of
> snapshots, selected by either rule.

```
  destination snapshots (one set)
  ┌────────────────────────────────────────────────────────────┐
  │  (b) still on the SOURCE         (a) within GFS policy     │
  │  ┌───────────────────────┐    ┌──────────────────────────┐ │
  │  │  recent, dense        │    │  hourly/daily/weekly/    │ │
  │  │                       │    │  monthly                 │ │
  │  │  (source-backed)      │####│  representatives         │ │   ## = overlap,
  │  │                       │####│  (the long-tail taper)   │ │        kept once
  │  └───────────────────────┘    └──────────────────────────┘ │
  │      KEEP = the UNION (whichever reaches further back)     │
  └────────────────────────────────────────────────────────────┘
```

**So the destination is a *superset* of the source.** While the source holds a
snapshot, the destination keeps its copy regardless of the GFS numbers; the GFS
policy only thins the **long tail** — snapshots the source has *already* dropped.
Don't be surprised if the destination holds **more** than `keep_daily` implies.

### Which policy actually governs (per tier)

Because it's a union, **whichever retention reaches further back wins, at each tier
independently:**

- **Destination longer than source (the intended setup):** di-snapsend's numbers
  govern the archive depth; source-backed just protects the recent dense history from
  being pruned while the source still references it. The two cooperate. **Recommended.**
- **Source longer than destination at some tier:** the source-backed rule overrides
  di-snapsend's shorter limit *at that tier* — the destination can't prune something
  the source still has, so that tier's config is **effectively dead** (it never gets
  to act). Not a bug and not data loss (you simply keep more), but the config doesn't
  mean what it says. di-snapsend logs an INFO line per run/subvolume when this
  happens (`source retention is holding N extra snapshot(s) beyond the destination
  policy …`) so it isn't invisible.
- **Mixed:** resolved per-tier — the larger reach wins at each of hourly/daily/weekly/
  monthly in the same run.

### The hourly tier and the "source holding extra" line

`keep_hourly` is the **finest** GFS tier (added after daily/weekly/monthly): it keeps the
newest snapshot of each of the most recent N hours. It exists to **match Snapper's
granularity**. Snapper typically keeps a couple of days of *hourlies* on the source; if
di-snapsend's smallest bucket is daily, every intra-day hourly is "beyond the destination
policy" yet source-backed — so rule (b) keeps it and the INFO line above reports a count
that climbs through the day and drops at the daily rollover. Accurate, but noisy.

Set `keep_hourly ≥ the source's hourly count` (e.g. `keep_hourly = 48` for Snapper's
default ~48 hourlies) and those snapshots get a **real destination bucket** — they're
kept by rule (a) as policy, not by rule (b) as overflow — so the override line stops
reporting the hourly band. Leaving `keep_hourly` unset (or `0`) **disables the tier
entirely**, which is exactly the behaviour of releases before it existed: a config
without the key prunes identically to today. New installs default to `keep_hourly = 24`.

### Rule of thumb

> For di-snapsend's retention to be the thing that governs your destination archive,
> set **each tier of the destination retention ≥ the corresponding tier on the
> source**, and keep the source retention short. A common, sensible setup: the source
> keeps a week or two of recent snapshots (quick rollback on a small disk), while the
> destination keeps a long GFS taper (e.g. 14 daily / 8 weekly / 6 monthly, or
> longer) for archival/DR. Configured this way the source stays lean, the destination
> holds the deep history, and **shortening the source retention never reduces
> recoverability** — the destination is a superset and still holds everything the
> source does, plus more.

**Why it's designed this way:** the union rule is what eliminates pointless
re-send/re-prune churn — di-snapsend never deletes a destination copy only to re-send
it next run because its source still exists.

The **pinned** newest correlated pair (Rule 3) is always kept regardless of the
numbers, so the next incremental's parent always survives.

### Retention timezone

`[retention].timezone` chooses the calendar used for the hourly/daily/weekly/monthly
bucket boundaries (global, all subvols):

- `"local"` (**default**) — boundaries align to **the source machine's local
  calendar**, so a "Monday weekly" is genuinely your local Monday. Recommended. (For
  example, on a machine at UTC+10, a snapshot taken Monday 08:00 local is correctly
  bucketed into the local Monday's week.)
- `"utc"` — UTC boundaries; timezone-independent, for strict cross-zone determinism
  or shared/relocating setups.

Folder **names** are always local-time **+ UTC offset** (e.g. `…+1000…`) regardless of
this setting. The hourly tier honours this timezone exactly like the coarser tiers —
its bucket key is `(year, month, day, hour)` taken from the same zone-shifted instant.
Local bucketing in a DST zone is cosmetically imperfect for exactly one day a year — a
fall-back day/hour may keep one extra snapshot, a spring-forward one fewer — but never
loses data or breaks the chain; no DST configuration is needed.

## Destination layout & the restore pointer

Every tier lands under a **per-source-host** subtree, so several source machines can
share one destination with zero collision. The host segment is the source machine's
name, derived automatically (`socket.gethostname()`), not configured:

```
/srv/snapshots-recv/                 # = [server].recv_base, on the destination
└── <source-hostname>/               # e.g. source-host/
    ├── home/<localdate>-<offset>-<num>-<short_uuid>/snapshot   # 20260627-2300+1000-8-a2159d69
    ├── root/<localdate>-<offset>-<num>-<short_uuid>/snapshot
    ├── boot/                        # boot tier (rsync mirror)
    └── boot-efi/
```

Received subvolumes are named `<localdate>-<offset>-<num>-<short_uuid>` — the **date
leads** (source-local time, `%Y%m%d-%H%M`, colon-free) so `ls` sorts chronologically;
`<offset>` is the UTC offset (`+1000`, `+0000`) which self-documents the source's zone
and keeps names unique across a DST fall-back; `<num>` mirrors Snapper's number and
`<short_uuid>` is the uniqueness backstop / correlation key. The name carries *local*
time for readability, but **the authoritative retention timestamp is parsed back out
of the name** (to UTC).

> **Don't manually rename received snapshot folders.** The folder name encodes the
> snapshot's original source timestamp, which retention parses back to decide the
> snapshot's "when" for daily/weekly/monthly bucketing. Renaming a folder corrupts its
> apparent timestamp and can make retention mis-bucket it.

`<recv_base>/<source-host>/<subvol>/<subvol>.latest` always points at the newest
received subvol's `…/snapshot` — a stable target for a downstream archiver (e.g.
restic) running **on the destination**.

> **Note for maintainers — `.latest` is write-only over the transport.** di-snapsend
> only ever *writes* `.latest` (via `ln -sfn`, which the forced-command filter
> allows); it never *reads* it back over SSH. The symlink is for a consumer running
> locally on the destination, which reads it server-side. So `readlink` is
> deliberately **not** in `snapsend-ssh-filter`'s allowlist — don't add it, and don't
> introduce an over-the-transport `.latest` read without also widening the filter.

The boot tier (`/boot` ext4, `/boot/efi` FAT32) is mirrored with `rsync -aAX --delete`
to `<recv_base>/<source-host>/{boot,boot-efi}` — a single current copy, not versioned
here (a downstream archiver versions the receive area), under the **same**
`<source-host>/` as the snapshot tiers. It feeds an external bare-metal restore flow
(format EFI/boot, restore both, rebuild initramfs + GRUB); this tool only *produces*
the mirror.

> **Layout note (if upgrading from an older version):** the destination path is
> `<recv_base>/<source-host>/<subvol>/<localdate>-<offset>-<num>-<uuid>/snapshot`.
> There is no automatic migration of subvols from an older naming scheme — delete the
> old receive area and let di-snapsend rebuild from empty (a fresh full send), e.g. on
> the destination:
> ```sh
> # for each old <subvol>/<wrapper> holding a `snapshot` subvol:
> sudo btrfs subvolume delete /srv/snapshots-recv/<old-path>/*/snapshot
> sudo rm -rf /srv/snapshots-recv/<old-paths>
> ```

## Tests

```sh
SNAPSEND_QUIET=1 python3 -m unittest -v test_di_snapsend
```

The suite covers the silent-but-catastrophic logic: `is_correlated` (all clauses +
guards), `choose_parent`, `_newest_correlated_pair`, the Rule-1 validity properties,
`parse_subvolume_show` against real `btrfs subvolume show` output, exhaustive
retention bucketing (multi-year, sporadic, boundary/DST, pin, source/destination
interaction across tiers) under both timezones, config loading, timestamp parsing,
and `_run_pipe`'s all-stage exit-code capture. Retention **execution** on real
subvolumes is covered separately — see `tests/RETENTION-TESTING.md`.
