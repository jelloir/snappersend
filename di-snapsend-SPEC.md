# di-snapsend — Design Specification & Implementation Brief

**Status:** Design complete, implemented.
**Audience:** implementers + reviewers.
**Companion file:** `di-snapsend-DESIGN.py` (annotated skeleton — the structural
contract; this document is the *why* and the worked examples behind it).

**Changelog:**
- **2026-06-29 — added optional `keep_hourly` retention tier.** A fourth, finest GFS
  tier above daily, so the destination can match Snapper's hourly granularity and quiet
  the "source holding N extra" INFO line for the hourly band (§9). Existing configs
  without the key are **unchanged** (hourly disabled, `0`). New installs default to
  `keep_hourly = 24`. Additive only — correlation, transfer, naming, Option B, and the
  override-logging logic are untouched.

---

## 1. What this tool is

`di-snapsend` is a **thin orchestration layer** that replicates Snapper-created
Btrfs snapshots from a **source** host (the sending side — typically a
workstation/laptop) to a **destination** host (the receiving side — typically an
always-on server) over SSH. It is a standalone tool: the on-host
disaster-recovery / snapshot-history layer of a 3-2-1 backup strategy.

It is a peer to btrbk and snbk in *role*, but deliberately minimal: it does
**not** reimplement the Btrfs stream format or network transport. It shells out
to the tools that already solve those problems:

```
btrfs send [-p PARENT] SRC | [mbuffer |] ssh DEST "sudo btrfs receive DST"
```

btrfs does the filesystem + stream work. ssh does the (encrypted) network. This
tool does only the three things a wrapper actually has to get right:

1. **Enumeration** — read Snapper's snapshots directly off disk (no format
   guessing), and inventory what the destination already holds.
2. **Parent selection** — pick the correct `-p` parent for incrementals.
3. **Cleanup + retention** — detect and delete partial transfers; prune the
   destination per policy without ever breaking the incremental chain.

### Why build it rather than use btrbk or snbk

| Option | Blocker |
|--------|---------|
| btrbk | Config DSL doesn't map cleanly onto Snapper's `<N>/snapshot` nesting — hit in practice (sent nothing). Powerful but heavy (7k lines Perl). |
| snbk | Mirror-only retention; requires an out-of-Debian OBS repo on the source (snapper ≥ 0.12; Debian Trixie ships 0.10.6). |
| **di-snapsend** | Reads Snapper's own layout directly; stays in Debian main (needs only `btrfs-progs` + `openssh`, both in main); retention is plain Python we control. |

Aligned with the project's values: minimalism, "do one thing well", Debian-main
containment, and fully reasoning about our own tools.

---

## 2. Source layout assumptions (Snapper on Btrfs)

di-snapsend reads Snapper's snapshots **directly off disk** on the source. For
each replicated subvolume, Snapper exposes its snapshots at the subvolume's
mountpoint as:

```
<mountpoint>/.snapshots/<N>/
├── snapshot/      <- the read-only Btrfs subvolume we send
└── info.xml       <- Snapper metadata (number, timestamp, pre/post type)
```

So for `root` (mountpoint `/`) the tool reads `/.snapshots/<N>/snapshot`, and for
`home` (mountpoint `/home`) it reads `/home/.snapshots/<N>/snapshot`. The on-disk
Btrfs nesting may differ between subvolumes — e.g. root's `.snapshots` is often a
**top-level** subvolume while home's is **nested** under `@home` — but this does
**not** change the read logic: both expose `<N>/snapshot` at their mountpoint.
The set of subvolumes to replicate (and their mountpoints) is configured in
`[subvolumes.*]`; the tool assumes nothing beyond "Snapper's `<N>/snapshot`
layout exists at the configured mountpoint."

### `btrfs subvolume show` — the fields the rules depend on

A source snapshot's `btrfs subvolume show` looks like:

```
UUID:            <source-uuid>
Parent UUID:     <live-subvol-uuid>
Received UUID:   -
Flags:           readonly
```

Parsing facts the engine relies on:
- `Flags:` contains the literal substring `readonly` for RO subvols →
  `"readonly" in flags_line.lower()` is a reliable test.
- Source snapshots have `Received UUID: -` (they're locally created). After a
  send, the **destination's** copy carries `Received UUID = <source UUID>` — this
  is the correlation link (Rule 2).
- `Parent UUID` is the live subvolume and is typically identical across all of a
  subvolume's snapshots → it is **useless for ordering**. Use the Snapper number
  for within-machine age ordering.

### Source-side retention is Snapper's, and irregular by design

Snapper owns the source retention and will gap/churn the snapshot numbering as it
prunes (expected — see §4; correlation is by UUID, not number). Snapper configs
also produce **pre/post pairs** (from the apt hook) and **non-contiguous numbers**;
both are handled by enumerating what's actually on disk rather than assuming a
contiguous range. How source (Snapper) retention and destination (di-snapsend)
retention combine is covered in §9.

---

## 3. The correctness model (the non-negotiable rules)

These three rules are distilled from reading btrbk's source. Each exists because
a naive "send | receive; delete if it looks broken" script is unsafe. The
skeleton encodes all three; this section is the rationale + worked examples so
the implementation cannot drift.

### Rule 1 — Validity of a received subvolume

**A subvolume existing on the target does NOT mean the transfer completed.**
`btrfs receive` is not atomic and btrfs-progs does **not** auto-delete a failed
receive (confirmed in btrbk source comment, `btrfs:1590`).

| State | `readonly` | `received_uuid` | Meaning |
|-------|-----------|-----------------|---------|
| Clean | `true` | set (not `-`) | Transfer completed; safe to use as parent |
| Garbled | `false` | `-` | Partial/interrupted; **we must delete it** |

After every receive, run `btrfs subvolume show` on the landed path and check
**both** conditions. If garbled, `btrfs subvolume delete` it on the destination and
treat the transfer as failed. Never advance the parent pointer past an
unverified transfer.

Encoded as: `Subvol.is_valid_received` and `Subvol.is_garbled`.

### Rule 2 — Parent eligibility ("correlation")

A source snapshot `S` may be the `-p` parent for an incremental **only if** the
destination already holds a snapshot correlated with `S` (same Btrfs lineage). Both
must be read-only. Correlation holds when **any** of:

```
S.uuid          == T.received_uuid     (T was received from S)
T.uuid          == S.received_uuid
S.received_uuid == T.received_uuid     (both received from a common source)
   AND S.received_uuid != '-'
```

(from btrbk `_is_correlated`, `btrfs:2587`)

**Worked example with real data.** Source home snapshot #1 has
`uuid = a2159d69-…`, `received_uuid = -`. After we send it, the destination's copy
will have `received_uuid = a2159d69-…`. On the next run, `is_correlated(source#1,
dest_copy)` returns true via the first clause (`S.uuid == T.received_uuid`).
So source #1 is a valid parent for sending source #2 incrementally. ✓

**Parent choice:** the parent for sending snapshot `N` is the **newest** source
snapshot that is (a) strictly older than `N` (lower Snapper number) and (b)
correlated with something on the destination. If none → full send.

Encoded as: `is_correlated()` and `choose_parent()`.

### Rule 3 — Retention prune guard

Retention must **never** delete the snapshot that is the current newest
correlated pair, on **either** side — it is the parent the next incremental
depends on. Delete it and the next run silently degrades to a full send (or
fails outright).

Compute the newest correlated `(source, target)` pair first and **pin** it;
build delete sets that honour the keep-policy **and** exclude the pinned pair.

Encoded as: `_newest_correlated_pair()` (pin), consumed by `apply_retention()`.

**Invariant this imposes on scheduling:** source retention must always leave at
least one snapshot the destination also holds, to serve as parent. With Snapper
keeping 48 hourly and this tool running hourly, that is satisfied with wide
margin. If the source were offline for >48h, the next run would correctly fall
back to a full send (no breakage, just a bigger transfer).

**Replication is event-aligned in addition to the hourly timer.** As of the apt-hook
addition, a replication run also fires immediately after every apt transaction (an
`apt.conf.d` `DPkg::Post-Invoke` hook starting the same `snapsend.service`), after
Snapper's apt post-snapshot has been taken. This is what captures the dangerous case — a
kernel/grub/initramfs change written by package postinst scripts — promptly and
boot-aligned, instead of up to ~an hour later (the timer's `RandomizedDelaySec`),
decoupled from the matching `@` snapshot and so newer than the newest replicated snapshot
in the live `/boot` rsync tier (§11). The apt hook **only adds runs**; it never removes
the periodic baseline. The hourly cadence and the 48-hourly parent margin reasoning above
are therefore **unchanged** — the baseline still bounds the incremental-parent age; the
hook just tightens alignment for OS/boot changes. The one accepted residual is a purely
manual, non-apt boot change (e.g. a hand `update-grub`): not caught until the next
snapshot/timer tick — a tiny, self-healing window. The hook starts the existing service,
so the engine's `flock` makes a collision with an in-flight (timer-started) run a clean
no-op (`_AlreadyRunning`).

**Why the apt hook uses `DPkg::Post-Invoke` (and ordering by filename sort).** The
replication hook (`apt/95snapsend-replicate`) fires at `DPkg::Post-Invoke` — the **same**
stage the snapper apt integration (`80snapper` / `81snapper-enhanced`) uses. The original
design used `DPkg::Post-Invoke-Success` (to gate on a clean transaction), but **VM testing
on apt 3.0.x (Debian 13 / trixie) proved that stage is never executed for normal package
operations** — `apt install` / `reinstall` / `remove` all complete without running any
`DPkg::Post-Invoke-Success` hook, so a hook placed there silently never fires. (The string
still exists in `libapt-pkg`; apt 3.0's `pkgDPkgPM::Go()` only invokes `DPkg::Post-Invoke`,
and whatever path still wires `-Success` is not reached by ordinary front-end operations.)
`DPkg::Post-Invoke` runs reliably — it is the very stage snapper depends on.

Because we now share snapper's stage, **ordering is by filename sort**, which apt applies
to the entries *within* a stage: `95snapsend-replicate` sorts after `80snapper` /
`81snapper-enhanced`, so snapper's post-snapshot is created (synchronously, in its hook)
before snapsend's entry starts the service. The replicated state is therefore the
consistent post-change `@`. This makes the `95`-vs-`80/81` filename relationship
**load-bearing**; the installer emits a warn-only check that our file sorts last and flags
the case where snapper is wired on a different stage (where filename sort would not order
us against it).

**No clean-transaction gating.** `Post-Invoke` runs on every transaction, success or
failure, and apt exposes **no** success/status signal to the hook (verified: the hook
environment carries only `DPKG_FRONTEND_LOCKED`). So unlike the original `-Success` intent
we do **not** gate on a clean transaction. This is benign and the safe direction: snapsend
only mirrors already-committed snapshots plus the live boot tier (it never propagates
anything irreversibly, and the destination retains history), snapper itself snapshots on
this same stage regardless of outcome, and the hourly timer would replicate the identical
state on its next tick anyway. The VM validation (an `apt reinstall` shows a
`snapsend.service` start in `journalctl`, with the snapper post-snapshot timestamp
preceding it) is the authoritative end-to-end confirmation.

---

## 4. The numbering / retention divergence (resolved)

**Concern:** once Snapper retention deletes old snapshots on the source, source
numbering gaps and diverges from what the destination holds (destination keeps long
retention). Won't this break matching?

**Resolution: it does not, because correlation is by UUID, never by number.**

Worked through with real state. Suppose source home currently holds 1–24 and all
are on the destination. Overnight, Snapper timeline cleanup deletes 1–10 on the
source (aged past the hourly window). Next run:

- Source holds 11–N. Destination holds 1–N (long retention).
- `choose_parent()` for the new snapshot finds the newest source snapshot whose
  **UUID** correlates with a destination copy. Source #11 still exists; its UUID still
  matches the destination's copy-of-#11 → valid parent.
- The deletion of 1–10 on the source is **invisible** to correlation, because
  the tool never looks up snapshots by number across machines. Numbers are used
  **only** for within-machine age sorting.

The divergence is the asymmetric retention working as intended (short on source,
long on destination). The single invariant (Rule 3 / §3) — keep at least one shared
snapshot as parent — is guaranteed by hourly operation against a 48-hourly
Snapper window.

### Destination-side layout & naming scheme (decided)

Every tier lives under a **per-host** subtree, so multiple machines share one
destination with zero collision. The host segment is `socket.gethostname()` — derived
automatically, never configured (it's a fact about the machine, not a preference),
and shared by the snapshot tiers and the boot tier so they never diverge:

```
<recv_base>/                                              e.g. /srv/snapshots-recv
└── <source-hostname>/                                         source-host/
    ├── home/<localdate>-<offset>-<num>-<short_uuid>/snapshot   home/20260627-2300+1000-8-a2159d69/snapshot
    ├── root/<localdate>-<offset>-<num>-<short_uuid>/snapshot
    ├── boot/                                                  (boot tier — §11)
    └── boot-efi/
```

A subvol's destination is composed at runtime as
`<recv_base>/<hostname>/<subvol_name>`; each received snapshot then lives in a
per-transfer **directory** named `<localdate>-<offset>-<num>-<short_uuid>`, with
the subvol one level down (`btrfs receive` always names it `snapshot` — §5.2):

- `localdate`: the **source snapshot's own** timestamp (`Subvol.when`, a naive-UTC
  instant) rendered in the machine's **system local** time, `%Y%m%d-%H%M` —
  **colon-free** by design (ISO `HH:MM` colons are legal on Linux but break tooling
  and are awful to type; do NOT use `datetime.isoformat()`). The date **leads** so
  `ls` of the receive area sorts chronologically. A missing timestamp falls back to
  the literal `nodate`.
- `offset`: the local UTC offset at that instant (`%z`, e.g. `+1000`, `-0500`,
  `+0000`). It **disambiguates the DST fall-back hour** — when a local clock-time
  repeats, the offset differs (`+0100` vs `+0000`) so the two folder names never
  collide — and self-documents which zone the machine was in (useful across
  relocations). The **authoritative** time stays UTC (`when`); the name is a label.
- `num`: the source Snapper number **at send time** — human-readable, mirrors how
  Snapper presents snapshots.
- `short_uuid`: first 8 hex chars of the source `uuid` — the ultimate uniqueness
  backstop (every subvol is unique regardless of the date portion), and ties the
  directory name to the correlation key. Falls back to `nouuid` if absent.

The wrapper directory mirrors Snapper's own `<N>/snapshot` layout and lets every
snapshot be received directly into its final home (no subvolume `mv` — §5.2).
Enumeration reads the subvol at `<name>/snapshot`, recovers `num` from the
directory name, and parses the `<localdate><offset>` label back to a **naive-UTC
instant** into the subvol's `when`, so ordering/retention key on the
**source-snapshot time**. (This is deliberate: btrfs `Creation time` on a
*received* subvol is the **receive time**, not the source-snapshot time — verified
on Trixie's btrfs-progs — so the name is the only carrier of source time on the
target and must remain authoritative; a malformed name simply falls back to the
btrfs creation time without crashing.) Retention deletes the subvol then `rmdir`s
the wrapper.

The `<recv_dir>/<subvol>.latest` symlink always points at the newest received
subvol's `…/snapshot`, giving restic a stable target. This is our own equivalent
of btrbk's `latest` pointer.

### Pre/post pair handling (root only)

Root snapshots include apt-hook pre/post pairs. They send and correlate like any
other RO snapshot. The only refinement: **destination-side retention should avoid
orphaning half a pre/post pair** when *we* prune — if a `post` is kept, prefer
keeping its `pre` for restore sanity. This is a destination-retention nicety (Rule 3
territory), not a correctness requirement. Implement as a "keep pairs together"
check in `apply_retention()` for the root subvol.

---

## 5. Transfer pipeline (implementation detail)

### Full send (first time, or no correlated parent)
```
btrfs send SRC | [mbuffer |] ssh DEST "sudo btrfs receive RECV_DIR"
```

### Incremental (have correlated parent P)
```
btrfs send -p P.path SRC | [mbuffer |] ssh DEST "sudo btrfs receive RECV_DIR"
```

### Critical implementation requirements

1. **Capture every stage's exit code.** This is a multi-process pipe; a failure
   in `btrfs send` must not be masked by a `0` from `ssh`. Use `Popen` chaining
   and `wait()` on each process, checking all return codes (the
   `_run_pipe()` stub). Bash equivalent would be `PIPESTATUS`; in Python, hold
   references to each `Popen` and inspect `.returncode` after the final stage
   closes.

2. **Receive in place — no subvolume `mv`, ever.** Snapper's subvolume is
   literally named `snapshot`, so `btrfs receive DIR` always writes a child
   subvol named `DIR/snapshot`. To keep each received snapshot uniquely named on
   the destination, receive each one directly into its **own final per-transfer
   directory** (`RECV_DIR = <recv_base>/<hostname>/<subvol>`, composed at runtime
   — §4):
   - `final_dir = RECV_DIR/<date>-<num>-<short_uuid>`  (date = `%Y%m%d-%H%M`, §4)
   - `ssh ... sudo mkdir -p final_dir`
   - `btrfs send [-p P] SRC | [mbuffer |] ssh ... sudo btrfs receive final_dir`
   - the subvol lands as `final_dir/snapshot` and is **never moved**.
   - Verify Rule 1 on `final_dir/snapshot` (readonly **and** `received_uuid`
     set). On garble/failure: `btrfs subvolume delete final_dir/snapshot` then
     `rmdir final_dir`, and treat the transfer as failed.
   - On success: repoint `.latest` at `final_dir/snapshot`.

   **Why not stage-then-`mv`?** A received subvol is read-only with
   `received_uuid` set — that RO + `received_uuid` state *is* the valid-receive
   signature Rule 1 checks and the correlation key Rule 2 depends on. `mv`-ing it
   across directories falls back to a recursive copy (the source RO subvol can't
   be written into the destination → `Read-only file system`), and flipping it to
   RW to work around that **resets `received_uuid`** (needs `--force`), destroying
   the correlation key. Receiving in place avoids both: the subvol is created at
   its final home and only ever read thereafter. Partial-transfer safety is
   preserved — a killed receive leaves a detectable garbled `final_dir/snapshot`
   that Rule 1 rejects and cleanup deletes (subvol + `rmdir`) before anything
   references it. (This is a deliberate naming-contract change: the layout is now
   `<num>-<short_uuid>/snapshot`, mirroring Snapper's own `<N>/snapshot` nesting —
   see §4.)

3. **`.latest` symlink** updated only after a verified-clean receive, pointing at
   the received subvol `final_dir/snapshot` (restic backs up the subvol's
   contents, so the target is the subvol, not the wrapper directory).

4. **mbuffer optional** (`cfg.use_mbuffer`) — smooths throughput and gives a
   progress/rate readout over WiFi. Insert between send and ssh when enabled.
   Tool must function without it (don't hard-require the package).

5. **Seed transfer.** The first full send may be large. Document running it over
   a wired link if available; incrementals are small over WiFi. (Operational
   note for the README, not a code requirement.)

---

## 6. Per-subvolume flow (already in skeleton: `replicate_subvol`)

```
1. ssh mkdir -p RECV_DIR
2. source_snaps = enumerate /home/.snapshots (or /.snapshots)   [RO only]
3. target_snaps = enumerate RECV_DIR on the destination
4. missing = source snaps not correlated with any target snap
5. sort missing oldest-first (so each can parent the next)
6. for each missing snap:
     parent = choose_parent(snap, source_snaps, target_snaps)   [Rule 2]
     ok = send_receive(...)                                      [Rule 1 verify]
     if not ok: STOP (leave chain intact for retry), mark failure
     else: refresh target_snaps (new parent now available)
7. if all ok: apply_retention(...)                              [Rule 3 pin]
```

Failure is **fail-safe**: stop at the first bad transfer, leave everything
already transferred intact, retry next run. Never prune on a failed run.

---

## 7. Implementation task list for Claude Code

Bodies to fill in the skeleton (search for `NotImplementedError` /
`TODO(claude-code)`):

1. **`_run_pipe(send_argv, ssh_argv, use_mbuffer)`** — piped exec with all-stage
   exit-code capture. The crux function. Return `True` iff every stage exits 0.

2. **`send_receive()`** — wire in the receive-in-place transfer (§5.2: receive
   directly into `<recv_dir>/<num>-<short_uuid>/`, no subvolume `mv`), call
   `_run_pipe`, run Rule 1 verification on `final_dir/snapshot`, handle garble
   cleanup (delete subvol + `rmdir`), update `.latest`.

3. **`apply_retention()`** — **target-only** (Decision 1): prune only the
   destination; never touch source snapshots (Snapper owns those). Implement
   `keep_hourly/daily/weekly/monthly` per-subvol from the config file, exclude the
   pinned pair (Rule 3), apply the pre/post "keep pairs together" check for
   root (§4), and (superset model, §9.1) keep every target whose source still
   exists — GFS thins only the source-aged-out long tail. Retention tiers are
   hourly/daily/weekly/monthly (hourly optional, default-disabled on upgrade). Buckets need
   real timestamps (from `info.xml` or subvol creation time), not just numbers —
   see task 5.

4. **`parse_subvolume_show()`** — already drafted and validated against real
   output; harden it (handle missing fields gracefully, cross-check `readonly`
   via `btrfs property get <path> ro` if `Flags:` parsing is ever ambiguous).

5. **`list_snapper_snapshots()`** — drafted; add `info.xml` timestamp parsing if
   timestamps are wanted for retention (the daily/weekly/monthly buckets need a
   real timestamp, not just the number — pull from `info.xml` `<date>` or the
   subvol creation time).

6. **`main()` + argparse** — three-tier precedence (CLI flag > env var > default,
   matching the suite convention), `--dry-run`, `--subvol <name>` filter,
   `--server <host>`. Build `Config`, loop `replicate_subvol` over `cfg.subvols`.

7. **Logging** — structured logging to `/var/log/snapsend.log` (the suite uses
   timestamped log files + coloured terminal helpers; mirror `di-*.sh` style:
   `[INFO]/[OK]/[WARN]/[ERROR]/[STEP]`).

8. **Locking** — a lockfile (e.g. `/var/lock/snapsend.lock` via `flock`) so two
   runs can't overlap. btrbk does this; cheap insurance.

9. **`boot_backup()`** — drafted; implement the rsync exec with exit-code
   handling (treat rc 0 and 24 as success on the FAT32 efi path; warn otherwise
   without aborting the run — boot tier is independent of the snapshot tiers).
   Runs once per host after the subvol loop. The restore side already exists in
   `di-btrfs-recovery.sh` — this only feeds it. (See §11.)

10. **systemd units + apt hook** — `snapsend.service` (oneshot) + `snapsend.timer`
    (hourly + randomized delay, `Persistent=true`) as the periodic baseline, plus a
    missed-run watchdog mirroring the di-btrbk-send pattern. Additionally an apt
    `DPkg::Post-Invoke` hook (`/etc/apt/apt.conf.d/95snapsend-replicate`, filename-sorted
    after snapper's apt post-snapshot hook so it runs after it within the same stage)
    `systemctl --no-block start`s the **same** `snapsend.service` after every apt
    transaction — a trigger into the existing service, not a second execution path, so
    `flock`/logging/limits all apply and a collision is a clean `_AlreadyRunning` no-op
    (see §3; the `Post-Invoke` stage choice and why not `-Success` is explained there).
    Generated by an installer step (see §8).

11. **Tests** — unit-test the pure logic with synthetic `Subvol` objects:
    `is_correlated()` (all three clauses + the readonly guard), `choose_parent()`
    (newest-correlated-older, full-send fallback), `_newest_correlated_pair()`,
    and the garble/valid properties. These are the parts where a bug is
    silent-but-catastrophic, so they get the test coverage. Transfer/ssh paths
    can be integration-tested against a loopback btrfs image.

---

## 8. Packaging into the suite (di-* convention)

The tool is Python, but its **installer/config** is a Bash `di-snapsend.sh` with
two roles (the historical spellings `--server`/`--laptop` are kept as aliases of
`--dest`/`--source`):

- `di-snapsend.sh --dest` (alias `--server`) — provision the **destination**
  receive end: dedicated transport user, receive area (on Btrfs), restricted SSH
  key via a forced-command wrapper limited to the exact `btrfs receive`/`btrfs
  subvolume`/etc. command set, and a scoped sudoers rule.
- `di-snapsend.sh --source` (alias `--laptop`) — install the engine on the
  **source** to `/usr/local/bin/di-snapsend`, write `/etc/snapsend/config`,
  generate the SSH key, pin the destination host key, and enable the systemd timer
  + watchdog.
- The Python tool itself (`di-snapsend`) is the engine; the `.sh` is the installer.

### Config file schema — `/etc/snapsend/config` (TOML)

Written with sane defaults by `di-snapsend.sh --source`; tuned in place. Parsed
with `tomllib` (stdlib, Python 3.11+).

```toml
[server]
host     = "dest-host"
ssh_port = 22
user     = "snapsend"
ssh_key  = "/etc/snapsend/ssh/id_ed25519"
use_mbuffer = true
# Single base for ALL tiers. Each tier is composed at runtime as
# <recv_base>/<hostname>/...  (hostname = socket.gethostname(), derived — §4):
#   snapshots -> <recv_base>/<hostname>/<subvol>/<date>-<num>-<short_uuid>/snapshot
#   boot      -> <recv_base>/<hostname>/{boot,boot-efi}
recv_base = "/srv/snapshots-recv"

# Non-Btrfs boot tier (rsync mirror, not versioned — see §11).
[boot]
enabled   = true
paths     = ["/boot", "/boot/efi"]   # efi nested last

# One table per replicated subvolume — `mountpoint` only. The destination is
# composed from [server].recv_base + hostname + the table name (no recv_dir here).
[subvolumes.home]
mountpoint = "/home"          # where Snapper's .snapshots lives

[subvolumes.root]
mountpoint = "/"

# TARGET-side retention only (source is Snapper-owned — Decision 1). GFS thinning
# applies ONLY to the long tail (snapshots the source has aged out); every target
# whose source still exists is retained — the destination is a superset of the source
# (Option B, §9.1). [retention.default] is the fallback; per-subvol tables override.
[retention.default]
keep_hourly  = 24             # finest tier; match the source's Snapper hourly count
keep_daily   = 14
keep_weekly  = 8
keep_monthly = 6

[retention.root]
keep_hourly  = 24
keep_daily   = 30             # apt pre/post history kept longer
keep_weekly  = 12
keep_monthly = 12
```

> **`keep_hourly` back-compat:** the per-table parser reads `keep_hourly` with a
> default of **0** (`int(t.get("keep_hourly", 0))`), so a config table that omits the
> key disables the hourly tier — pruning is byte-for-byte identical to releases before
> the tier existed. The shipped `config.example.toml` and the built-in `Config`
> defaults use `24` for new installs. Upgrading an existing deployment therefore never
> silently changes its retention; opting in is an explicit `keep_hourly = N`.

`Config` (the dataclass in the skeleton) is **populated from this file** at
startup, not hard-coded. Operational flags (`--server`, `--dry-run`, `--subvol`)
override the corresponding file values via the three-tier precedence; retention
has no CLI equivalent — it is file-only policy.

The destination receive directory and `.latest` contract are identical to the
btrbk-send design, so the **restic-on-destination** component (next milestone) can
point at `<recv_base>/<hostname>/<subvol>/<subvol>.latest` regardless of which
sender is in use. This keeps the downstream restic tier decoupled from this
decision.

---

## 9. Decisions (locked 2026-06-27)

1. **Source-side retention: TARGET-ONLY.** di-snapsend prunes **only the
   destination**. All source retention is left entirely to Snapper — one owner
   of local retention, no risk of fighting Snapper's timeline cleanup. The
   `keep_source_last` knob is **removed** from the design. di-snapsend reads
   source snapshots but never deletes them.

   **Superset model (Option B).** Target retention applies GFS thinning **only to
   snapshots the source has already aged out**. While a source snapshot still
   exists, its target copy is **always retained** — the destination is a *superset* of
   the source. Concretely, `apply_retention` keeps the union of: the GFS
   hourly/daily/weekly/monthly set, the pinned parent (Rule 3), the root pre/post
   partners, **and every target whose source counterpart is still present**
   (`source_backed`). Only the "long tail" — targets whose source is gone — is
   GFS-thinned. This is required for correctness, not just tidiness: the "what to
   send" decision (`replicate_subvol.missing` = sources not on the target) and the
   "what to retain" decision must not fight. Without it, a target pruned by GFS
   while its source still exists is immediately re-sent on the next run, then
   re-pruned — endless churn (observed live: an hourly source snapshot re-sent and
   re-deleted every run). The superset rule removes the churn *by construction*:
   the snapshots that would be re-sent are exactly the ones now pinned by
   `source_backed`. Consequence: the destination short-term holds the **full current
   source set** plus a GFS-thinned long tail — more than a strict `keep_daily`
   count implies, but cheap (Btrfs COW) and the desired DR-mirror behaviour; the
   long tail is still bounded so the destination never grows without limit.

2. **Replicate BOTH home and root.** Home is the churny personal data; root
   carries full-system DR plus the apt pre/post history. Both are sent. Root's
   pre/post pair handling (§4) applies.

3. **Retention is CONFIG-FILE DEFINED**, per-subvolume, in `/etc/snapsend/config`
   (TOML — `tomllib` is stdlib on Trixie's Python 3.13). A `[retention.default]`
   provides fallbacks; per-subvol tables (`[retention.root]`, `[retention.home]`)
   override. See §8 for the schema. Retention is **policy**, so it lives in the
   file, not in code or CLI flags. Operational flags (`--server`, `--dry-run`,
   `--subvol`) keep the three-tier precedence (flag > env > config).

4. **Transport user: `snapsend`** (renamed from the placeholder `btrbk`). The
   `--dest` installer role (alias `--server`) creates this dedicated
   least-privilege user.

5. **Retention bucket boundaries are timezone-configurable; default LOCAL.**
   `[retention].timezone` (global, `"local"` | `"utc"`, default `"local"`; warn +
   fall back to local on an unrecognized value) chooses the calendar used for the
   GFS hourly/daily/weekly/monthly **bucket boundaries**:
   - `"local"` (default) — hour/day/week/month boundaries align to **this machine's
     local calendar**, so a "Monday weekly" is the operator's local Monday. The
     internal datetime layer is naive-UTC; `_bucket_keep` converts each `when` to
     local **only** to extract the bucket key. The hourly bucket key is
     `(year, month, day, hour)` from that same zone-shifted instant, so it honours
     the timezone exactly like the coarser tiers.
   - `"utc"` — UTC boundaries, fully timezone-independent (for strict cross-zone
     determinism / shared setups).

   **Scope of the zone-shift:** ONLY the bucket-key extraction in `_bucket_keep` is
   zone-aware. Correlation (Rules 1–3), the pinned parent, Option B `source_backed`,
   and the newest-first **ordering** all operate on the true instant / identity and
   are untouched — a zone shift never changes instant ordering, so the sort is
   unaffected.

   **DST is safe.** Local bucketing needs no DST special-casing. The only effect is
   cosmetic and harmless: on a fall-back day/hour a local bucket may keep two snapshots
   instead of one; on spring-forward it is short. Neither loses data nor breaks the
   chain — a once-a-year off-by-one in *how many* snapshots are kept that single
   day/hour. The DST caveat applies to the hourly tier identically to daily/weekly/
   monthly. (Folder names are always local-time **+ UTC offset** — §4 — so even the
   repeated fall-back hour yields distinct, non-colliding names.)

   **GFS taper (four tiers).** `_bucket_keep` walks targets newest-first and keeps the
   newest snapshot of each populated bucket, for the most recent `keep_hourly` hours /
   `keep_daily` days / `keep_weekly` ISO-weeks / `keep_monthly` months (union, kept
   once if a snapshot is the rep of several tiers). The tiers run finest→coarsest —
   **hourly → daily → weekly → monthly** — hourly being the finest granularity and
   shortest reach. Each tier is independent and any tier set to `0` is disabled (the
   `lim[p] > 0` guard); `keep_hourly = 0` therefore reduces to the original
   daily/weekly/monthly GFS. The hourly tier exists to let the destination match
   Snapper's hourly granularity on the source (see the union-rule note below).

### Source vs destination retention interaction (the union rule)

Two **independent** retention systems run: Snapper prunes the **source**;
di-snapsend prunes the **destination**. The destination keep-rule is a **UNION** — a
destination snapshot is kept if **either** it's within di-snapsend's GFS policy
**or** its source counterpart still exists (`source_backed`), kept once if both (no
duplication). So the destination is a **superset** of the source, and **whichever
retention reaches further back wins, per tier:**

- Destination policy longer than source (the intended setup): di-snapsend governs
  the archive depth; source-backed just shields the recent dense history. They
  cooperate.
- Source longer than destination at a tier: the source-backed rule overrides the
  shorter destination limit *at that tier* — that tier's config is effectively dead
  (correct, not data loss, just "you keep more"). **Decision 5b:** `apply_retention`
  emits an INFO line per run/subvolume when this happens (`override = source_backed
  − keep_paths` non-empty), so the otherwise-silent "holding more than the config
  implies" is visible. Log-only; it changes no keep/prune decision.
- Mixed: resolved per-tier in one run.

**The hourly tier and the override line.** The classic instance of "source longer than
destination" was the *hourly* band: Snapper keeps ~48 hourlies on the source, but with
daily as di-snapsend's finest bucket, every intra-day hourly was source-backed yet
"beyond policy", so the override INFO line reported a count climbing through the day and
dropping at the daily rollover (accurate but noisy). Setting `keep_hourly` ≥ the source's
hourly count gives those snapshots a real GFS bucket: they enter `keep_paths` via the
hourly tier, leave the `override = source_backed − keep_paths` set, and the line stops
reporting the hourly band. The override-logging logic is **unchanged** — it self-corrects
once the tier is configured. Left at `0`/unset, the message behaves exactly as before
(the destination genuinely has a coarser policy than the source).

**Guidance:** set each destination tier ≥ the corresponding source tier and keep the
source retention short — then the destination governs the archive, the source stays
lean, and shortening the source never reduces recoverability. The union rule is also
what eliminates re-send/re-prune churn (di-snapsend never deletes a copy only to
re-send it because its source still exists). The user-facing version of this is in
the README's "Retention" section.

### Destination retention numbers (decision 3 detail)

Example starting policy — tune in the config file as the destination fills:

| Subvol | keep_hourly | keep_daily | keep_weekly | keep_monthly | Rationale |
|--------|-------------|-----------|-------------|--------------|-----------|
| home | 24 | 14 | 8 | 6 | Personal data; long tail on cheap storage |
| root | 24 | 30 | 12 | 12 | Keep apt pre/post upgrade history longer for system rollback |

`keep_hourly` is new-install default `24`; on an existing config that omits it the
hourly tier stays disabled (`0`) — see the back-compat note in §8.

These are the "long retention on cheap on-prem storage" tier — deliberately
generous since Btrfs COW makes history cheap. Adjust freely; the pin guard
(Rule 3) protects the incremental parent regardless of how aggressive these get.

---

## 11. Boot tier — non-Btrfs EFI/boot mirror (rsync)

`/boot` (ext4) and `/boot/efi` (FAT32) are **not Btrfs** — they have no
snapshots, no UUIDs-as-snapshots, and cannot be `btrfs send`-ed. They are a
**parallel, independent tier** within di-snapsend: a single current mirror via
`rsync`, **not versioned**, run alongside (not through) the snapshot machinery.

### Why it exists

Full DR needs a bootable system reconstructed **with our custom bootloader
config** (GRUB setup, initramfs, EFI entries). The snapshot tiers cover `@` and
`@home`, but the bootloader lives on separate non-Btrfs partitions. Without this
mirror, a DR restore would have the data but not a clean path to boot it. This
ports the original `di-btrbk.sh` boot/efi rsync logic — from a USB target to the
destination, and from a separate bash helper into a Python function.

### Behaviour

- `rsync -aAX --delete` each boot path to
  `<recv_base>/<hostname>/<name>/`, where `/boot -> boot/` and
  `/boot/efi -> boot-efi/` (matches the original naming). The `<recv_base>` and
  the resolved `<hostname>` are the **same** ones the snapshot tiers use (§4), so
  the full per-host subtree is consistent: `<host>/{home,root,boot,boot-efi}`.
- Runs over SSH with `--rsync-path "sudo rsync"` so the remote side can write
  the receive area as the unprivileged `snapsend` transport user.
- **Single current copy only.** No versioning here — and it doesn't need any,
  because the mirror lands in the destination's receive area, so the **downstream
  restic-on-destination tier versions it for free** (each tool does its job: rsync
  makes the current mirror, restic versions everything on the destination).
- **Fail-safe + independent.** A boot rsync failure is logged/warned but does
  **not** abort the snapshot tiers (and vice versa). Boot backup runs **once per
  host**, after the per-subvol replication loop.

### Implementation notes

- EFI is FAT32 → no ownership/perms/ACLs/xattrs. `-aAX` emits benign warnings on
  the efi path; treat rsync exit codes **0 and 24** (vanished source files) as
  success, warn otherwise. (Encoded in `boot_backup()`.)
- Trailing slashes are load-bearing: `"<src>/"` copies *contents* into
  `"<dest>/"`.
- The **restore side already exists** in `di-btrfs-recovery.sh` (it formats EFI
  FAT32 + /boot ext4 and restores both via rsync, then chroots to
  `update-initramfs` + `grub-install` + `update-grub`). This tier simply *feeds*
  that existing recovery flow — no new restore code needed.

Encoded as: `boot_backup(cfg, hostname)` + the `[boot]` config table +
`Config.boot_backup_enabled/boot_paths` + the shared `Config.recv_base`.

---

## 10. Summary

- **Layout verified**, parse logic validated against real `btrfs subvolume show`
  output — no structural change needed to the skeleton.
- **Three correctness rules** (validity / correlation / prune-guard) are the
  heart; all encoded, all with worked examples using real UUIDs.
- **Numbering divergence under retention is a non-issue** — correlation is
  UUID-based; numbers are within-machine labels only.
- **Naming:** `<num>-<short_uuid>/snapshot` on the destination (received in place, no
  subvolume `mv`); `.latest` symlink → that subvol for restic.
- **Boot tier:** non-Btrfs `/boot` + `/boot/efi` mirrored via rsync (not
  versioned); feeds the existing `di-btrfs-recovery.sh` restore flow.
- **Implementation is bounded:** ~11 well-scoped tasks, the riskiest of which
  (`_run_pipe`, retention) are clearly specified and unit-testable.
- **Stays in Debian main**, reads Snapper natively, fully reasoned-about — the
  whole reason for building it instead of bending btrbk or adopting snbk-via-OBS.
