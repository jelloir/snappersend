# snappersend

**snappersend replicates [Snapper](http://snapper.io/)-managed Btrfs snapshots from a
source host to a destination over SSH**, using `btrfs send | ssh btrfs receive`. A
**source** (typically a workstation/laptop running Snapper) ships snapshots to a
**destination** (typically an always-on server). It's the on-host replication layer of
a 3-2-1 backup strategy: keep recent snapshots locally for quick rollback, mirror a
deep history to another machine for disaster recovery, and (optionally) let a tool like
restic archive the destination offsite.

It is a thin orchestration layer — it does **not** reimplement the Btrfs stream format
or the network. It shells out to the tools that already solve those:

```
btrfs send [-p PARENT] CLONE | [mbuffer |] ssh DEST "sudo btrfs receive DST"
```

snappersend is a ground-up rewrite of `di-snapsend`. It exists to fix two things, and
both fixes are the heart of its design:

1. **Retention is WYSIWYG.** The destination keeps **exactly** the GFS policy you
   configure — no hidden "superset", no silently-kept extras. (One documented
   exception: the pinned parent, below.)
2. **Gaps never force a full send.** snappersend keeps its **own** read-only parent
   clones, immune to Snapper's retention, so an incremental survives an arbitrary
   offline gap (a holiday, a powered-off laptop) instead of falling back to a full
   re-send.

## How it works

### Newest-only sends

Each run, snappersend sends **only Snapper's current newest snapshot** for each
configured subvolume — not every snapshot Snapper made since the last run. The
destination is therefore a **sample at snappersend's run cadence**, not a copy of every
Snapper snapshot. This is deliberate: it's what makes retention WYSIWYG (the send path
never looks backward, so there's no "re-send a snapshot we already pruned" problem). For
the rollback / DR use case this is exactly what you want, and the more often you run
snappersend, the finer the sample.

### The parent-preservation tree

On the **source**, snappersend keeps a private tree of read-only **clones** of the
snapshots it has successfully sent — one set per subvolume, under
`PARENT_TREE_BASE/<subvol>/` (default `/.snappersend/<subvol>/`).

- A clone is made with `btrfs subvolume snapshot -r`. It **shares extents via reflink**
  (near-zero space) but is an **independent subvolume**: deleting Snapper's original
  does not touch it. That independence is the whole point — it's what decouples the
  incremental parent's lifetime from Snapper's retention.
- The tree **must be on the same Btrfs filesystem** as the source subvolume (so
  `btrfs send -p` can diff locally and the clone is reflinked, not copied). The default
  `/.snappersend` satisfies this for the usual `@`/`@home` layout.

**snappersend sends the clone, not Snapper's snapshot.** This is subtle but essential.
A `btrfs subvolume snapshot` always gets a brand-new UUID. The destination correlates
incrementals by UUID (`received_uuid`). If snappersend sent Snapper's snapshot but kept
a *clone* as the parent, the clone's UUID wouldn't match what the destination received,
and the next incremental would break. By sending the clone itself, the destination's
`received_uuid` equals the clone's UUID — so the clone snappersend keeps **is** the
thing the destination correlates with. (`btrfs send -p` between two clones of different
Snapper snapshots works fine — it just diffs two read-only subvolumes that share
extents.)

### The promote-on-confirmed-send invariant

This is the safety rule that makes the parent tree trustworthy. The tree only ever
advances to a clone that has been **confirmed landed on the destination**. Per run, per
subvolume:

1. **Read** the existing clones — the prior confirmed-sent states, untouched.
2. **Choose the parent**: the newest clone that still **correlates** with a snapshot
   the destination holds. (No correlating clone → full send.)
3. If Snapper's current newest is already represented by a correlating clone, there's
   **nothing to send** — go straight to retention. (Re-runs are idempotent.)
4. Otherwise **stage a clone** of Snapper's newest and **send it**, `-p <parent clone>`.
5. **On a verified-good receive only**: keep the staged clone and prune clones beyond
   `PARENT_KEEP`. *The tree advances only here.*
6. **On any send/receive failure**: delete the staged clone — the tree returns to
   exactly its prior state. Next run retries off the same preserved parent.

What this buys you:

- **Holiday / long gap.** Every failed run leaves the parent frozen and still
  correlating with the destination's newest. When you come back online, the incremental
  works off the preserved clone — **no full send**. This is the scenario snappersend
  exists for.
- **Divergence** (the destination's snapshot was deleted or altered, or it's the very
  first run): no clone correlates, so snappersend does a **full send**, logs it loudly
  at `INFO` (`no shared parent with destination — full send`), and reseeds the parent
  tree on success. Full send is the rare genuine-divergence / first-run path, never the
  routine gap path.
- **Crash between “send OK” and “prune”** is safe: the staged clone is already in the
  tree and the destination already has it, so the next run just continues off it. The
  invariant guarantees the parent is never *ahead* of the destination — stale-behind is
  recoverable, ahead would be the corruption trap, so snappersend always stays on the
  safe side.

## Retention — WYSIWYG GFS

The destination is pruned to a **pure grandfather-father-son** keep-set over the
destination's own snapshots: the newest snapshot of each of the most recent
`TIMELINE_LIMIT_HOURLY` hours, `…_DAILY` days, `…_WEEKLY` ISO-weeks, `…_MONTHLY`
months, and `…_YEARLY` years, unioned. A tier set to `0` is disabled. Undatable
snapshots are always kept.

> **The single WYSIWYG asterisk — the pinned parent.** The destination keeps exactly
> your GFS policy, **plus** the most recent snapshot is always retained as the
> incremental base (so the next incremental's `-p` parent still exists). That's the only
> snapshot kept beyond what the GFS numbers say.

There is **no** "source-backed" union and **no** override logging — unlike di-snapsend,
the destination never silently holds more than its policy implies. Source snapshots are
never touched; Snapper owns local retention entirely.

`RETENTION_TIMEZONE` (`local` default, or `utc`) chooses the calendar used for bucket
boundaries; folder names are always local-time + UTC offset regardless. For the `root`
subvolume, snappersend also avoids orphaning half of an apt pre/post snapshot pair (a
small nicety that composes fine with pure GFS).

## Configuration

snappersend reads a flat `KEY="value"` file (default `/etc/snappersend/config`), the
**same format as Snapper's own config files**, parsed by
[`python-dotenv`](https://pypi.org/project/python-dotenv/) (`python3-dotenv`, in Debian
main). The retention block uses Snapper's exact `TIMELINE_LIMIT_*` names — including
`TIMELINE_LIMIT_YEARLY` — so a Snapper user reads it and immediately understands it.

- **Honoured keys:** `SERVER_HOST` (required), `SSH_PORT`, `SERVER_USER`, `SSH_KEY`,
  `RECV_BASE`, `USE_MBUFFER`, `SUBVOLUMES`, `PARENT_TREE_BASE`, `PARENT_KEEP`,
  `BOOT_ENABLED`, `BOOT_PATHS`, `RETENTION_TIMEZONE`, `TIMELINE_LIMIT_{HOURLY,DAILY,
  WEEKLY,MONTHLY,YEARLY}`, and per-subvol overrides `<SUBVOL>_TIMELINE_LIMIT_*`.
- **Ignored gracefully** (so you can keep Snapper-style keys around, or point
  snappersend at a Snapper-shaped file without it choking): `SUBVOLUME`, `FSTYPE`,
  `QGROUP`, `SPACE_LIMIT`, `FREE_LIMIT`, `ALLOW_USERS`, `ALLOW_GROUPS`, `SYNC_ACL`,
  `BACKGROUND_COMPARISON`, `NUMBER_*`, `TIMELINE_CREATE`, `TIMELINE_CLEANUP`,
  `TIMELINE_MIN_AGE`, `EMPTY_PRE_POST_*`.

See `config.example` for an annotated starting point.

> **Why python-dotenv** (not `configparser` or `configobj`): a Snapper config is a flat
> `KEY="value"` file with no `[section]` header, so `configparser` can't read it without
> a synthesised fake section. `configobj` can, but raises on a duplicate key.
> `python-dotenv` is purpose-built for exactly this format and degrades gracefully on
> malformed input — the right fit for "read a Snapper-shaped file tolerantly". It's the
> one packaged dependency, and it's in Debian (Trixie) main.

## CLI

```
snappersend                 # replicate every configured subvolume
snappersend --dry-run       # read both sides; log intended clones/sends/promotions/
                            #   prunes; change nothing
snappersend --subvol home   # just one subvolume
snappersend --config PATH   # alternate config (default /etc/snappersend/config)
snappersend --no-mbuffer    # don't pipe through mbuffer
snappersend --skip-boot     # skip the /boot + /boot/efi rsync tier
snappersend -v              # verbose: log every shell command
```

Logging is `[STEP]/[INFO]/[OK]/[WARN]/[ERROR]` to the terminal (coloured on a tty) and
to `/var/log/snappersend.log` (override `$SNAPPERSEND_LOG`). The full-send fallback is
always logged at `INFO` with its reason. snappersend exits non-zero if any subvolume
fails, leaving the chains intact for retry. A `flock` (`/var/lock/snappersend.lock`)
prevents overlapping runs.

## Install / first run

snappersend is a single Python script plus its config; the destination transport
(least-privilege user, scoped sudoers, forced-command SSH key) is provisioned exactly as
for di-snapsend and is reused unchanged — the remote command set
(`btrfs receive`/`subvolume`/`property`, `mkdir`, `rmdir`, `ls`, `ln`, `rsync`) is
identical.

1. Install the script and `python3-dotenv`:
   ```sh
   sudo install -m 0755 snappersend /usr/local/bin/snappersend
   sudo apt-get install -y python3-dotenv btrfs-progs openssh-client mbuffer rsync
   ```
2. Write `/etc/snappersend/config` from `config.example`; set `SERVER_HOST` and your
   `SUBVOLUMES`.
3. Make sure the source can reach the destination over the transport key (host key
   pinned in root's `known_hosts`).
4. **First run** does a **full send** per subvolume, which **seeds the parent tree**.
   Do the seed over a wired link if you can; subsequent incrementals are small.
   ```sh
   sudo snappersend --dry-run     # check the plan
   sudo snappersend               # seed
   ```

The first run logs `no shared parent with destination — full send` for each subvolume
(expected — there's no parent clone yet). After it, `/.snappersend/<subvol>/` holds the
seed clone, and every later run is a small incremental off the preserved parent.

> **Not yet included (a later pass):** the systemd timer, the missed-run watchdog, and
> Snapper-hook triggering. snappersend writes a success stamp
> (`/var/lib/snappersend/last-success`) the future watchdog will read, but ships now as
> a clean CLI that does one full replication pass per invocation.

## Tests

```sh
SNAPPERSEND_QUIET=1 python3 -m pytest test_snappersend.py -q
```

The suite covers the parent-tree invariant (failed send leaves the parent unchanged;
success promotes and prunes; diverged destination → full send + reseed; crash-after-
send-before-promote is safe), WYSIWYG GFS retention including the yearly tier (and that
no source-backed snapshot survives beyond GFS), Snapper-schema config parsing, and the
carried-over correctness logic (valid-received detection, UUID correlation,
receive-in-place, run-lock collision).
