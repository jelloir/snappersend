# Retention testing — decision vs execution

Retention has two halves, tested separately because they fail in different ways:

| Half | What it does | How it's tested |
|------|--------------|-----------------|
| **Decision** | `_bucket_keep` + `apply_retention` read folder-name timestamps + UUID/correlation and compute the keep/prune set (GFS daily/weekly/monthly ∪ pin ∪ Option-B source-backed ∪ root pre/post). | **Part A** — exhaustive **pure-logic unit tests** (`test_di_snapsend.py`). No filesystem; fast; covers years of history and every edge case under both `local` and `utc` timezones. |
| **Execution** | the real `btrfs subvolume delete` + wrapper `rmdir` + `.latest` repoint over SSH. | **Part B** — on the **VM**, against **real (empty) subvolumes** (`tools/fabricate-history.sh` + `tools/retention-vm-check.py`). |

### Why the split — the empty-folder caveat

Retention executes `btrfs subvolume delete`, which **fails on a plain `mkdir`'d
directory** ("not a subvolume"). So:

- the **unit** layer must stay on synthetic `Subvol` objects (Part A) — the
  decision reads only each snapshot's timestamp + UUID, none of which needs a real
  subvolume; and
- **real-filesystem** testing must use **real subvolumes** (`btrfs subvolume
  create`, Part B) — never empty `mkdir`'d folders, which deletion would reject.

`btrfs subvolume create` is near-instant and ~zero space, so fabricating hundreds
of backdated "snapshots" on the VM is cheap.

---

## Part A — exhaustive pure-logic suite

In `test_di_snapsend.py`. Built on the existing `mksub` / `use_tz` scaffolding plus:

- `mktarget(dt_utc_naive, num)` — a synthetic **target** whose dated name
  (`<localdate>-<offset>-<num>-<short_uuid>`) matches what `send_receive` writes and
  `list_target_snapshots` parses, so bucketing sees the same instant the name
  encodes. Renders the name in **local** time — **call it inside the relevant
  `use_tz(...)` block.**
- `mksource(num)` — the correlated source (for pin / Option-B scenarios).
- `gen_history(start, end, step)` — emit a target history at any cadence.
- `oracle_keep(targets, kd, kw, km, tz)` — an **independent, group-based** reference
  GFS implementation (structurally different from the engine's newest-first
  counter). Every scenario asserts `di._bucket_keep(...) == oracle_keep(...)`
  **exactly**, plus hand-reasoned spot checks.

### Run

```sh
SNAPSEND_QUIET=1 python3 -m unittest -v test_di_snapsend
# survivor-count report per scenario:
SNAPSEND_REPORT=1 python3 -m unittest test_di_snapsend.TestRetentionExhaustive \
                                      test_di_snapsend.TestRetentionApplyInteractions 2>&1 | grep retention-report
```

All Part A tests pin `TZ` internally (`use_tz`) and are **deterministic on any host
timezone** (verified under America/New_York, UTC, Asia/Kolkata, Pacific/Kiritimati
+14, Etc/GMT+12).

### Scenarios (`TestRetentionExhaustive` + `TestRetentionApplyInteractions`)

Each asserts the **exact** keep/prune set, under both `local` (Australia/Brisbane,
UTC+10) and `utc`:

1. **Multi-year dense hourly** — 2 years (~17.5k) hourly, `14/8/6`: exact GFS taper,
   union not double-counted, count within the `kd+kw+km` envelope.
2. **Sporadic ~3-weekly** — empty buckets must **not** consume quota
   (`keep_weekly=8` keeps the 8 most recent *populated* weeks).
3. **Sporadic ~2-monthly** — 6 most recent populated months survive; union means
   "kept by any class ⇒ kept".
4. **Sporadic yearly-ish** over 6 years — "don't nuke my only yearly backup": the
   newest within-limit survive, only the truly-beyond-all-limits oldest prunes.
5. **Mixed cadence** — hourly-recent → daily → monthly; clean taper across changes.
6. **Boundary precision** — local-midnight (UTC+10), month-end/start, ISO-week edge
   (Sun/Mon), **leap day** (29 Feb), and a full **DST** span (Europe/London) that
   matches the oracle and never over-prunes (documented ±1/day cosmetic only).
7. **Pin (Rule 3)** — pinned parent kept even when GFS would drop it.
8. **Option B** — source-backed never pruned; long tail still prunes; steady-state
   prunes nothing.
9. **Pre/post pairs (root)** — pair not orphaned among survivors.
10. **Zero-limit classes** — disabled classes keep/delete nothing; all-zero keeps
    only pin + source-backed.
11. **Undatable safety** — an unparseable name (`when is None`) is never deleted.

### Reported survivor counts (illustrative, `14/8/6` unless noted)

```
multi-year-hourly  [local] 17544 -> 22 kept   [utc] -> 23 kept
mixed-cadence      [local]   957 -> 22 kept   [utc] -> 23 kept
dst-transition     [local]  5880 -> 24 kept
sporadic-3wk (0/8/6)          35 ->  8 kept
sporadic-2mo (3/4/6)          18 ->  6 kept
yearly-ish   (2/3/6)           7 ->  6 kept (1 pruned: the oldest)
apply: steady-state           10 -> 10 kept (0 pruned)
apply: long-tail (kd=2)       10 ->  3 kept (7 pruned)
```

---

## Part B — VM execution test (real empty subvolumes)

Proves the **chosen prune-set is actually executed** on Btrfs: deleted, wrappers
`rmdir`'d, `.latest` repointed — using real subvolumes under a **dedicated fake
host segment** (`fabricate-test`) so it never touches real backups.

### Tools

- `tools/fabricate-history.sh` — **run on the server**. Creates / lists / cleans
  backdated **real empty** subvolumes with correct dated names.
- `tools/retention-vm-check.py` — **run on the laptop** (has the config + key).
  Enumerates the fabricated targets, computes the GFS decision via the engine,
  executes `apply_retention` for real (no sources → pure GFS + execution path),
  re-enumerates, and asserts on-disk survivors == decision and `.latest` resolves
  to a surviving `…/snapshot`.

### Run

```sh
# 1) on the SERVER — fabricate a backdated history
sudo ./fabricate-history.sh fabricate home hourly 120     # dense, 5 days
#    (or: ... fabricate home weekly 52                     # sporadic, 1 year)

# 2) on the LAPTOP — execute real retention + verify
sudo python3 tools/retention-vm-check.py home --keep-daily 3 --keep-weekly 0 --keep-monthly 0
sudo python3 tools/retention-vm-check.py home --keep-daily 3 --keep-weekly 0 --keep-monthly 0 --expect-noop  # idempotent

# 3) on the SERVER — clean up (reversible; returns the VM to a clean state)
sudo ./fabricate-history.sh cleanup
```

### Verified results

| Scenario | Fabricated | Policy | On-disk survivors | Match |
|----------|-----------|--------|-------------------|-------|
| Dense hourly | 120 | `kd=3` | 3 (newest hour of each of the 3 newest **local** days) | ✅ |
| Dense hourly re-run | 3 | `kd=3` | 3, **0 deleted** (idempotent) | ✅ |
| Sporadic weekly | 52 | `0/8/6` | 12 (8 weekly + 6 monthly reps, deduped) | ✅ |
| Sporadic weekly re-run | 12 | `0/8/6` | 12, **0 deleted** | ✅ |

In every case the on-disk survivors **exactly** matched the engine's GFS decision,
pruned wrappers were `rmdir`'d (only survivors + `<subvol>.latest` remained), and
`.latest` resolved to the newest surviving real `…/snapshot`. Re-runs deleted
nothing and errored nowhere.

### Scope note

Part B deliberately scopes to the **GFS + execution** mechanics (no sources →
no correlation), per the brief's recommended option. The **pin (Rule 3)**,
**Option B source-backed**, and **pre/post** *decisions* are covered exhaustively
in Part A; their *execution* is exercised by the normal end-to-end `di-snapsend`
runs (where the pinned parent provably survives retention across runs).
