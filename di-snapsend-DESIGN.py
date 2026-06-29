#!/usr/bin/env python3
"""
di-snapsend — Snapper-native Btrfs send/receive replication (DESIGN SKELETON)
=============================================================================

PURPOSE
    A thin orchestration layer that replicates Snapper-created Btrfs snapshots
    from the laptop to the server over SSH. It does NOT reimplement the Btrfs
    stream format or the network transport — it shells out to:

        btrfs send [-p PARENT] SRC | ssh SERVER "btrfs receive DST"

    ...and confines itself to the three things a wrapper actually has to get
    right: snapshot enumeration, parent selection, and partial-transfer cleanup
    + retention. This is the "why reinvent btrfs?" design: btrfs does the hard
    filesystem + stream work, ssh does the network, we do the bookkeeping.

WHY THIS EXISTS (vs btrbk / snbk)
    - btrbk: powerful but its config DSL doesn't map onto Snapper's
      <N>/snapshot layout cleanly (the integration seam we hit in practice).
    - snbk: Snapper-native but mirror-only retention + requires an out-of-Debian
      OBS repo.
    This tool reads Snapper's own on-disk layout DIRECTLY (no format guessing),
    stays in Debian main (only needs btrfs-progs + openssh, both in main),
    and lets us express whatever retention policy we want in plain Python.

=============================================================================
CORRECTNESS MODEL  (distilled from reading btrbk's source — the hard-won bits)
=============================================================================

These three rules are the entire reason a naive "send | receive; delete if it
looks broken" script is unsafe. Each is lifted from btrbk's actual logic.

(1) VALIDITY OF A RECEIVED SUBVOLUME
    A subvolume EXISTING on the target does NOT mean the transfer completed.
    btrfs-progs does not make receive atomic and does NOT auto-delete a failed
    receive. A correctly-received subvolume has BOTH:
        - readonly == true
        - received_uuid is SET (not '-')
    A "garbled" (partial/interrupted) subvolume is the opposite:
        - readonly == false  AND  received_uuid == '-'
    => After every receive, verify both. If garbled, WE must delete it by hand.
       (btrbk btrfs:1572-1591)

(2) PARENT ELIGIBILITY ("correlation")
    A laptop snapshot S may be used as `-p` parent for an incremental ONLY if
    the server holds a snapshot that is "correlated" with S — i.e. the same
    Btrfs lineage. Correlation (both sides must be readonly) holds when:
        S.uuid          == T.received_uuid   (T was received from S), OR
        T.uuid          == S.received_uuid,  OR
        S.received_uuid == T.received_uuid  (both received from a common src)
    => The parent for sending snapshot N is the NEWEST laptop snapshot that is
       both (a) older than N and (b) correlated with something on the server.
       (btrbk btrfs:2587 _is_correlated)

(3) RETENTION PRUNE GUARD
    Retention must NEVER delete the snapshot that is the current newest
    correlated pair — on EITHER side — because it is the parent the next
    incremental depends on. Delete it and the next run silently degrades to a
    full send (or fails). The newest common/correlated snapshot is pinned.
    (btrbk keeps `preserve_min latest`; same idea, enforced explicitly here.)

=============================================================================
DATA WE READ (no guessing — Snapper tells us everything)
=============================================================================
    Snapper layout (confirm on millionaire with `ls -la /home/.snapshots/`):
        /<mountpoint>/.snapshots/<N>/snapshot   <- the actual RO subvolume
        /<mountpoint>/.snapshots/<N>/info.xml   <- Snapper metadata (num, date)

    Per-subvolume Btrfs identity comes from:
        btrfs subvolume show <path>     -> uuid, parent_uuid, received_uuid,
                                           readonly flag

    Server inventory comes from (over ssh):
        btrfs subvolume show <each received dir>   (same fields)

=============================================================================
TRANSFER PIPELINE
=============================================================================
    Full send (first time / no correlated parent):
        btrfs send  SRC          | mbuffer | ssh SERVER "sudo btrfs receive DST"
    Incremental (have a correlated parent P):
        btrfs send -p P SRC      | mbuffer | ssh SERVER "sudo btrfs receive DST"

    mbuffer is optional (throughput smoothing + progress). The pipe is the
    network: btrfs is transport-agnostic, ssh moves the bytes.

    FAILURE HANDLING (the gotcha you asked about — and it IS basically
    "detect + delete + tidy", just done via rule (1) above, not guesswork):
        - capture pipe exit status of EVERY stage (PIPESTATUS-equivalent)
        - after receive, run rule (1) validity check on the target subvol
        - if invalid/garbled: ssh SERVER delete the garbled subvol, log, abort
        - never advance the parent pointer past an unverified transfer
"""

from __future__ import annotations
import subprocess
import sys
import os
import json
import socket
from dataclasses import dataclass, field
from typing import Optional


# ============================================================================
# CONFIG  (will become CLI flags > env > defaults, di-* three-tier precedence)
# ============================================================================

@dataclass(frozen=True)
class Config:
    """
    Populated from /etc/snapsend/config (TOML) at startup — NOT hard-coded.
    Operational flags (--server/--dry-run/--subvol) override via three-tier
    precedence; retention is file-only policy (see SPEC §8/§9).
    """
    server_host: str
    server_ssh_port: int = 22
    server_user: str = "snapsend"       # dedicated least-priv transport user
    ssh_key: str = "/etc/snapsend/ssh/id_ed25519"
    # subvol-name -> {"mountpoint": str, "recv_dir": str}
    subvols: dict = field(default_factory=lambda: {
        "home": {"mountpoint": "/home", "recv_dir": "/srv/snapshots-recv/home"},
        "root": {"mountpoint": "/",     "recv_dir": "/srv/snapshots-recv/root"},
    })
    # TARGET-ONLY retention (Decision 1): source is Snapper-owned, never pruned
    # here. Per-subvol policy; falls back to "default". Keys: keep_hourly (finest,
    # default-disabled on upgrade), keep_daily, keep_weekly, keep_monthly. The
    # pinned parent (Rule 3) is always excluded. New-install defaults shown below.
    retention: dict = field(default_factory=lambda: {
        "default": {"keep_hourly": 24, "keep_daily": 14, "keep_weekly": 8, "keep_monthly": 6},
        "root":    {"keep_hourly": 24, "keep_daily": 30, "keep_weekly": 12, "keep_monthly": 12},
    })
    use_mbuffer: bool = True
    dry_run: bool = False

    # Non-Btrfs boot tier (EFI=FAT32, /boot=ext4): rsync'd as a single current
    # mirror, NOT versioned, NOT through the snapshot machinery (SPEC §11).
    # Target lands under the server's receive area so the downstream restic tier
    # versions it for free. Set boot_backup_enabled=False to skip.
    boot_backup_enabled: bool = True
    boot_paths: tuple = ("/boot", "/boot/efi")   # rsync these (efi nested last)
    boot_recv_base: str = "/srv/snapshots-recv"  # + /<hostname>/{boot,boot-efi}

    def retention_for(self, subvol_name: str) -> dict:
        """Per-subvol retention with fallback to default."""
        return self.retention.get(subvol_name, self.retention["default"])


def load_config(path: str = "/etc/snapsend/config") -> dict:
    """
    Parse the TOML config into kwargs for Config. SKELETON — claude-code:
    use tomllib (stdlib 3.11+). Map [server], [subvolumes.*], [retention.*]
    tables onto the Config fields. Apply CLI/env overrides AFTER loading.
    """
    raise NotImplementedError("claude-code: tomllib parse -> Config kwargs")


# ============================================================================
# SUBVOLUME IDENTITY  (the fields rules 1-3 depend on)
# ============================================================================

@dataclass
class Subvol:
    """One Btrfs subvolume's identity, parsed from `btrfs subvolume show`."""
    path: str
    uuid: str
    parent_uuid: str            # '-' if none
    received_uuid: str          # '-' if not a received subvol
    readonly: bool
    # Snapper number + timestamp when this is a snapper snapshot (else None).
    snapper_num: Optional[int] = None
    snapper_time: Optional[str] = None

    # --- rule (1): is this a cleanly-received target subvol? ---------------
    @property
    def is_valid_received(self) -> bool:
        """A clean receive is readonly AND has received_uuid set."""
        return self.readonly and self.received_uuid != "-"

    @property
    def is_garbled(self) -> bool:
        """Partial/interrupted receive: writable AND no received_uuid."""
        return (not self.readonly) and self.received_uuid == "-"


# --- rule (2): correlation test (parent eligibility) -----------------------
def is_correlated(a: Subvol, b: Subvol) -> bool:
    """
    True if a and b are the same Btrfs lineage and thus a valid send parent
    pairing. Both must be readonly. (btrbk _is_correlated, btrfs:2587)
    """
    if not (a.readonly and b.readonly):
        return False
    return (
        a.uuid == b.received_uuid
        or b.uuid == a.received_uuid
        or (a.received_uuid != "-" and a.received_uuid == b.received_uuid)
    )


# ============================================================================
# SHELL HELPERS
# ============================================================================

def run_local(argv: list[str], *, check: bool = True) -> subprocess.CompletedProcess:
    """Run a local command, capturing output. Logging stub — flesh out."""
    # TODO(claude-code): structured logging to /var/log/snapsend.log
    return subprocess.run(argv, check=check, capture_output=True, text=True)


def ssh_argv(cfg: Config, remote_cmd: str) -> list[str]:
    """Build the ssh invocation for a remote command string."""
    return [
        "ssh", "-i", cfg.ssh_key, "-p", str(cfg.server_ssh_port),
        "-o", "BatchMode=yes",
        f"{cfg.server_user}@{cfg.server_host}",
        remote_cmd,
    ]


def run_remote(cfg: Config, remote_cmd: str, *, check: bool = True):
    """Run a command on the server over ssh."""
    return run_local(ssh_argv(cfg, remote_cmd), check=check)


# ============================================================================
# PARSING  `btrfs subvolume show`
# ============================================================================

def parse_subvolume_show(text: str, path: str) -> Subvol:
    """
    Parse the human output of `btrfs subvolume show`. We need uuid, parent_uuid,
    received_uuid, and the readonly flag.

    NOTE(claude-code): prefer parsing reliably. Consider `btrfs subvolume show`
    line-by-line ("UUID:", "Parent UUID:", "Received UUID:", "Flags:"). The
    readonly state is in "Flags:" (contains 'readonly') on modern btrfs-progs;
    cross-check against `btrfs property get <path> ro`.
    """
    fields = {"uuid": "-", "parent_uuid": "-", "received_uuid": "-"}
    readonly = False
    for line in text.splitlines():
        s = line.strip()
        if s.startswith("UUID:"):
            fields["uuid"] = s.split(":", 1)[1].strip()
        elif s.startswith("Parent UUID:"):
            fields["parent_uuid"] = s.split(":", 1)[1].strip()
        elif s.startswith("Received UUID:"):
            fields["received_uuid"] = s.split(":", 1)[1].strip()
        elif s.startswith("Flags:"):
            readonly = "readonly" in s.lower()
    return Subvol(
        path=path,
        uuid=fields["uuid"],
        parent_uuid=fields["parent_uuid"],
        received_uuid=fields["received_uuid"],
        readonly=readonly,
    )


def show_local(path: str) -> Subvol:
    cp = run_local(["btrfs", "subvolume", "show", path])
    return parse_subvolume_show(cp.stdout, path)


def show_remote(cfg: Config, path: str) -> Optional[Subvol]:
    cp = run_remote(cfg, f"sudo btrfs subvolume show {path}", check=False)
    if cp.returncode != 0:
        return None
    return parse_subvolume_show(cp.stdout, path)


# ============================================================================
# ENUMERATION
# ============================================================================

def list_snapper_snapshots(mountpoint: str) -> list[Subvol]:
    """
    Read Snapper's snapshots directly from disk:
        <mountpoint>/.snapshots/<N>/snapshot
    Parse <N> from the dir name; pull Btrfs identity via subvolume show.
    Timestamp can come from info.xml or the dir mtime.

    NOTE(claude-code): only RO snapshots are sendable. Snapper snapshots are
    RO by default; skip any that are RW (rule 1/2 require readonly).
    Confirm exact path on millionaire — memory says nested @home/.snapshots.
    """
    snaps: list[Subvol] = []
    snap_root = os.path.join(mountpoint, ".snapshots")
    if not os.path.isdir(snap_root):
        return snaps
    for entry in sorted(os.listdir(snap_root), key=_int_or_zero):
        if not entry.isdigit():
            continue
        sub_path = os.path.join(snap_root, entry, "snapshot")
        if not os.path.isdir(sub_path):
            continue
        sv = show_local(sub_path)
        sv.snapper_num = int(entry)
        snaps.append(sv)
    return snaps


def list_target_snapshots(cfg: Config, recv_dir: str) -> list[Subvol]:
    """List received subvolumes on the server under recv_dir (over ssh)."""
    cp = run_remote(cfg, f"sudo ls -1 {recv_dir}", check=False)
    out: list[Subvol] = []
    if cp.returncode != 0:
        return out
    for name in cp.stdout.split():
        if name.endswith(".latest"):
            continue
        if name.startswith(".incoming-"):  # in-flight staging dir — skip
            continue
        sv = show_remote(cfg, os.path.join(recv_dir, name))
        if sv:
            out.append(sv)
    return out


def _int_or_zero(s: str) -> int:
    return int(s) if s.isdigit() else 0


# ============================================================================
# PARENT SELECTION  (rule 2)
# ============================================================================

def choose_parent(
    snapshot: Subvol,
    source_snaps: list[Subvol],
    target_snaps: list[Subvol],
) -> Optional[Subvol]:
    """
    Pick the best `-p` parent for `snapshot`: the NEWEST source snapshot that is
    (a) strictly older than `snapshot` and (b) correlated with some snapshot
    already on the target. Returns None -> must do a full send.
    (btrbk picks the latest correlated common parent.)
    """
    candidates = [
        s for s in source_snaps
        if s.snapper_num is not None
        and snapshot.snapper_num is not None
        and s.snapper_num < snapshot.snapper_num
        and any(is_correlated(s, t) for t in target_snaps)
    ]
    if not candidates:
        return None
    return max(candidates, key=lambda s: s.snapper_num)


# ============================================================================
# TRANSFER  (the send|receive pipe + rule 1 verification)
# ============================================================================

def send_receive(
    cfg: Config,
    snapshot: Subvol,
    recv_dir: str,
    parent: Optional[Subvol],
) -> bool:
    """
    Stream one snapshot to the server, then VERIFY it landed cleanly (rule 1).
    Returns True only on a verified-good receive. On garble, deletes the
    partial subvol on the server and returns False.

    Pipeline:
        btrfs send [-p PARENT] SNAP | [mbuffer |] ssh SERVER "sudo btrfs receive DST"
    """
    send_cmd = ["btrfs", "send"]
    if parent is not None:
        send_cmd += ["-p", parent.path]
    send_cmd += [snapshot.path]

    # DECIDED naming scheme (see SPEC §4): <snapper_num>-<short_uuid>
    #   - snapper_num: readable, mirrors Snapper's own numbering at send time
    #   - short_uuid:  first 8 hex of source uuid; guarantees uniqueness on the
    #                  server even as laptop numbers churn under retention, and
    #                  ties the dir name to the correlation key.
    short_uuid = snapshot.uuid.split("-")[0]
    received_name = f"{snapshot.snapper_num}-{short_uuid}"

    # DECIDED receive strategy (SPEC §5.2): Snapper's subvol is literally named
    # "snapshot", so every naive receive collides. Receive into a per-transfer
    # staging dir, verify (Rule 1), then rename to the final name. Keeps the
    # final dir free of half-written subvols and makes the .latest repoint clean.
    staging_dir = os.path.join(recv_dir, f".incoming-{short_uuid}")
    final_path = os.path.join(recv_dir, received_name)
    recv_remote = f"sudo btrfs receive {staging_dir}"

    if cfg.dry_run:
        _log(f"[dry-run] would send: {' '.join(send_cmd)} | ssh ... {recv_remote}")
        return True

    run_remote(cfg, f"sudo mkdir -p {staging_dir}", check=False)

    # TODO(claude-code): run send_cmd and the ssh receive as a connected pipe,
    # capturing BOTH exit codes (Popen chaining + wait on each; check all
    # .returncode). mbuffer inserted between if cfg.use_mbuffer.
    pipe_ok = _run_pipe(send_cmd, ssh_argv(cfg, recv_remote), cfg.use_mbuffer)

    # The received subvol lands as <staging_dir>/snapshot (Snapper's subvol name).
    staged_path = os.path.join(staging_dir, "snapshot")

    # --- rule (1) verification, regardless of reported pipe status ----------
    staged_sv = show_remote(cfg, staged_path)

    if staged_sv is None or staged_sv.is_garbled or not staged_sv.is_valid_received:
        _log(f"ERROR: garbled/incomplete receive in {staging_dir} — cleaning up")
        run_remote(cfg, f"sudo btrfs subvolume delete {staged_path}", check=False)
        run_remote(cfg, f"sudo rmdir {staging_dir}", check=False)
        return False

    if not pipe_ok:
        # Pipe reported failure but subvol looks valid — be conservative.
        _log(f"WARN: pipe reported error though staged subvol looks valid; "
             f"discarding {staging_dir} and retrying next run")
        run_remote(cfg, f"sudo btrfs subvolume delete {staged_path}", check=False)
        run_remote(cfg, f"sudo rmdir {staging_dir}", check=False)
        return False

    # Verified clean: promote staging -> final name, then drop the staging dir.
    run_remote(cfg, f"sudo mv {staged_path} {final_path}", check=False)
    run_remote(cfg, f"sudo rmdir {staging_dir}", check=False)
    _update_latest_symlink(cfg, recv_dir, received_name)
    return True


def _run_pipe(send_argv, ssh_argv_list, use_mbuffer) -> bool:
    """
    Connect `btrfs send` -> [mbuffer ->] ssh receive, return True iff every
    stage exited 0. SKELETON — implement with Popen chaining + wait on all.
    """
    raise NotImplementedError("claude-code: implement piped exec w/ all exit codes")


def _update_latest_symlink(cfg: Config, recv_dir: str, name: str) -> None:
    """
    Maintain <recv_dir>/<subvol>.latest -> newest received subvol, so restic
    has a stable target (our own equivalent of btrbk's latest pointer).
    """
    base = os.path.basename(recv_dir.rstrip("/"))
    link = os.path.join(recv_dir, f"{base}.latest")
    run_remote(cfg, f"sudo ln -sfn {os.path.join(recv_dir, name)} {link}", check=False)


# ============================================================================
# BOOT TIER  (non-Btrfs: EFI=FAT32, /boot=ext4 — rsync mirror, not versioned)
# ============================================================================
# /boot and /boot/efi cannot be btrfs-sent (wrong fs, no snapshots). They are
# mirrored to the server with `rsync -aAX --delete` so DR can reconstruct a
# bootable system WITH our custom bootloader config. Single current copy only;
# the downstream restic-on-server tier picks it up from the receive area and
# versions it for free. Matches the original di-btrbk.sh boot/efi logic, ported
# from a USB target to the server and from a separate bash script into Python.
# ============================================================================

def boot_backup(cfg: Config, hostname: str) -> bool:
    """
    rsync each non-Btrfs boot path to /srv/snapshots-recv/<hostname>/<name>/ on
    the server over SSH. Returns True iff every path synced cleanly. Fail-safe:
    a failure is logged/warned but does not abort the rest of the run (boot
    backup is independent of the snapshot tiers).

    Mapping (matches original): /boot -> boot/ , /boot/efi -> boot-efi/
    """
    if not cfg.boot_backup_enabled:
        _log("Boot backup disabled — skipping.")
        return True

    _log(f"== Boot mirror ({', '.join(cfg.boot_paths)} -> "
         f"{cfg.boot_recv_base}/{hostname}/) ==")

    rsh = f"ssh -i {cfg.ssh_key} -p {cfg.server_ssh_port}"
    all_ok = True

    for src in cfg.boot_paths:
        # /boot -> "boot", /boot/efi -> "boot-efi"  (stable, collision-free)
        name = src.strip("/").replace("/", "-")
        dest = f"{cfg.boot_recv_base}/{hostname}/{name}"

        # Ensure the destination dir exists on the server (rsync won't mkdir -p
        # the parent chain on the remote side).
        run_remote(cfg, f"sudo mkdir -p {dest}", check=False)

        # Trailing slashes matter: "<src>/" copies contents into "<dest>/".
        # -aAX preserves perms/ACLs/xattrs; --delete makes it a true mirror.
        # rsync runs under sudo on the remote so it can write the receive area.
        rsync_argv = [
            "rsync", "-aAX", "--delete",
            "-e", rsh,
            "--rsync-path", "sudo rsync",
            f"{src.rstrip('/')}/",
            f"{cfg.server_user}@{cfg.server_host}:{dest}/",
        ]

        if cfg.dry_run:
            _log(f"[dry-run] would rsync: {' '.join(rsync_argv)}")
            continue

        # TODO(claude-code): run rsync_argv, capture rc + stderr, log via the
        # structured logger. EFI (FAT32) has no perms/ownership/xattrs, so -aAX
        # will emit benign warnings on the efi path — treat rsync rc 0 and 24
        # (vanished source files) as success; warn (don't fail run) otherwise.
        cp = run_local(rsync_argv, check=False)
        if cp.returncode in (0, 24):
            _log(f"  {src} mirrored to {dest}")
        else:
            _log(f"  WARN: rsync of {src} failed (rc={cp.returncode}); "
                 "continuing — boot backup is independent of snapshot tiers")
            all_ok = False

    return all_ok




# ============================================================================
# RETENTION  (rule 3 — never prune the pinned newest correlated pair)
# ============================================================================

def apply_retention(
    cfg: Config,
    subvol_name: str,
    source_snaps: list[Subvol],
    target_snaps: list[Subvol],
    recv_dir: str,
) -> None:
    """
    Prune the TARGET ONLY (Decision 1) per the per-subvol policy from config.
    Source snapshots are NEVER pruned here — Snapper owns local retention.

    PIN the newest correlated pair so the next incremental's parent survives
    (Rule 3), then delete target snapshots beyond keep_hourly/daily/weekly/monthly,
    excluding the pinned target. For root, additionally avoid orphaning half a
    pre/post pair (SPEC §4).

    SKELETON — claude-code: implement the keep_* bucketing. Needs real
    timestamps per target snapshot (from received info.xml or subvol creation
    time) to assign hourly/daily/weekly/monthly buckets.
    """
    policy = cfg.retention_for(subvol_name)
    pinned_source, pinned_target = _newest_correlated_pair(source_snaps, target_snaps)
    _log(f"[{subvol_name}] retention {policy}; pinned target="
         f"{getattr(pinned_target, 'path', None)}")
    # TODO(claude-code): build TARGET delete set honoring `policy` AND excluding
    # pinned_target; for root also keep pre/post pairs together. Delete via
    # `ssh ... sudo btrfs subvolume delete`. NEVER touch source_snaps.
    raise NotImplementedError("claude-code: target-only retention with pin")


def _newest_correlated_pair(
    source_snaps: list[Subvol],
    target_snaps: list[Subvol],
) -> tuple[Optional[Subvol], Optional[Subvol]]:
    """Find the newest (source, target) pair that is correlated — the parent
    the next incremental will use. This pair is never pruned."""
    best: tuple[Optional[Subvol], Optional[Subvol]] = (None, None)
    best_num = -1
    for s in source_snaps:
        for t in target_snaps:
            if is_correlated(s, t) and (s.snapper_num or -1) > best_num:
                best_num = s.snapper_num or -1
                best = (s, t)
    return best


# ============================================================================
# MAIN PER-SUBVOL FLOW
# ============================================================================

def replicate_subvol(cfg: Config, name: str, mountpoint: str, recv_dir: str) -> bool:
    """Full flow for one subvol: enumerate -> diff -> send missing -> retention."""
    _log(f"== Replicating '{name}' ({mountpoint} -> {recv_dir}) ==")
    run_remote(cfg, f"sudo mkdir -p {recv_dir}", check=False)

    source_snaps = list_snapper_snapshots(mountpoint)
    if not source_snaps:
        _log(f"No snapper snapshots under {mountpoint}/.snapshots — nothing to do.")
        return True
    target_snaps = list_target_snapshots(cfg, recv_dir)

    # Which source snapshots are not yet on the target (by correlation)?
    missing = [
        s for s in source_snaps
        if not any(is_correlated(s, t) for t in target_snaps)
    ]
    # Send oldest-missing-first so each can parent the next.
    missing.sort(key=lambda s: s.snapper_num or 0)

    ok = True
    for snap in missing:
        parent = choose_parent(snap, source_snaps, target_snaps)
        kind = "incremental" if parent else "FULL"
        _log(f"  sending #{snap.snapper_num} ({kind}"
             + (f", parent #{parent.snapper_num}" if parent else "") + ")")
        if not send_receive(cfg, snap, recv_dir, parent):
            _log(f"  STOP: transfer failed at #{snap.snapper_num}; "
                 "leaving chain intact for retry.")
            ok = False
            break
        # refresh target view so the next iteration sees the new parent
        target_snaps = list_target_snapshots(cfg, recv_dir)

    if ok:
        apply_retention(cfg, name, source_snaps, target_snaps, recv_dir)
    return ok


def main(argv: list[str]) -> int:
    # TODO(claude-code):
    #   1. argparse: --server, --dry-run, --subvol <name>, --skip-boot
    #      (three-tier precedence, flag > env > config-file value).
    #   2. cfg_kwargs = load_config("/etc/snapsend/config"); apply overrides;
    #      Config(**cfg_kwargs).
    #   3. acquire /var/lock/snapsend.lock (flock) around the whole run.
    #   4. for name, sv in cfg.subvols.items():
    #          replicate_subvol(cfg, name, sv["mountpoint"], sv["recv_dir"])
    #      (honor --subvol filter; aggregate per-subvol success).
    #   5. boot_backup(cfg, socket.gethostname())   # once per host, after subvols
    #   6. exit code reflects aggregate success (snapshot tiers + boot tier).
    raise NotImplementedError("claude-code: wire CLI + load_config + loop "
                              "replicate_subvol, then boot_backup")


def _log(msg: str) -> None:
    print(msg, file=sys.stderr)


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
