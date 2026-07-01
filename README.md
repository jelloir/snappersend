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
snappersend --report        # read-only status view (see below); change nothing
snappersend --subvol home   # just one subvolume
snappersend --config PATH   # alternate config (default /etc/snappersend/config)
snappersend --no-mbuffer    # don't pipe through mbuffer
snappersend --skip-boot     # skip the /boot + /boot/efi rsync tier
snappersend -v              # verbose: log every shell command
```

### `--report` — read-only status & health view

`--report` prints, per subvolume, how the destination snapshots and source parent clones
stand **right now** — it **changes nothing** (no sends, clones, deletes, renames, property
writes, or success stamp) and takes no run lock, so it is safe to run alongside a real
replication. It honours `--subvol` and `--server`, and each subvol's own
`retention_for()` policy. For each subvol it shows:

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
always logged at `INFO` with its reason. snappersend exits non-zero if any subvolume
fails, leaving the chains intact for retry. A `flock` (`/var/lock/snappersend.lock`)
prevents overlapping runs.

## Install / first run

snappersend is a single Python script plus its config. The destination gets a
least-privilege transport: a dedicated `snappersend` user, a scoped sudoers rule, and a
forced-command SSH filter, so the source key can only ever run the exact
send/receive/rsync commands snappersend issues — nothing else, even if the key leaks.

There's a small chicken-and-egg: the **destination** needs the **source's** public key,
but that key doesn't exist until you set up the source. So the order is: prep the
destination → set up the source (which generates the key) → authorize that key on the
destination → verify and seed. Each block below is copy-paste; run it from a checkout of
this repo on the machine named in the heading.

### 1. Destination (the receiver) — as root

```sh
SNAP_USER=snappersend                    # transport user; match SERVER_USER in the config
RECV_BASE=/srv/snapshots-recv            # MUST be on a Btrfs filesystem; match RECV_BASE

# a) least-privilege transport user + receive area
sudo useradd --system --create-home --shell /bin/bash "$SNAP_USER" 2>/dev/null || true
sudo mkdir -p "$RECV_BASE"
sudo chown "$SNAP_USER:$SNAP_USER" "$RECV_BASE"

# b) scoped sudoers — exactly the commands snappersend runs remotely, nothing more
#    (if you changed SNAP_USER above, edit the username on the grant line to match)
sudo tee /etc/sudoers.d/snappersend >/dev/null <<'EOF'
Cmnd_Alias SNAPPERSEND_CMDS = \
    /usr/bin/btrfs receive *, \
    /usr/bin/btrfs subvolume show *, \
    /usr/bin/btrfs subvolume delete *, \
    /usr/bin/btrfs property get *, \
    /usr/bin/mkdir -p *, \
    /usr/bin/rmdir *, \
    /usr/bin/ls *, \
    /usr/bin/ln -sfn *, \
    /usr/bin/rsync *
snappersend ALL=(root) NOPASSWD: SNAPPERSEND_CMDS
Defaults!SNAPPERSEND_CMDS !requiretty
EOF
sudo visudo -cf /etc/sudoers.d/snappersend           # must print "parsed OK"

# c) forced-command filter (defense in depth on top of sudoers)
sudo install -m 0755 snappersend-ssh-filter /usr/local/bin/snappersend-ssh-filter

# d) create the transport user's .ssh (its key is authorized in step 3)
sudo install -d -m 0700 -o "$SNAP_USER" -g "$SNAP_USER" "/home/$SNAP_USER/.ssh"
```

### 2. Source (the sender) — as root

```sh
# a) install snappersend, its one dependency, and the config
sudo install -m 0755 snappersend /usr/local/bin/snappersend
sudo apt-get install -y python3-dotenv btrfs-progs openssh-client mbuffer rsync
sudo install -d -m 0755 /etc/snappersend
sudo cp -n config.example /etc/snappersend/config
sudoedit /etc/snappersend/config          # set SERVER_HOST + SUBVOLUMES (+ SERVER_USER/RECV_BASE if you changed them)

# b) load your config values, generate the transport key (root owns it — snappersend runs as root)
eval "$(sudo grep -E '^(SERVER_HOST|SSH_PORT|SERVER_USER|SSH_KEY|RECV_BASE)=' \
        /etc/snappersend/config | sed 's/[[:space:]]*#.*//')"
: "${SSH_PORT:=22}"
sudo install -d -m 0700 "$(dirname "$SSH_KEY")"
sudo test -f "$SSH_KEY" || sudo ssh-keygen -t ed25519 -N '' -C "snappersend@$(hostname)" -f "$SSH_KEY"

# c) pin the destination's host key in ROOT's known_hosts (snappersend connects with
#    BatchMode=yes and never answers prompts, so trust must be pre-recorded, as root)
sudo install -d -m 0700 /root/.ssh
sudo sh -c "ssh-keyscan -p '$SSH_PORT' -t ed25519 '$SERVER_HOST' >> /root/.ssh/known_hosts"

# d) print the PUBLIC key — copy this line; you'll authorize it on the destination next
sudo cat "$SSH_KEY.pub"
```

### 3. Destination — authorize the source's key (as root)

Paste the public key printed by step 2d into `PUBKEY` below, then run it on the destination:

```sh
PUBKEY='ssh-ed25519 AAAA...snappersend@source'          # <-- the line from step 2d
OPTS='command="/usr/local/bin/snappersend-ssh-filter",no-pty,no-agent-forwarding,no-port-forwarding,no-X11-forwarding'
echo "$OPTS $PUBKEY" | sudo tee -a /home/snappersend/.ssh/authorized_keys >/dev/null
sudo chown snappersend:snappersend /home/snappersend/.ssh/authorized_keys
sudo chmod 0600 /home/snappersend/.ssh/authorized_keys
```

### 4. Source — verify, then seed (as root)

```sh
# prove the whole path in one shot: host key + key auth + forced command + remote sudo.
# `ls` is in the allowlist, so a clean exit 0 printing the recv path means you're set.
sudo ssh -i "$SSH_KEY" -p "$SSH_PORT" -o BatchMode=yes -o ConnectTimeout=10 \
     "$SERVER_USER@$SERVER_HOST" "sudo ls -1d $RECV_BASE"

sudo snappersend --dry-run     # read both sides, change nothing — check the plan
sudo snappersend               # seed (full send per subvol; do it over a wired link)
```

If step 4's `ssh` test doesn't print the path:
- `Host key verification failed` → step 2c didn't run, or the destination's key changed — re-pin.
- `Permission denied (publickey)` → the key isn't authorized on the destination — recheck step 3.
- `snappersend-ssh-filter: rejected command` → auth and host key are fine; you just ran a
  command outside the allowlist (expected for anything but the `sudo ls` test).
- hangs then `Connection timed out` → wrong `SERVER_HOST`/`SSH_PORT`, or a firewall.

The seed run logs `no shared parent with destination — full send` for each subvolume
(expected — there's no parent clone yet). After it, `/.snappersend/<subvol>/` holds the
seed clone, and every later run is a small incremental off the preserved parent.

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
ExecStart=/usr/local/bin/snappersend
```

`/etc/systemd/system/snapper-timeline.service.d/10-snappersend.conf` — the trigger:

```ini
[Unit]
Wants=snappersend.service               # pull us in each timeline run…
Before=snappersend.service              # …ordered after the snapshot completes
```

```sh
sudo install -m0644 systemd/snappersend.service /etc/systemd/system/
sudo install -Dm0644 systemd/snapper-timeline.service.d/10-snappersend.conf \
     /etc/systemd/system/snapper-timeline.service.d/10-snappersend.conf
sudo systemctl daemon-reload
# verify the ordering graph:
systemctl show snapper-timeline.service -p Wants -p Before | grep snappersend
systemctl show snappersend.service -p After | grep snapper-timeline
```

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

> **Desktop notification (optional — not part of snappersend).** snappersend deliberately
> ships **no** notification code: failure *detection* is already systemd-native
> (`snappersend.service` exits non-zero on failure and lands in the journal /
> `systemctl --failed`). If you want a desktop toast, wire a **standalone** `OnFailure=`
> unit — kept entirely separate from snappersend — that calls a small dispatcher. The one
> real gotcha: an `OnFailure=` unit runs as **root with no graphical session**, so a bare
> `notify-send` no-ops; it must cross into the logged-in user's session bus:
> ```sh
> # in the dispatcher, after discovering the active user + uid:
> sudo -u "$user" \
>     DBUS_SESSION_BUS_ADDRESS="unix:path=/run/user/${uid}/bus" \
>     notify-send -u critical "snappersend failed" "See: journalctl -u snappersend.service"
> ```
> When no graphical session is logged in the toast simply doesn't show — nothing is lost,
> since the failure is already in the journal and surfaces next time you're at the
> machine. This is a starting point you adapt, not something snappersend depends on.

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
GFS), Snapper-schema config parsing, and the carried-over correctness logic
(valid-received detection, UUID correlation, receive-in-place, run-lock collision).
