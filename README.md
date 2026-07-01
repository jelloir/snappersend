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
3. **Make the source reach the destination over the transport key.** snappersend runs as
   **root** and connects with `BatchMode=yes` (see `ssh_argv`), so it never answers
   prompts: the destination's **host key must already be pinned in root's
   `known_hosts`**, and the private key must authenticate non-interactively. Do it once:

   ```sh
   # Load the values you just configured (root's shell — that's who snappersend runs as).
   eval "$(sudo grep -E '^(SERVER_HOST|SSH_PORT|SERVER_USER|SSH_KEY|RECV_BASE)=' \
           /etc/snappersend/config | sed 's/[[:space:]]*#.*//')"
   : "${SSH_PORT:=22}" "${RECV_BASE:=/srv/snapshots-recv}"

   # 3a. Pin the destination's host key in ROOT's known_hosts (fetch it, then VERIFY the
   #     fingerprint out-of-band against the destination before you trust it).
   sudo install -d -m 0700 /root/.ssh
   ssh-keyscan -p "$SSH_PORT" "$SERVER_HOST" 2>/dev/null | sudo tee -a /root/.ssh/known_hosts >/dev/null
   sudo ssh-keygen -F "$SERVER_HOST" -l          # prints the fingerprint you just pinned

   # 3b. Transport key. Reuse di-snapsend's key if it's already there; only generate one
   #     if you're setting up fresh. The destination's authorized_keys forced-command +
   #     scoped sudoers are part of the di-snapsend destination provisioning (reused as-is
   #     — a plain `ssh-copy-id` would drop the forced command, so add the PUBLIC half to
   #     the transport user's authorized_keys WITH the command="…" prefix, not bare).
   sudo test -f "$SSH_KEY" && echo "reusing $SSH_KEY" || \
     sudo ssh-keygen -t ed25519 -N '' -C snappersend -f "$SSH_KEY"

   # 3c. Prove the whole path — host key + key auth + forced command + remote sudo.
   #     `ls` is inside the forced-command allowlist, so a clean exit 0 means you're set:
   sudo ssh -i "$SSH_KEY" -p "$SSH_PORT" -o BatchMode=yes -o ConnectTimeout=10 \
        "$SERVER_USER@$SERVER_HOST" "sudo ls -1d $RECV_BASE"
   ```

   Reading the result of 3c:
   - **exit 0, prints the path** → good, snappersend can transport.
   - `Host key verification failed` → 3a didn't run (or the key changed); re-pin.
   - `Permission denied (publickey)` → the public half isn't in the transport user's
     `authorized_keys` on the destination (see 3b).
   - hangs then `Connection timed out` → wrong `SERVER_HOST`/`SSH_PORT` or a firewall.
4. **First run** does a **full send** per subvolume, which **seeds the parent tree**.
   Do the seed over a wired link if you can; subsequent incrementals are small.
   ```sh
   sudo snappersend --dry-run     # check the plan
   sudo snappersend               # seed
   ```

The first run logs `no shared parent with destination — full send` for each subvolume
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
