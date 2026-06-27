# di-snapsend

Snapper-native Btrfs snapshot replication from the laptop (`millionaire`) to the
server (`debian-server`) over SSH. The on-prem disaster-recovery / snapshot-history
tier of the Debstillation backup architecture.

It is a thin orchestration layer — it does **not** reimplement the Btrfs stream
format or the network. It shells out to the tools that already solve those:

```
btrfs send [-p PARENT] SRC | [mbuffer |] ssh SERVER "sudo btrfs receive DST"
```

…and confines itself to the three things a wrapper has to get right: snapshot
**enumeration**, **parent selection**, and partial-transfer **cleanup +
retention**. See `di-snapsend-SPEC.md` for the full design rationale and
`di-snapsend-DESIGN.py` for the annotated structural contract.

## The three correctness rules (why this isn't a one-liner)

1. **Validity of a received subvolume** — a subvolume *existing* on the target
   does not mean the transfer completed. A clean receive is `readonly` **and** has
   `received_uuid` set; a garbled one is writable with `received_uuid = -`. Every
   receive is verified; garbled subvols are deleted and the run treated as failed.
2. **Parent eligibility (correlation)** — a source snapshot is a valid `-p` parent
   only if the server already holds a snapshot of the same Btrfs lineage
   (correlated by **UUID**, never by number). The parent for sending `N` is the
   newest correlated source older than `N`; otherwise a full send.
3. **Retention prune guard** — the newest correlated pair is **pinned** and never
   pruned, on either side, so the next incremental's parent always survives.

Because correlation is UUID-based, Snapper churning/gapping the laptop numbering
under its own (short) retention never breaks matching against the server's long
retention. See SPEC §4.

## Layout of this repo

| File | Role |
|------|------|
| `di-snapsend` | the Python engine (installs to `/usr/local/bin/di-snapsend`) |
| `di-snapsend.sh` | installer — `--server` and `--laptop` roles |
| `config.example.toml` | annotated config (installs to `/etc/snapsend/config`) |
| `snapsend-ssh-filter` | forced-command wrapper for the transport key (server) |
| `systemd/` | `snapsend.{service,timer}` + missed-run watchdog units & script |
| `test_di_snapsend.py` | unit tests for the pure correctness logic |
| `di-snapsend-SPEC.md`, `di-snapsend-DESIGN.py` | design docs / contract |

Only `btrfs-progs` + `openssh` are required (both in Debian main); `mbuffer` is
optional; `tomllib` is stdlib on Python ≥ 3.11.

## Install

### 1. Laptop (sender)

```sh
sudo ./di-snapsend.sh --laptop
```

This installs the engine + watchdog, writes `/etc/snapsend/config` from the
example (edit `[server].host` first!), generates the transport key at
`/etc/snapsend/ssh/id_ed25519`, prints its public key, and enables
`snapsend.timer` (hourly) + `snapsend-watchdog.timer`.

### 2. Server (receiver)

Copy the printed `id_ed25519.pub` to the server, then:

```sh
sudo ./di-snapsend.sh --server /path/to/id_ed25519.pub
```

This creates the least-privilege `snapsend` user, the receive area
`/srv/snapshots-recv` (**must be on a Btrfs filesystem**), a scoped sudoers rule,
the forced-command ssh filter, and authorizes the laptop key (locked to that
filter, no pty/forwarding). Run without the pubkey argument to get the
`authorized_keys` line to paste manually.

### 3. Verify

```sh
sudo di-snapsend --dry-run        # reads both sides, changes nothing
sudo systemctl start snapsend.service
journalctl -u snapsend.service -f
```

### First-run SSH host-key trust

The laptop must trust the server's SSH host key before the first transfer, or
every send/rsync fails with `Host key verification failed` (and rsync with
`kex_exchange_identification: Connection reset`). The installer pins it
automatically via `ssh-keyscan` into **root's** `known_hosts` (the systemd
service runs as root, so the trust must live there — not the invoking user's).

If you provisioned manually, or changed `[server].host` after install, run once
as root:

```sh
sudo ssh-keyscan -t ed25519 debian-server >> /root/.ssh/known_hosts
```

Note: a manual `ssh snapsend@server` test will be **rejected by the
forced-command filter** (that's expected — the key is locked to the transfer
commands), but it still records the host key. `ssh-keyscan` is cleaner because it
fetches only the host key without running a remote command.

## The seed (first) transfer

The first run does a **full send** per subvolume, which for `@` and `@home` can be
large. Do the seed over a **wired link** if you can; subsequent incrementals are
small and fine over WiFi. The tool is fail-safe and resumable at snapshot
granularity — if a transfer is interrupted, the partial is discarded and re-sent
next run, and already-transferred snapshots are kept.

## Operations

- **Logs:** `/var/log/snapsend.log` (and the journal via systemd). Levels:
  `[STEP]/[INFO]/[OK]/[WARN]/[ERROR]`.
- **Dry run:** `--dry-run` (or `$SNAPSEND_DRY_RUN=1`) — reads remote state but
  performs no sends, promotes, or prunes.
- **One subvolume:** `--subvol home`.
- **Override server:** `--server other-host` (or `$SNAPSEND_SERVER`).
- **No mbuffer:** `--no-mbuffer`.
- **Watchdog:** `snapsend-watchdog` warns (syslog) if no successful run is
  recorded in `/var/lib/snapsend/last-success` within `SNAPSEND_MAX_AGE_HOURS`
  (default 6).
- **Locking:** a `flock` on `/var/lock/snapsend.lock` prevents overlapping runs.

Precedence for operational settings is **CLI flag > env var > config file**.
Retention is policy and lives only in the config file.

## Retention policy (the server is a superset of the laptop)

Retention prunes the **server only** (Snapper owns the laptop). The server is a
**superset** of the laptop: while a snapshot still exists on the laptop, its
server copy is always kept. The `keep_daily/keep_weekly/keep_monthly` GFS numbers
thin only the **long tail** — server snapshots whose laptop original has already
aged out. So don't be surprised if the server holds **more** than `keep_daily`
implies: it mirrors every current laptop snapshot plus a GFS-thinned tail of older
ones. This is intentional (cheap on COW storage, and the right DR-mirror
behaviour) and it is what prevents pointless re-send/re-prune churn — di-snapsend
never deletes a server copy only to re-send it next run. The pinned incremental
parent (Rule 3) is always kept regardless of the numbers.

## Server layout & the restore pointer

Every tier lands under a **per-host** subtree, so several machines can share one
server with zero collision. The host segment is derived automatically
(`socket.gethostname()`), not configured:

```
/srv/snapshots-recv/                 # = [server].recv_base
└── <hostname>/                      # e.g. millionaire/
    ├── home/<date>-<num>-<short_uuid>/snapshot   # e.g. 20260627-1300-8-a2159d69
    ├── root/<date>-<num>-<short_uuid>/snapshot
    ├── boot/                        # boot tier (rsync mirror)
    └── boot-efi/
```

Received subvolumes are named `<date>-<num>-<short_uuid>` — the **date leads**
(`%Y%m%d-%H%M`, colon-free) so `ls` of a subvol's directory sorts chronologically;
`<num>` mirrors Snapper's number and `<short_uuid>` ties the name to the
correlation key. `<recv_base>/<hostname>/<subvol>/<subvol>.latest` always points at
the newest received subvol's `…/snapshot` — the stable target for the downstream
**restic-on-server** tier.

The boot tier (`/boot` ext4, `/boot/efi` FAT32) is mirrored with
`rsync -aAX --delete` to `<recv_base>/<hostname>/{boot,boot-efi}` — a single
current copy, not versioned here (restic versions the receive area for free), and
under the **same** `<hostname>/` as the snapshot tiers. The **restore side already
exists** in `di-btrfs-recovery.sh`, which formats EFI/boot and restores both, then
chroots to rebuild initramfs + GRUB; this tool only *feeds* that flow.

> **Layout changed in this version — re-seed the receive area.** The destination
> moved to `<recv_base>/<hostname>/<subvol>/<date>-<num>-<uuid>/snapshot` (per-host
> segment + dated names). There is no automatic migration of subvols already on
> the server. If you have an older receive area, delete the old subvols and let
> di-snapsend rebuild from empty, e.g. on the server:
> ```sh
> # for each old <subvol>/<wrapper> holding a `snapshot` subvol:
> sudo btrfs subvolume delete /srv/snapshots-recv/<subvol>/*/snapshot
> sudo rm -rf /srv/snapshots-recv/<subvol> /srv/snapshots-recv/<old-hostname-dirs>
> ```
> then re-run `sudo di-snapsend` — the next run does full sends into the new layout.

## Tests

```sh
SNAPSEND_QUIET=1 python3 -m unittest -v test_di_snapsend
```

The suite covers the silent-but-catastrophic logic: `is_correlated` (all clauses
+ guards), `choose_parent`, `_newest_correlated_pair`, the Rule-1 validity
properties, `parse_subvolume_show` against real `btrfs subvolume show` output,
retention bucketing + pin + pre/post pairing, config loading, timestamp parsing,
and `_run_pipe`'s all-stage exit-code capture.
