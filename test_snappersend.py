"""
test_snappersend — pytest suite for snappersend.

Run:  SNAPPERSEND_QUIET=1 python3 -m pytest test_snappersend.py -q

The highest-value surface is the parent-tree promote-on-confirmed-send invariant, so
the bulk of this file drives the real `replicate_subvol` orchestration against an
in-memory fake of the two stateful worlds it touches — the destination's received
snapshots and the source's parent-clone tree — by monkeypatching the btrfs/ssh IO
boundary. The pure logic (correlation, WYSIWYG GFS incl. yearly, config parsing) is
unit-tested directly. Real end-to-end btrfs send/receive is covered separately by the
VM validation (see the build report), not here.
"""

import importlib.machinery
import importlib.util
import os
import sys

import pytest

os.environ.setdefault("SNAPPERSEND_QUIET", "1")


# --- load the extensionless script as a module -------------------------------
def _load():
    loader = importlib.machinery.SourceFileLoader(
        "snappersend", os.path.join(os.path.dirname(__file__), "snappersend"))
    spec = importlib.util.spec_from_loader("snappersend", loader)
    mod = importlib.util.module_from_spec(spec)
    sys.modules["snappersend"] = mod   # register before exec so @dataclass resolves
    loader.exec_module(mod)
    return mod


ss = _load()


# ============================================================================
# Subvol factories
# ============================================================================

def src_snap(num, when, uuid=None, typ=None, pre=None):
    """A Snapper source snapshot (readonly, its own uuid, not received)."""
    sv = ss.Subvol(path=f"/m/.snapshots/{num}/snapshot",
                   uuid=uuid or f"src-{num:04d}", parent_uuid="-",
                   received_uuid="-", readonly=True)
    sv.snapper_num = num
    sv.info_time = when
    sv.snapper_type = typ
    sv.pre_num = pre
    return sv


def clone_snap(num, when, uuid, path=None):
    """A parent-tree clone: its OWN uuid (what the destination correlates on),
    carrying the original snapshot's num/time."""
    sv = ss.Subvol(path=path or f"/.snappersend/x/{num}", uuid=uuid,
                   parent_uuid=f"src-{num:04d}", received_uuid="-", readonly=True)
    sv.snapper_num = num
    sv.info_time = when
    return sv


def target_of(clone, path=None):
    """The destination's received copy of a sent clone: received_uuid == clone.uuid."""
    sv = ss.Subvol(path=path or f"/recv/{clone.snapper_num}/snapshot",
                   uuid=f"dst-{clone.snapper_num:04d}", parent_uuid="-",
                   received_uuid=clone.uuid, readonly=True)
    sv.snapper_num = clone.snapper_num
    sv.info_time = clone.info_time
    return sv


def dt(y, m, d, h=12, mi=0):
    from datetime import datetime
    return datetime(y, m, d, h, mi)


# ============================================================================
# FakeWorld — drives the real replicate_subvol against in-memory state
# ============================================================================

class FakeWorld:
    """Monkeypatches the IO boundary so replicate_subvol runs its REAL orchestration
    against in-memory `tree` (parent clones) and `dest` (received snapshots)."""

    def __init__(self, monkeypatch, source, tree=None, dest=None,
                 send_succeeds=True, crash_after_send=False, reachable=True):
        self.source = source
        self.tree = list(tree or [])
        self.dest = list(dest or [])
        self.send_succeeds = send_succeeds
        self.crash_after_send = crash_after_send
        self.reachable = reachable
        self.sends = []           # (clone_uuid, parent_uuid_or_None)
        self.retention_calls = []
        self._uuid_seq = 1000

        monkeypatch.setattr(ss, "list_snapper_snapshots", lambda mp: list(self.source))
        monkeypatch.setattr(ss, "list_target_snapshots", self._list_target)
        monkeypatch.setattr(ss, "list_parent_clones", lambda cfg, n: self._clones())
        monkeypatch.setattr(ss, "make_clone", self._make_clone)
        monkeypatch.setattr(ss, "delete_clone", self._delete_clone)
        monkeypatch.setattr(ss, "send_receive", self._send_receive)
        monkeypatch.setattr(ss, "run_remote", lambda *a, **k: None)
        monkeypatch.setattr(ss, "apply_retention", self._apply_retention)
        # prune_parent_clones is the REAL function (exercises list/delete via fakes).

    def _list_target(self, cfg, rd):
        # A transport failure raises RemoteUnreachable (as the real ssh-backed
        # lister does on rc 255) — distinct from a reachable-but-empty destination.
        if not self.reachable:
            raise ss.RemoteUnreachable("ssh: Could not resolve hostname dest")
        return list(self.dest)

    def _clones(self):
        from datetime import datetime
        return sorted(self.tree,
                      key=lambda c: (c.info_time or datetime.min, c.snapper_num or 0),
                      reverse=True)

    def _make_clone(self, cfg, name, snap):
        self._uuid_seq += 1
        c = clone_snap(snap.snapper_num, snap.info_time, uuid=f"clone-{self._uuid_seq}",
                       path=f"/.snappersend/{name}/{snap.snapper_num}")
        # A re-clone replaces any orphan at the same label (same snapper_num here).
        self.tree = [t for t in self.tree if t.snapper_num != snap.snapper_num]
        self.tree.append(c)
        return c

    def _delete_clone(self, path):
        self.tree = [t for t in self.tree if t.path != path]

    def _send_receive(self, cfg, clone, recv_dir, parent):
        self.sends.append((clone.uuid, parent.uuid if parent else None))
        if not self.send_succeeds:
            return False
        # A verified-good receive lands a correlated target on the destination.
        self.dest.append(target_of(clone))
        if self.crash_after_send:
            # Simulate a crash AFTER the receive but before promotion/prune by
            # aborting the run right here (the clone is already in the tree).
            raise KeyboardInterrupt("simulated crash after send, before promote")
        return True

    def _apply_retention(self, cfg, name, source, target, clones, recv_dir):
        self.retention_calls.append((name, len(target), len(clones)))


@pytest.fixture
def cfg():
    return ss.Config(server_host="dest", parent_keep=2,
                     subvols={"home": {"mountpoint": "/home"}})


def run(cfg, world, name="home"):
    return ss.replicate_subvol(cfg, name, "/home", "/recv/home")


# ============================================================================
# (1) Failed send leaves the parent tree unchanged
# ============================================================================

def test_failed_send_leaves_parent_unchanged(monkeypatch, cfg):
    p = clone_snap(5, dt(2026, 6, 1), uuid="clone-existing")
    dest0 = [target_of(p)]
    src = [src_snap(5, dt(2026, 6, 1)), src_snap(6, dt(2026, 6, 2))]
    w = FakeWorld(monkeypatch, src, tree=[p], dest=dest0, send_succeeds=False)

    tree_before = {c.path for c in w.tree}
    ok = run(cfg, w)

    assert ok is False                                  # subvol reported failure
    assert w.sends and w.sends[0][1] == "clone-existing"  # sent -p the preserved parent
    assert {c.path for c in w.tree} == tree_before      # tree byte-for-byte unchanged
    assert any(c.uuid == "clone-existing" for c in w.tree)  # old parent still there
    assert w.retention_calls == []                      # no retention after failure
    # Next run off the SAME parent: now succeeding, it must reuse clone-existing.
    w.send_succeeds = True
    assert run(cfg, w) is True
    assert w.sends[-1][1] == "clone-existing"


# ============================================================================
# (2) Successful send promotes the new clone and prunes beyond parent_keep
# ============================================================================

def test_success_promotes_and_prunes(monkeypatch, cfg):
    # parent_keep=2; start with two old clones both already on the destination.
    c3 = clone_snap(3, dt(2026, 5, 30), uuid="clone-3")
    c4 = clone_snap(4, dt(2026, 5, 31), uuid="clone-4")
    dest0 = [target_of(c3), target_of(c4)]
    src = [src_snap(4, dt(2026, 5, 31)), src_snap(7, dt(2026, 6, 2))]
    w = FakeWorld(monkeypatch, src, tree=[c3, c4], dest=dest0)

    ok = run(cfg, w)
    assert ok is True
    # Sent #7 incrementally off the newest correlating clone (#4).
    assert w.sends[-1][1] == "clone-4"
    nums = sorted(c.snapper_num for c in w.tree)
    assert nums == [4, 7]                       # #7 promoted, #3 pruned (keep=2)
    assert any(c.snapper_num == 7 for c in w.tree)
    assert all(c.snapper_num != 3 for c in w.tree)
    assert w.retention_calls and w.retention_calls[-1][0] == "home"


def test_idempotent_rerun_sends_nothing(monkeypatch, cfg):
    # Newest snapper #7 already has a correlating clone on the destination.
    c7 = clone_snap(7, dt(2026, 6, 2), uuid="clone-7")
    w = FakeWorld(monkeypatch, [src_snap(7, dt(2026, 6, 2))],
                  tree=[c7], dest=[target_of(c7)])
    ok = run(cfg, w)
    assert ok is True
    assert w.sends == []                       # nothing sent
    assert w.retention_calls                   # but retention still runs


# ============================================================================
# (3) Diverged destination -> full send + reseed
# ============================================================================

def test_diverged_destination_full_send_and_reseed(monkeypatch, cfg):
    # We hold a clone, but the destination's snapshot does NOT correlate with it
    # (it was deleted/replaced) -> no shared parent -> full send.
    stale = clone_snap(4, dt(2026, 5, 31), uuid="clone-stale")
    foreign = ss.Subvol(path="/recv/zzz/snapshot", uuid="dst-x", parent_uuid="-",
                        received_uuid="totally-unrelated", readonly=True)
    foreign.snapper_num = 99
    w = FakeWorld(monkeypatch, [src_snap(8, dt(2026, 6, 3))],
                  tree=[stale], dest=[foreign])

    ok = run(cfg, w)
    assert ok is True
    assert w.sends and w.sends[-1][1] is None          # FULL send (no -p parent)
    assert any(c.snapper_num == 8 for c in w.tree)      # reseeded with #8
    # #8's clone now correlates with the destination (it was received).
    newest = max(w.tree, key=lambda c: c.snapper_num)
    assert any(ss.is_correlated(newest, t) for t in w.dest)


def test_first_run_empty_everything_full_send(monkeypatch, cfg):
    w = FakeWorld(monkeypatch, [src_snap(1, dt(2026, 6, 1))], tree=[], dest=[])
    ok = run(cfg, w)
    assert ok is True
    assert w.sends and w.sends[-1][1] is None           # full send
    assert [c.snapper_num for c in w.tree] == [1]       # seeded


# ============================================================================
# (3b) Unreachable destination != divergence — a transient transport failure
#      must NOT force a full send on the next run (regression for the real-VM
#      failure-decoupling bug: an empty target list from a down link looked like
#      divergence, so the newest was re-staged, its deterministic-label clone was
#      deleted, and correlation with the destination was lost).
# ============================================================================

def test_unreachable_destination_preserves_parent_and_recovers_incrementally(monkeypatch, cfg):
    # Steady state: newest #7 already confirmed-sent (its clone correlates on dest).
    c7 = clone_snap(7, dt(2026, 6, 2), uuid="clone-7")
    w = FakeWorld(monkeypatch, [src_snap(7, dt(2026, 6, 2))],
                  tree=[c7], dest=[target_of(c7)], reachable=False)
    tree_before = {c.path for c in w.tree}
    dest_before = {t.received_uuid for t in w.dest}

    # A run while the link is down must fail the subvol but touch NOTHING.
    assert run(cfg, w) is False
    assert w.sends == []                                # never even attempted a send
    assert {c.path for c in w.tree} == tree_before      # parent tree intact
    assert {t.received_uuid for t in w.dest} == dest_before
    assert w.retention_calls == []                      # no retention on abort
    assert any(c.uuid == "clone-7" for c in w.tree)     # confirmed parent survives

    # Link restored, newest still #7: must recognise it's already on the destination
    # and send nothing — NOT a forced full send.
    w.reachable = True
    assert run(cfg, w) is True
    assert w.sends == []                                # still nothing sent (idempotent)

    # And a genuinely new snapshot after recovery goes incrementally off #7.
    w.source = [src_snap(7, dt(2026, 6, 2)), src_snap(8, dt(2026, 6, 3))]
    assert run(cfg, w) is True
    assert w.sends[-1][1] == "clone-7"                  # incremental parent, not full


# ============================================================================
# (4) Crash after send, before promote, is safe (next run continues off it)
# ============================================================================

def test_crash_after_send_before_promote_is_safe(monkeypatch, cfg):
    p = clone_snap(5, dt(2026, 6, 1), uuid="clone-5")
    w = FakeWorld(monkeypatch, [src_snap(5, dt(2026, 6, 1)), src_snap(6, dt(2026, 6, 2))],
                  tree=[p], dest=[target_of(p)], crash_after_send=True)

    # The crash aborts mid-run; replicate_subvol surfaces it as a failed subvol.
    with pytest.raises(KeyboardInterrupt):
        run(cfg, w)
    # Reality after the crash: the destination HAS #6 and the clone IS in the tree
    # (clone-before-send), we just never pruned. Both are true:
    assert any(t.snapper_num == 6 for t in w.dest)
    assert any(c.snapper_num == 6 for c in w.tree)

    # Next run (no new snapshot): #6 is already correlated on the destination, so it
    # is a clean no-op, NOT a re-send and NOT a broken receive.
    w.crash_after_send = False
    w.sends.clear()               # ignore the crashed run's send; watch only new ones
    ok = run(cfg, w)
    assert ok is True
    assert w.sends == []          # nothing re-sent; the crash self-healed
    # And if a newer snapshot appears, it parents cleanly off the #6 clone.
    w.source.append(src_snap(7, dt(2026, 6, 3)))
    assert run(cfg, w) is True
    assert w.sends[-1][1] == [c.uuid for c in w.tree if c.snapper_num == 6][0]


# ============================================================================
# (5) WYSIWYG retention incl. yearly — pure GFS + pinned parent, no superset
# ============================================================================

def test_bucket_keep_yearly_tier(monkeypatch):
    # One snapshot per year for several years; keep_yearly=2 keeps the two newest.
    tgts = [target_of(clone_snap(n, dt(2020 + n, 6, 1), uuid=f"u{n}")) for n in range(5)]
    tgts.sort(key=lambda t: t.when, reverse=True)
    keep = ss._bucket_keep(tgts, 0, 0, 0, 0, keep_yearly=2)
    kept_years = sorted({t.when.year for t in tgts if t.path in keep})
    assert kept_years == [2023, 2024]            # only the two most recent years


def test_wysiwyg_no_source_backed_survivors(cfg):
    # 10 daily destination snapshots; keep_daily=3, everything else 0. Pure GFS must
    # keep EXACTLY 3 + the pinned parent — NOT extra ones just because a source
    # snapshot still exists (there is no "source-backed superset").
    from datetime import datetime
    clones = []
    dest = []
    for n in range(10):
        c = clone_snap(n, datetime(2026, 6, 1 + n, 12), uuid=f"c{n}")
        clones.append(c)
        dest.append(target_of(c))
    # A full set of correlated SOURCE snapshots still exists (superset bait).
    source = [src_snap(n, datetime(2026, 6, 1 + n, 12), uuid=f"src-{n:04d}")
              for n in range(10)]
    # Make the clones correlate with their source by sharing received_uuid lineage:
    # here source uuid == clone.parent — but correlation for retention is source<->
    # target. Give targets a received_uuid that also equals a source uuid to bait the
    # old superset rule. (Even so, pure GFS must ignore source presence.)

    cfg2 = ss.Config(server_host="d", retention={"home": {
        "keep_hourly": 0, "keep_daily": 3, "keep_weekly": 0,
        "keep_monthly": 0, "keep_yearly": 0}, "default": {}})

    # Capture deletions by faking run_remote.
    calls = {"deleted": []}

    def fake_run_remote(c, cmd, check=True):
        if "subvolume delete" in cmd:
            calls["deleted"].append(cmd)
        class R: returncode = 0; stderr = ""
        return R()
    ss_run_remote = ss.run_remote
    ss.run_remote = fake_run_remote
    try:
        ss.apply_retention(cfg2, "home", source, dest, clones, "/recv/home")
    finally:
        ss.run_remote = ss_run_remote

    # 10 targets, keep_daily=3 distinct days -> keep 3, plus pinned parent (newest,
    # already among the 3) -> exactly 7 pruned.
    assert len(calls["deleted"]) == 7


def test_pinned_parent_survives_when_gfs_would_drop_it(cfg):
    # keep_daily=1 would keep only the newest day; but the pinned parent (the dest
    # copy correlating with the newest clone) must also survive even if it's older.
    from datetime import datetime
    older = clone_snap(1, datetime(2026, 1, 1, 12), uuid="c-old")
    # The newest clone correlates with an OLD target (simulate the chain base being
    # behind the newest destination snapshot).
    clones = [older]
    t_old = target_of(older)                         # correlates with the parent clone
    t_new = target_of(clone_snap(9, datetime(2026, 6, 9, 12), uuid="c-new"))
    dest = [t_old, t_new]

    pin = ss.pinned_target(clones, dest)
    assert pin is t_old                              # pinned = base of next incremental

    calls = {"deleted": []}

    def fake_run_remote(c, cmd, check=True):
        if "subvolume delete" in cmd:
            calls["deleted"].append(cmd)
        class R: returncode = 0; stderr = ""
        return R()
    cfg2 = ss.Config(server_host="d", retention={"default": {
        "keep_hourly": 0, "keep_daily": 1, "keep_weekly": 0,
        "keep_monthly": 0, "keep_yearly": 0}})
    saved = ss.run_remote
    ss.run_remote = fake_run_remote
    try:
        ss.apply_retention(cfg2, "home", [], dest, clones, "/recv/home")
    finally:
        ss.run_remote = saved
    # t_new kept by GFS (newest day), t_old kept by the pin -> nothing pruned.
    assert calls["deleted"] == []


# ============================================================================
# (6) Snapper-schema config parsing incl. YEARLY + ignored keys
# ============================================================================

def test_config_parsing_timeline_and_ignored_keys(tmp_path):
    p = tmp_path / "config"
    p.write_text(
        'SERVER_HOST="dest-host"\n'
        'SUBVOLUMES="root:/ home:/home"\n'
        'TIMELINE_LIMIT_HOURLY="24"\n'
        'TIMELINE_LIMIT_DAILY="14"\n'
        'TIMELINE_LIMIT_WEEKLY="8"\n'
        'TIMELINE_LIMIT_MONTHLY="6"\n'
        'TIMELINE_LIMIT_YEARLY="2"\n'
        'ROOT_TIMELINE_LIMIT_DAILY="30"\n'
        # Snapper-only keys that must be ignored without error:
        'SUBVOLUME="/"\n'
        'FSTYPE="btrfs"\n'
        'NUMBER_LIMIT="15"\n'
        'SYNC_ACL="yes"\n'
    )
    kw = ss.load_config(str(p))
    cfg = ss.Config(**kw)
    assert cfg.server_host == "dest-host"
    assert set(cfg.subvols) == {"root", "home"}
    home = cfg.retention_for("home")
    assert home == {"keep_hourly": 24, "keep_daily": 14, "keep_weekly": 8,
                    "keep_monthly": 6, "keep_yearly": 2}
    # Per-subvol override replaces ONE tier; the rest inherit the default.
    root = cfg.retention_for("root")
    assert root["keep_daily"] == 30
    assert root["keep_hourly"] == 24 and root["keep_yearly"] == 2


def test_config_missing_server_host_raises(tmp_path):
    p = tmp_path / "config"
    p.write_text('TIMELINE_LIMIT_DAILY="7"\n')
    with pytest.raises(KeyError):
        ss.load_config(str(p))


def test_config_non_numeric_tier_degrades(tmp_path):
    p = tmp_path / "config"
    p.write_text('SERVER_HOST="d"\nTIMELINE_LIMIT_DAILY="oops"\n'
                 'TIMELINE_LIMIT_YEARLY="3"\n')
    cfg = ss.Config(**ss.load_config(str(p)))
    d = cfg.retention_for("home")
    assert d["keep_daily"] == 0       # non-numeric -> disabled, no crash
    assert d["keep_yearly"] == 3


# ============================================================================
# (7) Carried-over correctness logic
# ============================================================================

def test_valid_received_and_garbled_detection():
    good = ss.Subvol("/p", "u", "-", "ru", readonly=True)
    assert good.is_valid_received and not good.is_garbled
    garbled = ss.Subvol("/p", "u", "-", "-", readonly=False)
    assert garbled.is_garbled and not garbled.is_valid_received
    # RW with a received_uuid, or RO without one, is neither (ambiguous) — never
    # treated as a clean receive.
    weird = ss.Subvol("/p", "u", "-", "ru", readonly=False)
    assert not weird.is_valid_received


def test_correlation_by_uuid_with_dash_guards():
    a = ss.Subvol("/a", "AAAA", "-", "-", readonly=True)
    b = ss.Subvol("/b", "BBBB", "-", "AAAA", readonly=True)   # b received a
    assert ss.is_correlated(a, b) and ss.is_correlated(b, a)
    # '-' must never correlate with '-'
    x = ss.Subvol("/x", "-", "-", "-", readonly=True)
    y = ss.Subvol("/y", "-", "-", "-", readonly=True)
    assert not ss.is_correlated(x, y)
    # readonly is required on both sides
    rw = ss.Subvol("/c", "CCCC", "-", "-", readonly=False)
    d = ss.Subvol("/d", "DDDD", "-", "CCCC", readonly=True)
    assert not ss.is_correlated(rw, d)


def test_choose_parent_clone_picks_newest_correlating():
    c_old = clone_snap(3, dt(2026, 6, 1), uuid="old")
    c_new = clone_snap(7, dt(2026, 6, 5), uuid="new")
    orphan = clone_snap(8, dt(2026, 6, 6), uuid="orphan")   # not on destination
    dest = [target_of(c_old), target_of(c_new)]
    chosen = ss.choose_parent_clone([orphan, c_new, c_old], dest)
    assert chosen is c_new            # newest that correlates; orphan skipped
    assert ss.choose_parent_clone([orphan], dest) is None   # divergence -> full


def test_receive_in_place_naming(monkeypatch, cfg):
    # send_receive must build <date>-<offset>-<num>-<shortuuid>/snapshot and receive
    # in place (no mv, no RW flip). Capture the receive path.
    captured = {}

    def fake_run_pipe(send_argv, ssh_list, use_mbuffer):
        captured["send"] = send_argv
        captured["ssh"] = ssh_list
        return True

    def fake_show_remote(c, path):
        captured["final_path"] = path
        return ss.Subvol(path, "ruuid", "-", "ruuid", readonly=True)  # valid received

    monkeypatch.setattr(ss, "_run_pipe", fake_run_pipe)
    monkeypatch.setattr(ss, "show_remote", fake_show_remote)
    monkeypatch.setattr(ss, "run_remote", lambda *a, **k: None)
    monkeypatch.setattr(ss, "_update_latest_symlink", lambda *a, **k: None)

    clone = clone_snap(7, dt(2026, 6, 27, 13, 0), uuid="abcdef12-3456-7890")
    ok = ss.send_receive(cfg, clone, "/recv/home", parent=None)
    assert ok is True
    # final receive path is <recv>/<date>-<offset>-7-abcdef12/snapshot, received in place
    assert captured["final_path"].endswith("-7-abcdef12/snapshot")
    assert "/recv/home/2026" in captured["final_path"]
    # full send -> no -p in the send argv
    assert "-p" not in captured["send"]


def test_second_precision_names_order_same_minute():
    # Two snapshots in the SAME MINUTE but different seconds must produce distinct
    # labels that round-trip to distinct, correctly-ordered times. (Minute precision
    # tied them, and with Snapper recycling a number the tiebreak misordered "newest".)
    from datetime import datetime
    a = src_snap(90, datetime(2026, 7, 1, 1, 21, 13), uuid="aaaa1111-x")
    b = src_snap(91, datetime(2026, 7, 1, 1, 21, 41), uuid="bbbb2222-x")  # later second
    la, lb = ss._clone_label(a), ss._clone_label(b)
    assert la != lb                                   # distinct labels
    # parse the date part back out and confirm b (later second) > a
    ma = ss._NAME_RE.match(la); mb = ss._NAME_RE.match(lb)
    ta = ss._parse_dt_name(ma.group(1)); tb = ss._parse_dt_name(mb.group(1))
    assert ta is not None and tb is not None and tb > ta

    # Even though a has the LOWER snapper num that here belongs to the NEWER snapshot
    # (recycled number), choose_parent_clone must pick by time, i.e. b is newest...
    # and a real recycled case: the newer snapshot has the lower number.
    newer_lower_num = src_snap(90, datetime(2026, 7, 1, 1, 21, 41), uuid="newer-90")
    older_high_num = src_snap(91, datetime(2026, 7, 1, 1, 21, 13), uuid="older-91")
    cn = clone_snap(90, newer_lower_num.info_time, uuid="cn")
    co = clone_snap(91, older_high_num.info_time, uuid="co")
    dest = [target_of(cn), target_of(co)]
    # time-based selection picks the genuinely newest (cn, the recycled #90)
    assert ss.choose_parent_clone([co, cn], dest) is cn


def test_clone_label_unique_per_snapshot_not_per_number():
    # Two DIFFERENT snapshots that share a recycled number + same second must NOT
    # collide on a clone label (else make_clone would delete a still-needed parent).
    from datetime import datetime
    when = datetime(2026, 7, 1, 1, 21, 41)
    s1 = src_snap(90, when, uuid="11111111-aaaa")
    s2 = src_snap(90, when, uuid="22222222-bbbb")   # same num + same second, diff uuid
    assert ss._clone_label(s1) != ss._clone_label(s2)


# ============================================================================
# (8) --report: read-only tier/verdict view derived from the SAME bucketing logic
# ============================================================================

def _parse_verdicts(text):
    """Pull {snapper_num: 'KEEP'|'PRUNE'} out of the report's Destination section."""
    out = {}
    for line in text.splitlines():
        s = line.strip()
        for v in ("KEEP", "PRUNE"):
            if s.startswith(v) and "#" in s:
                out[int(s.split("#", 1)[1].split()[0])] = v
    return out


def _drive_report(monkeypatch, cfg, name, source, clones, dest, reachable=True):
    """Monkeypatch the read boundary and run the REAL _report_subvol."""
    from datetime import datetime
    monkeypatch.setattr(ss, "list_snapper_snapshots", lambda mp: list(source))
    monkeypatch.setattr(ss, "list_parent_clones", lambda c, n: sorted(
        clones, key=lambda x: (x.info_time or datetime.min, x.snapper_num or 0),
        reverse=True))

    def _lt(c, rd):
        if not reachable:
            raise ss.RemoteUnreachable("ssh: Could not resolve hostname dest")
        return list(dest)
    monkeypatch.setattr(ss, "list_target_snapshots", _lt)
    ss._report_subvol(cfg, name, "/m", "/recv/" + name)


def test_bucket_attribute_matches_keep_set_both_directions():
    # THE anti-drift guard: the paths _bucket_attribute tiers as kept are EXACTLY the
    # paths _bucket_keep keeps — subset each way, over several policies.
    from datetime import datetime
    tgts = [target_of(clone_snap(n, w, uuid=f"u{n}")) for n, w in enumerate([
        datetime(2026, 6, 10, 12), datetime(2026, 6, 10, 11), datetime(2026, 6, 10, 10),
        datetime(2026, 6, 9, 12),  datetime(2026, 6, 8, 12),  datetime(2026, 6, 1, 12),
        datetime(2025, 12, 1, 12), datetime(2024, 3, 1, 12)])]
    tgts.sort(key=lambda t: t.when, reverse=True)
    for pol in [(2, 3, 1, 1, 1), (0, 0, 0, 0, 2), (24, 14, 8, 6, 2), (0, 0, 0, 0, 0)]:
        attrib = ss._bucket_attribute(tgts, *pol)
        keep = ss._bucket_keep(tgts, *pol)
        assert set(attrib) == keep                       # both directions at once
        assert set(attrib) <= keep and keep <= set(attrib)
        # every recorded code is a real tier, coarsest-last, no dupes within a path
        for codes in attrib.values():
            assert codes and all(c in ss._TIER_ORDER for c in codes)
            assert [c for c in ss._TIER_ORDER if c in codes] == codes


def test_bucket_attribute_undatable_kept_as_undatable():
    u = target_of(clone_snap(1, None, uuid="u1"))        # when is None -> undatable
    assert u.when is None
    attrib = ss._bucket_attribute([u], 5, 5, 5, 5, 5)
    assert attrib[u.path] == [ss._UNDATABLE]
    assert u.path in ss._bucket_keep([u], 5, 5, 5, 5, 5)  # always kept


def test_report_prune_verdict_marks_exactly_paths_outside_keep_set(monkeypatch, capsys):
    # Report's KEEP/PRUNE must equal what apply_retention actually prunes, snapshot
    # for snapshot — the report is a faithful preview of the next run's plan.
    from datetime import datetime
    clones, dest, source = [], [], []
    for n in range(6):
        c = clone_snap(n, datetime(2026, 6, 1 + n, 12), uuid=f"c{n}")
        clones.append(c)
        dest.append(target_of(c))
        source.append(src_snap(n, datetime(2026, 6, 1 + n, 12), uuid=f"src-{n:04d}"))
    cfg2 = ss.Config(server_host="d", parent_keep=2, retention={"default": {
        "keep_hourly": 0, "keep_daily": 2, "keep_weekly": 0,
        "keep_monthly": 0, "keep_yearly": 0}})

    # Ground truth: what apply_retention deletes.
    deleted = []

    def fake_rr(c, cmd, check=True):
        if "subvolume delete" in cmd:
            deleted.append(cmd)
        class R: returncode = 0; stderr = ""
        return R()
    saved = ss.run_remote
    ss.run_remote = fake_rr
    try:
        ss.apply_retention(cfg2, "home", source, dest, clones, "/recv/home")
    finally:
        ss.run_remote = saved
    pruned_nums = {int(cmd.split("/recv/")[1].split("/")[0]) for cmd in deleted}

    _drive_report(monkeypatch, cfg2, "home", source, clones, dest)
    verdicts = _parse_verdicts(capsys.readouterr().out)
    assert {n for n, v in verdicts.items() if v == "PRUNE"} == pruned_nums
    assert {n for n, v in verdicts.items() if v == "KEEP"} == set(range(6)) - pruned_nums


def test_report_pinned_parent_shows_as_reason_not_tier(monkeypatch, capsys):
    from datetime import datetime
    older = clone_snap(1, datetime(2026, 1, 1, 12), uuid="c-old")
    t_old = target_of(older)
    t_new = target_of(clone_snap(9, datetime(2026, 6, 9, 12), uuid="c-new"))
    cfg2 = ss.Config(server_host="d", parent_keep=2, retention={"default": {
        "keep_hourly": 0, "keep_daily": 1, "keep_weekly": 0,
        "keep_monthly": 0, "keep_yearly": 0}})
    _drive_report(monkeypatch, cfg2, "home", [], [older], [t_old, t_new])
    out = capsys.readouterr().out
    pin_line = next(l for l in out.splitlines() if "pinned parent" in l)
    assert "KEEP" in pin_line and "#1 " in pin_line
    # It is kept by the pin ALONE — no GFS tier word may appear on that line.
    assert not any(w in pin_line for w in ("hourly", "daily", "weekly",
                                           "monthly", "yearly"))


def test_report_prepost_partner_shows_as_reason_not_tier(monkeypatch, capsys):
    from datetime import datetime
    d1, d2 = datetime(2026, 6, 1, 12), datetime(2026, 6, 2, 12)
    s_pre = src_snap(5, d1, uuid="u5", typ="pre")
    s_post = src_snap(6, d2, uuid="u6", typ="post", pre=5)
    c5 = clone_snap(5, d1, uuid="u5")
    c6 = clone_snap(6, d2, uuid="u6")
    dest = [target_of(c5), target_of(c6)]                # received_uuid == u5 / u6
    cfg2 = ss.Config(server_host="d", parent_keep=2, retention={"default": {
        "keep_hourly": 0, "keep_daily": 1, "keep_weekly": 0,
        "keep_monthly": 0, "keep_yearly": 0}})
    # subvol 'root' enables the pre/post partner keep.
    _drive_report(monkeypatch, cfg2, "root", [s_pre, s_post], [c6], dest)
    out = capsys.readouterr().out
    partner_line = next(l for l in out.splitlines() if "prepost partner" in l)
    assert "KEEP" in partner_line and "#5 " in partner_line
    assert not any(w in partner_line for w in ("hourly", "daily", "weekly",
                                               "monthly", "yearly"))


def test_report_chain_intact_and_lag_zero(monkeypatch, capsys):
    c7 = clone_snap(7, dt(2026, 6, 2), uuid="clone-7")
    cfg2 = ss.Config(server_host="d")
    _drive_report(monkeypatch, cfg2, "home", [src_snap(7, dt(2026, 6, 2))],
                  [c7], [target_of(c7)])
    out = capsys.readouterr().out
    assert "Chain: intact" in out
    assert "0 snapshot(s) behind" in out


def test_report_chain_warns_full_send_on_divergence(monkeypatch, capsys):
    stale = clone_snap(4, dt(2026, 5, 31), uuid="clone-stale")
    foreign = ss.Subvol(path="/recv/zzz/snapshot", uuid="dst-x", parent_uuid="-",
                        received_uuid="totally-unrelated", readonly=True)
    foreign.snapper_num = 99
    cfg2 = ss.Config(server_host="d")
    _drive_report(monkeypatch, cfg2, "home", [src_snap(8, dt(2026, 6, 3))],
                  [stale], [foreign])
    out = capsys.readouterr().out
    assert "next run will full-send" in out


def test_report_unreachable_prints_source_and_mutates_nothing(monkeypatch, cfg, capsys):
    # Dest down: source section (clones + presumed parent) still prints, dest marked
    # unreachable, and NOTHING is sent/cloned/pruned/retained.
    c7 = clone_snap(7, dt(2026, 6, 2), uuid="clone-7")
    w = FakeWorld(monkeypatch, [src_snap(7, dt(2026, 6, 2))],
                  tree=[c7], dest=[target_of(c7)], reachable=False)
    tree_before = {c.path for c in w.tree}
    dest_before = {t.received_uuid for t in w.dest}

    class _Args:
        subvol = None
    rc = ss._report(cfg, _Args())
    out = capsys.readouterr().out

    assert rc == 0
    assert "unreachable — cannot report dest tiers" in out
    assert "#7 " in out and "correlation unknown" in out   # source clone still shown
    assert "parent (presumed)" in out
    assert w.sends == []                                    # nothing sent
    assert w.retention_calls == []                         # no retention
    assert {c.path for c in w.tree} == tree_before         # parent tree intact
    assert {t.received_uuid for t in w.dest} == dest_before


def test_report_garble_flag(monkeypatch, capsys):
    # A received subvol that fails the rule-1 valid-received check is flagged GARBLE.
    c = clone_snap(3, dt(2026, 6, 3), uuid="c3")
    good = target_of(c)
    garbled = ss.Subvol(path="/recv/4/snapshot", uuid="dst-4", parent_uuid="-",
                        received_uuid="-", readonly=False)   # writable, no recv uuid
    garbled.snapper_num = 4
    garbled.info_time = dt(2026, 6, 4)
    cfg2 = ss.Config(server_host="d")
    _drive_report(monkeypatch, cfg2, "home", [], [c], [good, garbled])
    out = capsys.readouterr().out
    garble_line = next(l for l in out.splitlines() if "#4 " in l and "GARBLE" in l)
    assert "GARBLE" in garble_line


# ============================================================================
# (9) setup-dest / decom-dest — provisioning builders + CLI wiring (no real SSH)
# ============================================================================

def test_provision_script_shape():
    s = ss._provision_script("snappersend", "ssh-ed25519 AAAAKEY comment",
                             "/srv/snapshots-recv")
    # restrict-hardened key line — the security posture, no forced-command wrapper.
    assert "restrict ssh-ed25519 AAAAKEY comment" in s
    assert "ssh-filter" not in s and "snappersend-ssh-filter" not in s
    # scoped sudoers, validated, with destination-detected binary paths.
    assert 'command -v btrfs' in s
    assert "/etc/sudoers.d/snappersend" in s and "visudo -cf" in s
    assert "chmod 0440 /etc/sudoers.d/snappersend" in s
    # dedicated user + receive area; success sentinel.
    assert "useradd --system --create-home --shell /bin/bash" in s
    assert "passwd -l" in s
    assert "/srv/snapshots-recv" in s
    assert "PROVISION_OK" in s


def test_deprovision_script_preserves_data_by_default():
    s = ss._deprovision_script("snappersend", "/srv/snapshots-recv", "laptop",
                               purge_data=False)
    assert "rm -f /etc/sudoers.d/snappersend" in s
    assert "userdel --remove" in s
    assert "DECOM_OK" in s
    # No data destruction without --purge-data.
    assert "subvolume delete" not in s
    assert "/srv/snapshots-recv/laptop" not in s


def test_deprovision_script_purge_deletes_this_hosts_data_only():
    s = ss._deprovision_script("snappersend", "/srv/snapshots-recv", "laptop",
                               purge_data=True)
    assert "subvolume delete" in s
    assert "/srv/snapshots-recv/laptop" in s      # scoped to THIS host's subdir


def test_admin_ssh_argv_uses_invoking_user_when_root(monkeypatch):
    monkeypatch.setattr(ss.os, "geteuid", lambda: 0)
    monkeypatch.setenv("SUDO_USER", "james")
    argv = ss._admin_ssh_argv("admin", "dest", 22)
    assert argv[:4] == ["sudo", "-H", "-u", "james"]
    assert "admin@dest" in argv and "accept-new" in " ".join(argv)


def test_admin_ssh_argv_plain_when_not_root(monkeypatch):
    monkeypatch.setattr(ss.os, "geteuid", lambda: 1000)
    monkeypatch.setenv("SUDO_USER", "james")
    argv = ss._admin_ssh_argv("admin", "dest", 2222)
    assert argv[0] == "ssh" and "sudo" not in argv
    assert "admin@dest" in argv and "2222" in argv


class _CP:
    def __init__(self, rc, out="", err=""):
        self.returncode = rc; self.stdout = out; self.stderr = err


def test_run_admin_script_interactive_allows_sudo_password(monkeypatch):
    # A tty run must NOT use `sudo -n` (which fails when a password is required); it
    # forces a remote pty (-tt) and lets sudo prompt. Script goes base64 in the ARG,
    # keeping stdin free for the password.
    monkeypatch.setattr(ss.sys.stdin, "isatty", lambda: True)
    captured = {}

    def fake_run(argv, *a, **k):
        captured["argv"] = argv
        captured["kwargs"] = k
        return _CP(0)
    monkeypatch.setattr(ss.subprocess, "run", fake_run)
    rc, out, err = ss._run_admin_script("adm", "dest", 22, "echo hi")
    remote = captured["argv"][-1]
    assert "-tt" in captured["argv"]
    assert "sudo -n" not in remote and "| sudo bash" in remote
    assert "base64 -d" in remote                       # script passed as data, not stdin
    assert "capture_output" not in captured["kwargs"]  # terminal inherited
    assert rc == 0


def test_run_admin_script_noninteractive_uses_sudo_n(monkeypatch):
    # No tty (cron/CI): capture output and require passwordless sudo (`sudo -n`).
    monkeypatch.setattr(ss.sys.stdin, "isatty", lambda: False)
    captured = {}

    def fake_run(argv, *a, **k):
        captured["argv"] = argv
        captured["kwargs"] = k
        return _CP(0, "PROVISION_OK\n")
    monkeypatch.setattr(ss.subprocess, "run", fake_run)
    rc, out, err = ss._run_admin_script("adm", "dest", 22, "echo hi")
    remote = captured["argv"][-1]
    assert "-tt" not in captured["argv"]
    assert "sudo -n bash" in remote
    assert captured["kwargs"].get("capture_output") is True
    assert out == "PROVISION_OK\n"


def test_verify_transport_ok_and_failure(monkeypatch, cfg):
    cfg2 = ss.Config(server_host="dest", server_user="snappersend",
                     recv_base="/srv/snapshots-recv")
    monkeypatch.setattr(ss, "run_remote",
                        lambda c, cmd, check=True: _CP(0, "/srv/snapshots-recv\n"))
    ok, msg = ss._verify_transport(cfg2)
    assert ok and "remote sudo OK" in msg
    # ssh rc 255 -> transport failure, not "unexpected".
    monkeypatch.setattr(ss, "run_remote",
                        lambda c, cmd, check=True: _CP(255, "", "ssh: connect: refused"))
    ok, msg = ss._verify_transport(cfg2)
    assert not ok and "transport failure" in msg


def test_resolve_admin_parses_user_at_host_and_bare_user():
    cfg2 = ss.Config(server_host="backup", server_ssh_port=2200)

    class A:
        admin = "adm@other"
    assert ss._resolve_admin(A(), cfg2) == ("adm", "other", 2200)

    class B:
        admin = "adm"                     # bare user -> host from config
    assert ss._resolve_admin(B(), cfg2) == ("adm", "backup", 2200)


def test_ensure_config_writes_when_missing(tmp_path, monkeypatch):
    p = tmp_path / "cfg"
    answers = iter(["backup-host", "22", "snappersend",
                    str(tmp_path / "key"), "/srv/recv", "root:/ home:/home"])
    monkeypatch.setattr("builtins.input", lambda *a: next(answers))
    monkeypatch.setattr(ss.sys.stdin, "isatty", lambda: True)
    assert ss._ensure_config(str(p)) is True
    cfg = ss.Config(**ss.load_config(str(p)))
    assert cfg.server_host == "backup-host"
    assert set(cfg.subvols) == {"root", "home"}
    # An existing config is never clobbered.
    before = p.read_text()
    assert ss._ensure_config(str(p)) is True and p.read_text() == before


def test_cli_subcommands_parse():
    a = ss.build_parser().parse_args(["setup-dest", "james@dest"])
    assert a.command == "setup-dest" and a.admin == "james@dest"
    a = ss.build_parser().parse_args(["decom-dest", "--purge-data"])
    assert a.command == "decom-dest" and a.purge_data is True and a.admin is None
    # Bare invocation and --report still route to the run/report path (no subcommand).
    assert getattr(ss.build_parser().parse_args([]), "command", None) is None
    assert ss.build_parser().parse_args(["--report"]).command is None


def test_run_lock_collision(tmp_path, monkeypatch):
    import fcntl
    lock = tmp_path / "snappersend.lock"
    monkeypatch.setenv("SNAPPERSEND_LOCK", str(lock))
    holder = open(lock, "w")
    fcntl.flock(holder.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    try:
        with pytest.raises(ss._AlreadyRunning):
            with ss.run_lock():
                pass
    finally:
        fcntl.flock(holder.fileno(), fcntl.LOCK_UN)
        holder.close()


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-q"]))
