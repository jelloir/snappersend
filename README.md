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

Two design choices are the heart of it:

1. **Retention is WYSIWYG.** The destination keeps **exactly** the GFS policy you
   configure — no hidden "superset", no silently-kept extras. (One documented
   exception: the pinned parent, below.)
2. **Gaps never force a full send.** snappersend keeps its **own** read-only parent
   clones, immune to Snapper's retention, so an incremental survives an arbitrary
   offline gap (a holiday, a powered-off laptop) instead of falling back to a full
   re-send.

> **Prerequisite: Snapper.** snappersend does **not** create snapshots — it replicates
> the ones [Snapper](http://snapper.io/) already makes. It assumes Snapper is installed
> and actively taking timeline snapshots on the source, with a `.snapshots` directory
> under each configured subvolume's mountpoint (e.g. `/.snapshots`, `/home/.snapshots`).
> If a subvolume has no readable snapshots, snappersend logs a warning and skips it —
> there is nothing to send.

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
- **Unreachable destination ≠ divergence.** A transport failure (the destination is
  offline, DNS fails, the link is down) is detected as such and **never** mistaken for
  divergence: snappersend aborts that subvolume *before* touching the parent tree
  (`destination unreachable … parent tree left intact, will retry next run`) and exits
  non-zero. Because it doesn't re-stage the newest snapshot, it can't delete a
  still-correlating clone — so when the link returns, the next run resumes with a normal
  incremental instead of being forced into a full send. This is what makes the "holiday /
  long gap" guarantee hold even when the gap is caused by the *network* rather than the
  source being offline.
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

There is **no** "source-backed" union and **no** override logging: the destination never
silently holds more than its policy implies. Source snapshots are never touched; Snapper
owns local retention entirely.

`RETENTION_TIMEZONE` (`local` default, or `utc`) chooses the calendar used for bucket
boundaries; folder names are always local-time + UTC offset regardless. For the `root`
subvolume, snappersend also avoids orphaning half of an apt pre/post snapshot pair (a
small nicety that composes fine with pure GFS). The `bootmirror` subvolume (below) is
just another configured subvolume, so its retention is the same mechanism by
construction.

## The boot tier — the `@bootmirror` subvolume

`/boot` (ext4) and `/boot/efi` (FAT32) aren't Btrfs, so `btrfs send` can't carry them
directly. Instead they are mirrored **into** Btrfs on the source, and everything
downstream is ordinary subvolume machinery:

1. **`snappersend --bootmirror-sync`** rsyncs each `BOOT_PATHS` entry into the
   `@bootmirror` subvolume (mounted at `/.bootmirror`), re-rooted to match the live
   absolute paths — so a restore is a straight copy back:

   ```
   btrfs top-level
   ├── @            → /              (root, snapper-managed)
   ├── @home        → /home
   └── @bootmirror  → /.bootmirror   ← own snapper config ("bootmirror")
       ├── boot/                       mirror of /boot      (ext4 contents)
       └── boot/efi/                   mirror of /boot/efi  (ESP contents)
   ```

2. A systemd oneshot (**`bootmirror-sync.service`**) runs that sync **just before
   each snapper timeline tick**, so Snapper — with its own `bootmirror` config —
   snapshots the fresh mirror **in the same tick** as `@` and `@home`.
3. snappersend then replicates `bootmirror` as a perfectly ordinary configured
   subvolume: same parent-preservation tree, same incremental sends, same WYSIWYG
   GFS retention. There is **no boot special-case in the replication path at all**.

Why this design:

- **Epoch matching.** The old approach (rsync the live `/boot` to the destination at
  snappersend run time) sampled the boot state at a different moment than the Snapper
  root snapshot — potentially far from it. A kernel update landing between the two
  produced an initramfs/root-modules skew: exactly the case that breaks a restored
  snapshot's boot. Now the mirror is refreshed immediately before the timeline tick,
  so `/boot`, the ESP, and `/` share one epoch per snapshot.
- **The ESP's FAT32-ness stops mattering** once mirrored: `btrfs send` ships the
  mirrored files as ordinary file contents.
- **The pre-snapper step can never block or break snapper.** The coupling is
  ordering-only (`Wants=` + `Before=` from a drop-in — snapper does not wait on the
  sync's exit status), the unit has a bounded `TimeoutStartSec`, and the sync is
  **source-local**: no SSH, no destination, no network, no lock shared with the send
  run — a network or destination problem cannot stall it. Worst case the mirror is
  one tick stale and snapper snapshots the previous boot state: degraded, not broken.

Details:

- **FAT32 mtimes** (2-second granularity on the ESP) are handled automatically:
  vfat/msdos sources get rsync `--modify-window=1`; ext4 stays exact.
- **Nested mounts**: `/boot/efi` lives inside `/boot`'s tree, so the `/boot` pass
  excludes it (rsync shields excluded paths from `--delete`) and the `/boot/efi` pass
  syncs it separately with its own mtime window.
- rsync's `--itemize-changes` is kept **for the log only** (a "N change(s)" /
  "unchanged" line per path); Snapper does the versioning now, so nothing downstream
  depends on change detection.
- rsync rc 24 ("source files vanished" — e.g. a kernel update touching the live
  `/boot` mid-sync) counts as success.
- **Retention** for `bootmirror` is the ordinary per-subvol mechanism —
  `BOOTMIRROR_TIMELINE_LIMIT_*` — like any other name in `SUBVOLUMES`. (The old
  per-boot-path `BOOT_TIMELINE_LIMIT_*` / `BOOT_EFI_TIMELINE_LIMIT_*` overrides went
  away with the dest-side versioning.)

### Provisioning `@bootmirror` (source, one-time)

```sh
# 1. Create it as a TOP-LEVEL subvolume (a sibling of @/@home, NOT nested inside @,
#    so it is never captured inside @'s own snapshots).
mnt=$(mktemp -d) && sudo mount -o subvolid=5 UUID=<btrfs-fs-uuid> "$mnt"
sudo btrfs subvolume create "$mnt/@bootmirror"
sudo umount "$mnt" && rmdir "$mnt"

# 2. Mount it at /.bootmirror:
echo 'UUID=<btrfs-fs-uuid>  /.bootmirror  btrfs  subvol=@bootmirror  0 0' \
  | sudo tee -a /etc/fstab
sudo mkdir -p /.bootmirror && sudo mount /.bootmirror

# 3. Give it its own snapper config so it timelines like any other subvolume:
sudo snapper -c bootmirror create-config /.bootmirror

# 4. Declare it to snappersend in /etc/snappersend/config:
#      SUBVOLUMES="root:/ home:/home bootmirror:/.bootmirror"

# 5. Install bootmirror-sync.service + the snapper drop-in (systemd section below),
#    then seed the mirror once by hand:
sudo snappersend --bootmirror-sync
```

> **Upgrading from the dest-side versioned mirrors** (the old
> `<RECV_BASE>/<host>/{boot,boot-efi}` parents holding a `.mirror` subvolume plus
> dated versions): provision `@bootmirror` as above, then remove the old boot parents
> on the destination by hand (`btrfs subvolume delete` the `.mirror` and version
> subvolumes, then remove the plain wrapper dirs). snappersend deliberately carries
> no migration code for one-time steps.

## Configuration

snappersend reads a flat `KEY="value"` file (default `/etc/snappersend/config`), the
**same format as Snapper's own config files**, parsed by
[`python-dotenv`](https://pypi.org/project/python-dotenv/) (`python3-dotenv`, in Debian
main). The retention block uses Snapper's exact `TIMELINE_LIMIT_*` names — including
`TIMELINE_LIMIT_YEARLY` — so a Snapper user reads it and immediately understands it.

- **Honoured keys:** `SERVER_HOST` (required), `SSH_PORT`, `SERVER_USER`, `SSH_KEY`,
  `RECV_BASE`, `USE_MBUFFER`, `SUBVOLUMES`, `PARENT_TREE_BASE`, `PARENT_KEEP`,
  `BOOT_ENABLED` (gates `--bootmirror-sync`), `BOOT_PATHS` (its rsync sources),
  `RETENTION_TIMEZONE`, `TIMELINE_LIMIT_{HOURLY,DAILY,WEEKLY,MONTHLY,YEARLY}`, and
  per-subvol overrides `<SUBVOL>_TIMELINE_LIMIT_*` (incl. `BOOTMIRROR_...`).
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
snappersend --report        # read-only status view (see below); change nothing
snappersend --subvol home   # just one subvolume
snappersend --config PATH   # alternate config (default /etc/snappersend/config)
snappersend --no-mbuffer    # don't pipe through mbuffer
snappersend --bootmirror-sync   # ONLY rsync BOOT_PATHS into /.bootmirror, then exit
                                #   (source-local, no SSH — the pre-snapper-tick step)
snappersend -v              # verbose: log every shell command

snappersend setup-dest admin@host   # provision the destination over your admin login
                                     #   (transport user + scoped sudoers; ships no files)
snappersend decom-dest  admin@host   # remove that transport config (keeps received data)
```

### `--report` — read-only status & health view

`--report` prints, per configured subvolume (`bootmirror` included, like any other),
how the destination stands **right now** — it **changes nothing** (no sends, clones,
deletes, renames, property writes, or success stamp) and takes no run lock, so it is
safe to run alongside a real replication. It honours `--subvol` and `--server`, and
each subvolume's own `retention_for()` policy. For each subvol it shows:

- **Destination snapshots** (newest first): num + date, current **tier(s)**
  (`hourly|daily|weekly|monthly|yearly`, or `pinned parent` / `prepost partner` /
  `kept (undatable)` for the keeps that live outside GFS), the next-prune verdict
  **`KEEP`/`PRUNE`**, and a `GARBLE!` flag on any received subvol that fails the
  valid-received check.
- **Source parent clones**: num + date, which one is the current incremental parent
  (`*`), whether each still correlates with a destination snapshot, and whether the tree
  is at/over/under `parent_keep`.
- **Health lines**: *Chain* — intact, or `WARN: next run will full-send` when no clone
  correlates with any destination snapshot; and *Lag* — how far (snapshots + wall-clock)
  the destination trails the source's newest.

Tiers are **computed live** from the current policy, not stored: a snapshot that is
today's `daily` survivor becomes a `weekly` one as newer snapshots age out, so the report
is a **point-in-time view**, not a persisted label. The verdicts are derived from the
exact same `_bucket_attribute` loop retention uses, so `--report` and a real prune can
never disagree — a `PRUNE` here is precisely what the next run would delete. (Folder names
are deliberately **not** renamed to reflect tier: the `date-offset-num-uuid` name is
load-bearing for date/num parsing and UUID correlation.)

Logging is `[STEP]/[INFO]/[OK]/[WARN]/[ERROR]` to the terminal (coloured on a tty) and
to `/var/log/snappersend.log` (override `$SNAPPERSEND_LOG`). The full-send fallback is
always logged at `INFO` with its reason. Each successful transfer logs its kind and
duration (`received #N -> …/snapshot (incremental, 1.2s)`), and when mbuffer is in the
pipe its end-of-run summary is logged too (`transfer stats: 5120 kiByte in 0.4sec -
average of 12 MiB/s`) — the byte count is the stream size, i.e. the **changed data**
actually shipped for an incremental. snappersend exits non-zero if any subvolume fails,
leaving the chains intact for retry. A `flock` (`/var/lock/snappersend.lock`) prevents
overlapping runs.

All remote commands share one **multiplexed SSH connection** (OpenSSH
`ControlMaster`/`ControlPersist`): the first command pays the TCP + key handshake, every
later command rides the same master as a ~10ms channel. A run or `--report` issues
dozens of small remote commands, so without this the handshakes dominated wall-clock
(~0.4s each). The master exits on its own (60s idle) and is also closed explicitly when
snappersend exits; it works with the `restrict`-hardened transport key, which denies
pty/forwarding but not exec sessions.

## Install / first run

snappersend installs on the **source only** — the destination runs no snappersend code.
Every remote action is a stock command (`btrfs receive`, `mkdir`, `ls`, …) invoked as
`ssh <transport-user> "sudo <command>"`, so the destination needs just a little config,
which `snappersend setup-dest` writes for you over your own admin login.

### 1. Source (the sender) — as root

```sh
sudo install -m 0755 snappersend /usr/bin/snappersend
sudo apt-get install -y python3-dotenv btrfs-progs openssh-client mbuffer rsync
sudo install -d -m 0755 /etc/snappersend
sudo cp -n config.example /etc/snappersend/config
sudoedit /etc/snappersend/config     # set SERVER_HOST + SUBVOLUMES (setup-dest can also
                                      # create the config for you if it's absent)
```

### 2. Provision the destination — one command

From the source, point `setup-dest` at **your own** sudo-capable login on the destination
(`admin@host` — not the transport user). It runs as root locally but reaches the
destination as *you*, like a privileged `ssh-copy-id`:

```sh
sudo snappersend setup-dest admin@backup-host
```

That single command: generates the transport key on the source (if absent) and pins the
destination's host key; then, over your admin SSH session, creates the dedicated transport
user, installs its `restrict`-hardened `authorized_keys` line and **one** scoped
`/etc/sudoers.d/snappersend` (with the destination's own binary paths, `visudo`-validated),
and ensures the receive directory; then verifies the whole transport end-to-end and prints
`verify: OK`. It ships **no files** to the destination — the entire remote footprint is one
system user, one sudoers file, and one key line. If your admin account's `sudo` on the
destination needs a password, it is prompted for on your terminal (run it interactively).

### 3. Seed

```sh
sudo snappersend --dry-run     # read both sides, change nothing — check the plan
sudo snappersend               # seed (full send per subvol; do it over a wired link)
```

The seed run logs `no shared parent with destination — full send` for each subvolume
(expected — there's no parent clone yet). After it, `/.snappersend/<subvol>/` holds the
seed clone, and every later run is a small incremental off the preserved parent.

### Security model & uninstalling the destination

The transport is least-privilege: the scoped `/etc/sudoers.d/snappersend` is the security
boundary (the key can only run the exact receive/list/delete commands snappersend issues,
as root), and `restrict` on the `authorized_keys` line denies the key a pty and any
forwarding. There is deliberately **no forced-command wrapper script** on the destination —
one fewer file to install, rot, or forget on removal.

To cleanly remove the destination transport config (transport user + its
`authorized_keys`, and the sudoers file), run from the source:

```sh
sudo snappersend decom-dest admin@backup-host        # received data is PRESERVED
```

`decom-dest` never touches the received backups — snappersend deliberately offers no
way to delete them, so a mistyped command can never destroy your backup history.
Removing the received data after decommissioning is your job, done by hand on the
destination (the received snapshots are Btrfs subvolumes:
`btrfs subvolume delete` them, then remove the plain wrapper dirs). If the
`setup-dest`/`verify` transport check fails: `Permission denied (publickey)` means the key
wasn't authorized (re-run `setup-dest`); `Host key verification failed` means the pinned
host key changed; a hang then `Connection timed out` means a wrong `SERVER_HOST`/`SSH_PORT`
or a firewall.

### systemd wiring — run right after each Snapper timeline snapshot

Snapper (on Debian) has no native "run a script after a snapshot" hook, so snappersend
attaches to what *creates* the snapshots: the **`snapper-timeline.service`**. The wiring
is deliberately **decoupled** — a separate `snappersend.service` ordered strictly *after*
the timeline snapshot, pulled in by a drop-in on the timeline service:

`/etc/systemd/system/snappersend.service` — the run itself:

```ini
[Unit]
Description=Replicate latest Snapper snapshot via btrfs send/receive
After=snapper-timeline.service          # order after the snapshot; NOT Requires=
Wants=network-online.target
After=network-online.target
[Service]
Type=oneshot
ExecStart=/usr/bin/snappersend
```

`/etc/systemd/system/snapper-timeline.service.d/10-snappersend.conf` — the trigger:

```ini
[Unit]
Wants=snappersend.service               # pull us in each timeline run…
Before=snappersend.service              # …ordered after the snapshot completes
```

The boot tier hangs off the **same** timeline service, on the other side: a
`bootmirror-sync.service` oneshot ordered strictly *before* the snapshot (so snapper
captures a fresh `/.bootmirror` in the same tick), pulled in by its own drop-in.

`/etc/systemd/system/bootmirror-sync.service` — the pre-tick mirror refresh:

```ini
[Unit]
Description=SnapperSend: refresh /.bootmirror before snapper timeline
Before=snapper-timeline.service
# NO Requires/BindsTo — ordering only.
[Service]
Type=oneshot
ExecStart=/usr/bin/snappersend --bootmirror-sync
TimeoutStartSec=90
```

`/etc/systemd/system/snapper-timeline.service.d/20-bootmirror.conf` — its trigger:

```ini
[Unit]
Wants=bootmirror-sync.service
```

```sh
sudo install -m0644 systemd/snappersend.service systemd/bootmirror-sync.service \
     /etc/systemd/system/
sudo install -Dm0644 systemd/snapper-timeline.service.d/10-snappersend.conf \
     /etc/systemd/system/snapper-timeline.service.d/10-snappersend.conf
sudo install -m0644 systemd/snapper-timeline.service.d/20-bootmirror.conf \
     /etc/systemd/system/snapper-timeline.service.d/20-bootmirror.conf
sudo systemctl daemon-reload
# verify the ordering graph:
systemctl show snapper-timeline.service -p Wants -p Before -p After | grep -E 'snappersend|bootmirror'
systemctl show snappersend.service -p After | grep snapper-timeline
systemctl show bootmirror-sync.service -p Before | grep snapper-timeline
```

So one timeline tick runs, in order: `bootmirror-sync.service` (refresh the mirror) →
`snapper-timeline.service` (snapshot `@`, `@home`, `@bootmirror` in one epoch) →
`snappersend.service` (replicate them all). Both couplings are `Wants=`-soft: a
failure on either side never propagates into snapper's own health, and snapper never
waits on the sync's exit status — a wedged sync is cut off by `TimeoutStartSec` and
the tick proceeds with a one-tick-stale mirror.

Why this shape:

- **`Wants=`, not `Requires=`** — snappersend still runs on the hours Snapper's
  `--timeline` no-ops (it just finds nothing new and exits 0), and a snappersend failure
  (e.g. a transient network blip to the destination, a legitimate non-zero exit) **never
  propagates back** to mark `snapper-timeline.service` itself as failed. Snapper's health
  reflects snapshots; snappersend's health reflects the send. They stay independent.
- **A separate unit ordered `After=`, not `ExecStartPost=`** on the timeline unit — the
  packaged timeline unit is `Type=simple`, so an `ExecStartPost=` would race the
  snapshot's completion and couple snappersend's exit status into Snapper's service
  health. The separate-unit design avoids both.
- **No `[Install]`/own timer** — `snappersend.service` is triggered *only* by the
  timeline service's `Wants=`; a second timer would be redundant (snappersend is
  newest-only, once per snapshot cadence). Don't `enable` it. The `flock` guards against
  a run overrunning into the next trigger.

Installs the units to fire snappersend once per timeline snapshot; drive it by hand any
time with `sudo systemctl start snappersend.service` or `sudo snappersend`.

### Desktop notification on failure (optional)

snappersend ships **no** notification code — failure *detection* is already
systemd-native (`snappersend.service` exits non-zero on failure, lands in the journal, and
shows in `systemctl --failed`). This section adds a **desktop toast** on top, wired
entirely separately from snappersend via a standard systemd `OnFailure=` unit, so you can
add or remove it without touching snappersend itself.

The one real subtlety: an `OnFailure=` unit runs as **root with no graphical session**, so
a bare `notify-send` does nothing. The dispatcher below crosses into each logged-in user's
D-Bus session so the toast actually appears. If you're on a headless box (or logged out),
it simply no-ops — the failure is still in the journal.

**1. Install `notify-send`** (from libnotify) — you also need a notification daemon
running in your desktop session; GNOME/KDE have one built in, otherwise run `dunst` or
`mako`:

```sh
sudo apt-get install -y libnotify-bin
```

**2. Install the dispatcher** — it finds every active login and shows the toast in that
user's session:

```sh
sudo tee /usr/bin/snappersend-notify >/dev/null <<'EOF'
#!/usr/bin/env bash
# snappersend-notify — pop a desktop toast into every logged-in graphical session.
# Called by an OnFailure= unit (root, no session of its own), so it must reach into
# each user's D-Bus. $1 = the systemd unit that failed (e.g. snappersend.service).
set -u
unit="${1:-snappersend.service}"
title="Backup failed: ${unit}"
body="snappersend reported a failure. Check:  journalctl -u ${unit} -e"

for uid in $(loginctl list-users --no-legend | awk '{print $1}'); do
    user=$(id -un "$uid" 2>/dev/null) || continue
    bus="/run/user/${uid}/bus"
    [ -S "$bus" ] || continue          # no session bus for this user -> skip (headless)
    sudo -u "$user" \
        XDG_RUNTIME_DIR="/run/user/${uid}" \
        DBUS_SESSION_BUS_ADDRESS="unix:path=${bus}" \
        notify-send -u critical -a snappersend -i dialog-error "$title" "$body" \
        2>/dev/null || true
done
exit 0
EOF
sudo chmod 0755 /usr/bin/snappersend-notify
```

**3. Add the notifier unit** — a small template unit whose instance name (`%i`) is the
unit that failed, so the same notifier is reusable for anything:

```sh
sudo tee /etc/systemd/system/snappersend-notify@.service >/dev/null <<'EOF'
[Unit]
Description=Desktop notification that %i failed

[Service]
Type=oneshot
ExecStart=/usr/bin/snappersend-notify %i
EOF
```

**4. Wire it into snappersend via a drop-in** (an admin drop-in in `/etc` applies whether
snappersend was installed manually or from the `.deb`, without editing the shipped unit):

```sh
sudo install -d /etc/systemd/system/snappersend.service.d
sudo tee /etc/systemd/system/snappersend.service.d/50-notify.conf >/dev/null <<'EOF'
[Unit]
OnFailure=snappersend-notify@%n.service
EOF
sudo systemctl daemon-reload
```

**5. Test it.** Three checks, cheapest first.

*a) The dispatcher itself* — while sitting at your desktop, a toast should appear
immediately:

```sh
sudo /usr/bin/snappersend-notify snappersend.service
```

*b) The notifier under systemd* — start the notifier unit the way `OnFailure=` will (as
root, with no session of its own): this is the real test that the toast still reaches your
session bus.

```sh
sudo systemctl start snappersend-notify@snappersend.service.service
sudo journalctl -u 'snappersend-notify@*' -n 20 --no-pager   # shows the notify-send it ran
```

*c) The full `OnFailure=` link* — make `snappersend.service` actually fail so systemd fires
the notifier. **Note:** `SNAPPERSEND_SERVER=… systemctl start snappersend.service` does
**not** work — `systemctl start` runs the unit with the *unit's* environment, not your
shell's, so the variable never reaches it (it only takes effect when you run the
`snappersend` binary directly). Instead, drop in a one-off failing override:

```sh
# temporary: force the service to exit non-zero
printf '[Service]\nExecStart=\nExecStart=/bin/false\n' | \
  sudo tee /etc/systemd/system/snappersend.service.d/99-test-fail.conf >/dev/null
sudo systemctl daemon-reload
sudo systemctl start snappersend.service                       # fails on purpose
systemctl is-failed snappersend.service                        # -> failed
sudo journalctl -u 'snappersend-notify@*' -n 20 --no-pager     # -> the notifier fired

# undo the test override
sudo rm -f /etc/systemd/system/snappersend.service.d/99-test-fail.conf
sudo systemctl daemon-reload
sudo systemctl reset-failed snappersend.service
```

Adapt freely: change the wording/urgency, add a second `OnFailure=` for e-mail, or drop
`-i dialog-error` if your daemon doesn't theme icons. Nothing here is a snappersend
dependency — remove `50-notify.conf` (and `daemon-reload`) to turn it all off.

> **Not yet included (a later pass):** the **missed-run watchdog** (for a source that was
> powered off across several timeline ticks). snappersend writes a success stamp
> (`/var/lib/snappersend/last-success`) the future watchdog will read. The timeline-driven
> systemd trigger above **is** now shipped.

## Tests

```sh
SNAPPERSEND_QUIET=1 python3 -m pytest test_snappersend.py -q
```

The suite covers the parent-tree invariant (failed send leaves the parent unchanged;
success promotes and prunes; diverged destination → full send + reseed; an **unreachable
destination is not treated as divergence** — the parent tree is left intact and the next
run recovers incrementally; crash-after-send-before-promote is safe), WYSIWYG GFS
retention including the yearly tier (and that no source-backed snapshot survives beyond
GFS), Snapper-schema config parsing, the carried-over correctness logic
(valid-received detection, UUID correlation, receive-in-place, run-lock collision),
and the bootmirror sync (above all that it is **source-local**: any attempt to run a
remote command fails the test; plus the nested-ESP exclude, the vfat mtime window,
and rc-24 tolerance).
