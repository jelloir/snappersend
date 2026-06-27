# di-snapsend — Design Specification & Implementation Brief

**Status:** Design complete, ready for implementation.
**Audience:** Claude Code (implementation) + James (review).
**Companion file:** `di-snapsend-DESIGN.py` (annotated skeleton — the structural
contract; this document is the *why* and the worked examples behind it).

---

## 1. What this tool is

`di-snapsend` is a **thin orchestration layer** that replicates Snapper-created
Btrfs snapshots from the laptop (`millionaire`) to the server (`debian-server`)
over SSH. It is the on-prem disaster-recovery / snapshot-history tier of the
Debstillation backup architecture.

It is a peer to btrbk and snbk in *role*, but deliberately minimal: it does
**not** reimplement the Btrfs stream format or network transport. It shells out
to the tools that already solve those problems:

```
btrfs send [-p PARENT] SRC | [mbuffer |] ssh SERVER "sudo btrfs receive DST"
```

btrfs does the filesystem + stream work. ssh does the (encrypted) network. This
tool does only the three things a wrapper actually has to get right:

1. **Enumeration** — read Snapper's snapshots directly off disk (no format
   guessing), and inventory what the server already holds.
2. **Parent selection** — pick the correct `-p` parent for incrementals.
3. **Cleanup + retention** — detect and delete partial transfers; prune each
   side per policy without ever breaking the incremental chain.

### Why build it rather than use btrbk or snbk

| Option | Blocker for this setup |
|--------|------------------------|
| btrbk | Config DSL doesn't map cleanly onto Snapper's `<N>/snapshot` nesting — hit in practice (sent nothing). Powerful but heavy (7k lines Perl). |
| snbk | Mirror-only retention; requires an out-of-Debian OBS repo on the laptop (snapper ≥ 0.12; Trixie ships 0.10.6). |
| **di-snapsend** | Reads Snapper's own layout directly; stays in Debian main (needs only `btrfs-progs` + `openssh`, both in main); retention is plain Python we control. |

This is squarely aligned with the project's values: minimalism, "do one thing
well", Debian-main containment, and reasoning fully about our own tools.

---

## 2. Confirmed environment (millionaire, verified 2026-06-27)

### On-disk Snapper layout

| Subvol | Btrfs path | Snapshot path (what we read) | Mounted at |
|--------|-----------|------------------------------|------------|
| root | `@snapshots` (ID 264, top-level) | `@snapshots/<N>/snapshot` | `/.snapshots/<N>/snapshot` |
| home | `@home/.snapshots` (ID 271, nested under `@home` 268) | `@home/.snapshots/<N>/snapshot` | `/home/.snapshots/<N>/snapshot` |

Note the structural asymmetry: root's `.snapshots` is a **top-level** subvolume;
home's is **nested** under `@home`. This does not change the read logic — both
expose `<N>/snapshot` at their mountpoint — but is worth knowing.

Each snapshot directory contains:
```
/home/.snapshots/1/
├── snapshot/      <- the read-only Btrfs subvolume we send
└── info.xml       <- Snapper metadata (number, timestamp, description)
```

### Verified `btrfs subvolume show` output (the fields our rules depend on)

From `/home/.snapshots/1/snapshot`:
```
UUID:            a2159d69-abcd-934e-a327-68d19fc4cd1b
Parent UUID:     f877a71f-1aea-f040-8ad0-655e84d27d1c
Received UUID:   -
Flags:           readonly
```

Confirmed parsing facts:
- `Flags:` contains the literal substring `readonly` for RO subvols →
  `"readonly" in flags_line.lower()` is a reliable test.
- Source snapshots have `Received UUID: -` (they're locally created). After a
  send, the **server's** copy will carry `Received UUID = <source UUID>` — this
  is the correlation link (Rule 2).
- `Parent UUID` is the live `@home` subvol and is identical across all home
  snapshots → it is **useless for ordering**. Use the Snapper number for
  within-machine age ordering.

### Retention currently in effect

`home` config: `TIMELINE_LIMIT_HOURLY 48`, `DAILY 7`, `WEEKLY 4`,
`MONTHLY 0`, `NUMBER_LIMIT 0`, `TIMELINE_MIN_AGE 1800`.
Snapper owns local retention and will gap/churn the numbering (expected — see §4).

`root` config has **pre/post pairs** from the apt hook (e.g. 3/4 "apt upgrade",
15/16 "apt"), and **non-contiguous numbers** (5 is absent). Both facts are
handled by enumerating what's actually on disk rather than assuming a range.

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
**both** conditions. If garbled, `btrfs subvolume delete` it on the server and
treat the transfer as failed. Never advance the parent pointer past an
unverified transfer.

Encoded as: `Subvol.is_valid_received` and `Subvol.is_garbled`.

### Rule 2 — Parent eligibility ("correlation")

A laptop snapshot `S` may be the `-p` parent for an incremental **only if** the
server already holds a snapshot correlated with `S` (same Btrfs lineage). Both
must be read-only. Correlation holds when **any** of:

```
S.uuid          == T.received_uuid     (T was received from S)
T.uuid          == S.received_uuid
S.received_uuid == T.received_uuid     (both received from a common source)
   AND S.received_uuid != '-'
```

(from btrbk `_is_correlated`, `btrfs:2587`)

**Worked example with real data.** Laptop home snapshot #1 has
`uuid = a2159d69-…`, `received_uuid = -`. After we send it, the server's copy
will have `received_uuid = a2159d69-…`. On the next run, `is_correlated(laptop#1,
server_copy)` returns true via the first clause (`S.uuid == T.received_uuid`).
So laptop #1 is a valid parent for sending laptop #2 incrementally. ✓

**Parent choice:** the parent for sending snapshot `N` is the **newest** laptop
snapshot that is (a) strictly older than `N` (lower Snapper number) and (b)
correlated with something on the server. If none → full send.

Encoded as: `is_correlated()` and `choose_parent()`.

### Rule 3 — Retention prune guard

Retention must **never** delete the snapshot that is the current newest
correlated pair, on **either** side — it is the parent the next incremental
depends on. Delete it and the next run silently degrades to a full send (or
fails outright).

Compute the newest correlated `(source, target)` pair first and **pin** it;
build delete sets that honour the keep-policy **and** exclude the pinned pair.

Encoded as: `_newest_correlated_pair()` (pin), consumed by `apply_retention()`.

**Invariant this imposes on scheduling:** laptop retention must always leave at
least one snapshot the server also holds, to serve as parent. With Snapper
keeping 48 hourly and this tool running hourly, that is satisfied with wide
margin. If the laptop were offline for >48h, the next run would correctly fall
back to a full send (no breakage, just a bigger transfer).

---

## 4. The numbering / retention divergence (resolved)

**Concern:** once Snapper retention deletes old snapshots on the laptop, laptop
numbering gaps and diverges from what the server holds (server keeps long
retention). Won't this break matching?

**Resolution: it does not, because correlation is by UUID, never by number.**

Worked through with real state. Suppose laptop home currently holds 1–24 and all
are on the server. Overnight, Snapper timeline cleanup deletes 1–10 on the
laptop (aged past the hourly window). Next run:

- Laptop holds 11–N. Server holds 1–N (long retention).
- `choose_parent()` for the new snapshot finds the newest laptop snapshot whose
  **UUID** correlates with a server copy. Laptop #11 still exists; its UUID still
  matches the server's copy-of-#11 → valid parent.
- The deletion of 1–10 on the laptop is **invisible** to correlation, because
  the tool never looks up snapshots by number across machines. Numbers are used
  **only** for within-machine age sorting.

The divergence is the asymmetric retention working as intended (short on laptop,
long on server). The single invariant (Rule 3 / §3) — keep at least one shared
snapshot as parent — is guaranteed by hourly operation against a 48-hourly
Snapper window.

### Server-side naming scheme (decided)

Each received snapshot lives in a per-transfer **directory**, with the subvol one
level down (the subvol is always named `snapshot` by `btrfs receive` — §5.2):

```
<recv_dir>/<snapper_num>-<short_uuid>/snapshot      e.g.  root/24-a2159d69/snapshot
```

- `snapper_num`: the source Snapper number **at send time** — human-readable,
  mirrors how Snapper presents snapshots (the "keep the numbering" goal).
- `short_uuid`: first 8 hex chars of the source `uuid` — guarantees uniqueness
  on the server even as laptop numbers churn/repeat over months, and ties the
  directory name to the correlation key.

The wrapper directory `<snapper_num>-<short_uuid>` mirrors Snapper's own
`<N>/snapshot` layout and lets every snapshot be received directly into its final
home (no subvolume `mv` — §5.2). Enumeration reads the subvol at `<name>/snapshot`
but recovers `snapper_num` from the **directory** name; retention deletes the
subvol then `rmdir`s the wrapper.

The `<recv_dir>/<subvol>.latest` symlink always points at the newest received
subvol `<num>-<short_uuid>/snapshot` (by send order), giving restic a stable
target. This is our own equivalent of btrbk's `latest` pointer.

### Pre/post pair handling (root only)

Root snapshots include apt-hook pre/post pairs. They send and correlate like any
other RO snapshot. The only refinement: **server-side retention should avoid
orphaning half a pre/post pair** when *we* prune — if a `post` is kept, prefer
keeping its `pre` for restore sanity. This is a server-retention nicety (Rule 3
territory), not a correctness requirement. Implement as a "keep pairs together"
check in `apply_retention()` for the root subvol.

---

## 5. Transfer pipeline (implementation detail)

### Full send (first time, or no correlated parent)
```
btrfs send SRC | [mbuffer |] ssh SERVER "sudo btrfs receive RECV_DIR"
```

### Incremental (have correlated parent P)
```
btrfs send -p P.path SRC | [mbuffer |] ssh SERVER "sudo btrfs receive RECV_DIR"
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
   the server, receive each one directly into its **own final per-transfer
   directory**:
   - `final_dir = RECV_DIR/<num>-<short_uuid>`
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
3. target_snaps = enumerate RECV_DIR on server
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
   server; never touch source snapshots (Snapper owns those). Implement
   `keep_daily/weekly/monthly` per-subvol from the config file, exclude the
   pinned pair (Rule 3), and apply the pre/post "keep pairs together" check for
   root (§4). Retention buckets need real timestamps (from `info.xml` or subvol
   creation time), not just numbers — see task 5.

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

10. **systemd units** — `snapsend.service` (oneshot) + `snapsend.timer` (hourly +
    randomized delay, `Persistent=true`), plus a missed-run watchdog mirroring
    the di-btrbk-send pattern. Generated by an installer step (see §8).

11. **Tests** — unit-test the pure logic with synthetic `Subvol` objects:
    `is_correlated()` (all three clauses + the readonly guard), `choose_parent()`
    (newest-correlated-older, full-send fallback), `_newest_correlated_pair()`,
    and the garble/valid properties. These are the parts where a bug is
    silent-but-catastrophic, so they get the test coverage. Transfer/ssh paths
    can be integration-tested against a loopback btrfs image.

---

## 8. Packaging into the suite (di-* convention)

The tool is Python, but its **installer/config** should follow the suite's Bash
`di-*.sh` convention (the same shape as `di-btrbk-send.sh`):

- `di-snapsend.sh --server` — provision the receive end: dedicated transport
  user, receive subvolume, restricted SSH key (`ssh_filter`-style command
  restriction or a forced-command wrapper limited to `btrfs receive`/`btrfs
  subvolume`), sudoers rule. Reuse the proven logic from `di-btrbk-send.sh`'s
  server role almost verbatim — that part is tool-agnostic.
- `di-snapsend.sh --laptop` — install the Python tool to
  `/usr/local/bin/di-snapsend`, write `/etc/snapsend/config`, install the SSH
  key, write + enable the systemd timer and watchdog.
- The Python tool itself (`di-snapsend`) is the engine; the `.sh` is the
  installer. Mirrors how the suite separates concerns elsewhere.

### Config file schema — `/etc/snapsend/config` (TOML)

Written with sane defaults by `di-snapsend.sh --laptop`; tuned in place. Parsed
with `tomllib` (stdlib, Python 3.11+).

```toml
[server]
host     = "debian-server"
ssh_port = 22
user     = "snapsend"
ssh_key  = "/etc/snapsend/ssh/id_ed25519"
use_mbuffer = true

# Non-Btrfs boot tier (rsync mirror, not versioned — see §11).
[boot]
enabled   = true
paths     = ["/boot", "/boot/efi"]   # efi nested last
recv_base = "/srv/snapshots-recv"    # -> /<hostname>/{boot,boot-efi}

# One table per replicated subvolume.
[subvolumes.home]
mountpoint = "/home"          # where Snapper's .snapshots lives
recv_dir   = "/srv/snapshots-recv/home"

[subvolumes.root]
mountpoint = "/"
recv_dir   = "/srv/snapshots-recv/root"

# TARGET-side retention only (source is Snapper-owned — Decision 1).
# [retention.default] is the fallback; per-subvol tables override.
[retention.default]
keep_daily   = 14
keep_weekly  = 8
keep_monthly = 6

[retention.root]
keep_daily   = 30             # apt pre/post history kept longer
keep_weekly  = 12
keep_monthly = 12
```

`Config` (the dataclass in the skeleton) is **populated from this file** at
startup, not hard-coded. Operational flags (`--server`, `--dry-run`, `--subvol`)
override the corresponding file values via the three-tier precedence; retention
has no CLI equivalent — it is file-only policy.

The server receive directory and `.latest` contract are identical to the
btrbk-send design, so the **restic-on-server** component (next milestone) can
point at `/srv/snapshots-recv/<subvol>/<subvol>.latest` regardless of which
sender is in use. This keeps the downstream restic tier decoupled from this
decision.

---

## 9. Decisions (locked 2026-06-27)

1. **Source-side retention: TARGET-ONLY.** di-snapsend prunes **only the
   server**. All laptop/source retention is left entirely to Snapper — one owner
   of local retention, no risk of fighting Snapper's timeline cleanup. The
   `keep_source_last` knob is **removed** from the design. di-snapsend reads
   source snapshots but never deletes them.

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
   `--server` installer creates this dedicated least-privilege user.

### Server retention numbers (decision 3 detail)

Starting policy — tune in the config file as the server fills:

| Subvol | keep_daily | keep_weekly | keep_monthly | Rationale |
|--------|-----------|-------------|--------------|-----------|
| home | 14 | 8 | 6 | Personal data; long tail on cheap storage |
| root | 30 | 12 | 12 | Keep apt pre/post upgrade history longer for system rollback |

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
server, and from a separate bash helper into a Python function.

### Behaviour

- `rsync -aAX --delete` each boot path to
  `/srv/snapshots-recv/<hostname>/<name>/`, where `/boot -> boot/` and
  `/boot/efi -> boot-efi/` (matches the original naming).
- Runs over SSH with `--rsync-path "sudo rsync"` so the remote side can write
  the receive area as the unprivileged `snapsend` transport user.
- **Single current copy only.** No versioning here — and it doesn't need any,
  because the mirror lands in the server's receive area, so the **downstream
  restic-on-server tier versions it for free** (each tool does its job: rsync
  makes the current mirror, restic versions everything on the server).
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
`Config.boot_backup_enabled/boot_paths/boot_recv_base`.

---

## 10. Summary

- **Layout verified**, parse logic validated against real `btrfs subvolume show`
  output — no structural change needed to the skeleton.
- **Three correctness rules** (validity / correlation / prune-guard) are the
  heart; all encoded, all with worked examples using real UUIDs.
- **Numbering divergence under retention is a non-issue** — correlation is
  UUID-based; numbers are within-machine labels only.
- **Naming:** `<num>-<short_uuid>/snapshot` on the server (received in place, no
  subvolume `mv`); `.latest` symlink → that subvol for restic.
- **Boot tier:** non-Btrfs `/boot` + `/boot/efi` mirrored via rsync (not
  versioned); feeds the existing `di-btrfs-recovery.sh` restore flow.
- **Implementation is bounded:** ~11 well-scoped tasks, the riskiest of which
  (`_run_pipe`, retention) are clearly specified and unit-testable.
- **Stays in Debian main**, reads Snapper natively, fully reasoned-about — the
  whole reason for building it instead of bending btrbk or adopting snbk-via-OBS.
