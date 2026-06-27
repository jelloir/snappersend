#!/usr/bin/env python3
"""
Unit tests for di-snapsend's pure correctness logic (SPEC §7 task 11).

These cover the parts where a bug is silent-but-catastrophic:
  - is_correlated  (all three clauses + the readonly guard + the '-' guard)
  - choose_parent  (newest-correlated-older, full-send fallback)
  - _newest_correlated_pair  (the Rule-3 pin)
  - Subvol.is_valid_received / is_garbled  (Rule 1)
  - parse_subvolume_show  (against real `btrfs subvolume show` output)
  - retention bucketing + pin + pre/post pairing
  - config loading + timestamp parsing

Run:  SNAPSEND_QUIET=1 python3 -m unittest -v test_di_snapsend
The engine file has no .py extension (it installs as /usr/local/bin/di-snapsend),
so it is loaded here by path via importlib.
"""

import importlib.util
import os
import sys
import tempfile
import unittest
from datetime import datetime
from importlib.machinery import SourceFileLoader

os.environ.setdefault("SNAPSEND_QUIET", "1")  # silence the logger during tests

# The engine installs as /usr/local/bin/di-snapsend (no .py extension), so the
# extension-based loader can't infer it — load the source file explicitly. The
# module must be registered in sys.modules before exec so @dataclass can resolve
# its own module namespace.
_HERE = os.path.dirname(os.path.abspath(__file__))
_loader = SourceFileLoader("di_snapsend", os.path.join(_HERE, "di-snapsend"))
_spec = importlib.util.spec_from_loader("di_snapsend", _loader)
di = importlib.util.module_from_spec(_spec)
sys.modules["di_snapsend"] = di
_loader.exec_module(di)


def mksub(path="/x", uuid="-", parent="-", received="-", ro=True,
          num=None, info_time=None, creation=None, typ=None, pre=None):
    return di.Subvol(
        path=path, uuid=uuid, parent_uuid=parent, received_uuid=received,
        readonly=ro, snapper_num=num, info_time=info_time, creation_time=creation,
        snapper_type=typ, pre_num=pre,
    )


# --------------------------------------------------------------------------
# Rule 1 — validity of a received subvolume
# --------------------------------------------------------------------------
class TestValidity(unittest.TestCase):
    def test_clean_receive_is_valid(self):
        sv = mksub(ro=True, received="abc")
        self.assertTrue(sv.is_valid_received)
        self.assertFalse(sv.is_garbled)

    def test_garbled_is_writable_without_received_uuid(self):
        sv = mksub(ro=False, received="-")
        self.assertTrue(sv.is_garbled)
        self.assertFalse(sv.is_valid_received)

    def test_readonly_without_received_uuid_is_neither(self):
        # a local source snapshot: RO but never received
        sv = mksub(ro=True, received="-")
        self.assertFalse(sv.is_valid_received)
        self.assertFalse(sv.is_garbled)

    def test_writable_with_received_uuid_is_neither(self):
        sv = mksub(ro=False, received="abc")
        self.assertFalse(sv.is_valid_received)
        self.assertFalse(sv.is_garbled)


# --------------------------------------------------------------------------
# Rule 2 — correlation
# --------------------------------------------------------------------------
class TestCorrelation(unittest.TestCase):
    def test_target_received_from_source(self):
        # The worked example from SPEC §3: laptop #1 uuid a2159d69…, server copy
        # carries received_uuid = a2159d69… -> correlated via clause 1.
        s = mksub(uuid="a2159d69", received="-")
        t = mksub(uuid="server-uuid", received="a2159d69")
        self.assertTrue(di.is_correlated(s, t))

    def test_source_received_from_target(self):
        s = mksub(uuid="s", received="t-uuid")
        t = mksub(uuid="t-uuid", received="-")
        self.assertTrue(di.is_correlated(s, t))

    def test_common_source(self):
        s = mksub(uuid="s", received="common")
        t = mksub(uuid="t", received="common")
        self.assertTrue(di.is_correlated(s, t))

    def test_common_source_guard_rejects_dash(self):
        # both received_uuid == '-' must NOT count as a common source
        s = mksub(uuid="s", received="-")
        t = mksub(uuid="t", received="-")
        self.assertFalse(di.is_correlated(s, t))

    def test_readonly_guard(self):
        s = mksub(uuid="a", received="-", ro=False)
        t = mksub(uuid="x", received="a", ro=True)
        self.assertFalse(di.is_correlated(s, t))   # s not RO
        s.readonly = True
        t.readonly = False
        self.assertFalse(di.is_correlated(s, t))   # t not RO
        t.readonly = True
        self.assertTrue(di.is_correlated(s, t))

    def test_uncorrelated(self):
        s = mksub(uuid="a", received="-")
        t = mksub(uuid="b", received="c")
        self.assertFalse(di.is_correlated(s, t))

    def test_dash_uuid_never_correlates(self):
        # a parse failure leaves uuid='-'; it must not match a '-' received_uuid
        a = mksub(uuid="-", received="-", ro=True)
        b = mksub(uuid="x", received="-", ro=True)
        self.assertFalse(di.is_correlated(a, b))
        self.assertFalse(di.is_correlated(b, a))


# --------------------------------------------------------------------------
# Parent selection
# --------------------------------------------------------------------------
class TestChooseParent(unittest.TestCase):
    def setUp(self):
        # sources 1..4; targets correlate with 1 and 2 only.
        self.s1 = mksub(path="/s1", uuid="u1", num=1)
        self.s2 = mksub(path="/s2", uuid="u2", num=2)
        self.s3 = mksub(path="/s3", uuid="u3", num=3)
        self.s4 = mksub(path="/s4", uuid="u4", num=4)
        self.sources = [self.s1, self.s2, self.s3, self.s4]
        self.targets = [
            mksub(path="/t1", uuid="ut1", received="u1"),
            mksub(path="/t2", uuid="ut2", received="u2"),
        ]

    def test_picks_newest_correlated_older(self):
        # sending #4: newest correlated older source is #2
        p = di.choose_parent(self.s4, self.sources, self.targets)
        self.assertIs(p, self.s2)

    def test_ignores_newer_sources(self):
        # sending #3: only #1,#2 are older & correlated -> #2
        p = di.choose_parent(self.s3, self.sources, self.targets)
        self.assertIs(p, self.s2)

    def test_full_send_when_nothing_correlated(self):
        p = di.choose_parent(self.s1, self.sources, [])
        self.assertIsNone(p)

    def test_full_send_for_oldest(self):
        # #1 has nothing strictly older
        p = di.choose_parent(self.s1, self.sources, self.targets)
        self.assertIsNone(p)

    def test_skips_uncorrelated_even_if_older(self):
        # only #3 is correlated; sending #4 -> parent #3 (skips #1,#2 uncorrelated)
        targets = [mksub(path="/t3", uuid="ut3", received="u3")]
        p = di.choose_parent(self.s4, self.sources, targets)
        self.assertIs(p, self.s3)


# --------------------------------------------------------------------------
# Rule 3 — the pinned pair
# --------------------------------------------------------------------------
class TestNewestCorrelatedPair(unittest.TestCase):
    def test_picks_highest_numbered_correlated(self):
        s1 = mksub(path="/s1", uuid="u1", num=1)
        s2 = mksub(path="/s2", uuid="u2", num=2)
        s3 = mksub(path="/s3", uuid="u3", num=3)  # newest but NOT on target
        t1 = mksub(path="/t1", uuid="ut1", received="u1")
        t2 = mksub(path="/t2", uuid="ut2", received="u2")
        ps, pt = di._newest_correlated_pair([s1, s2, s3], [t1, t2])
        self.assertIs(ps, s2)
        self.assertIs(pt, t2)

    def test_none_when_no_correlation(self):
        s = mksub(path="/s", uuid="u", num=1)
        t = mksub(path="/t", uuid="x", received="y")
        self.assertEqual(di._newest_correlated_pair([s], [t]), (None, None))


# --------------------------------------------------------------------------
# parse_subvolume_show — against real-shaped output
# --------------------------------------------------------------------------
REAL_RO = """\
/home/.snapshots/1/snapshot
\tName: \t\t\tsnapshot
\tUUID: \t\t\ta2159d69-abcd-934e-a327-68d19fc4cd1b
\tParent UUID: \t\tf877a71f-1aea-f040-8ad0-655e84d27d1c
\tReceived UUID: \t\t-
\tCreation time: \t\t2026-06-27 10:00:00 +0000
\tSubvolume ID: \t\t256
\tGeneration: \t\t12345
\tFlags: \t\t\treadonly
\tSnapshot(s):
"""

GARBLED_RW = """\
/srv/snapshots-recv/home/.incoming-deadbeef/snapshot
\tName: \t\t\tsnapshot
\tUUID: \t\t\t11111111-2222-3333-4444-555555555555
\tParent UUID: \t\t-
\tReceived UUID: \t\t-
\tCreation time: \t\t2026-06-27 10:05:00 +0000
\tFlags: \t\t\t-
"""

RECEIVED_CLEAN = """\
/srv/snapshots-recv/home/1-a2159d69
\tName: \t\t\tsnapshot
\tUUID: \t\t\t99999999-0000-0000-0000-000000000000
\tParent UUID: \t\t-
\tReceived UUID: \t\ta2159d69-abcd-934e-a327-68d19fc4cd1b
\tCreation time: \t\t2026-06-27 10:06:00 +0000
\tFlags: \t\t\treadonly
"""


class TestParse(unittest.TestCase):
    def test_real_readonly_source(self):
        sv = di.parse_subvolume_show(REAL_RO, "/home/.snapshots/1/snapshot")
        self.assertEqual(sv.uuid, "a2159d69-abcd-934e-a327-68d19fc4cd1b")
        self.assertEqual(sv.parent_uuid, "f877a71f-1aea-f040-8ad0-655e84d27d1c")
        self.assertEqual(sv.received_uuid, "-")
        self.assertTrue(sv.readonly)
        self.assertEqual(sv.creation_time, datetime(2026, 6, 27, 10, 0, 0))

    def test_garbled_writable(self):
        sv = di.parse_subvolume_show(GARBLED_RW, "/x")
        self.assertFalse(sv.readonly)
        self.assertEqual(sv.received_uuid, "-")
        self.assertTrue(sv.is_garbled)

    def test_received_clean(self):
        sv = di.parse_subvolume_show(RECEIVED_CLEAN, "/x")
        self.assertTrue(sv.readonly)
        self.assertEqual(sv.received_uuid, "a2159d69-abcd-934e-a327-68d19fc4cd1b")
        self.assertTrue(sv.is_valid_received)
        # the received subvol correlates with its source
        src = di.parse_subvolume_show(REAL_RO, "/home/.snapshots/1/snapshot")
        self.assertTrue(di.is_correlated(src, sv))

    def test_missing_fields_default_gracefully(self):
        sv = di.parse_subvolume_show("nonsense\nno fields here\n", "/x")
        self.assertEqual(sv.uuid, "-")
        self.assertEqual(sv.received_uuid, "-")
        self.assertFalse(sv.readonly)

    def test_parent_uuid_not_confused_with_uuid(self):
        # "Parent UUID:" must not be matched by the "uuid:" branch
        sv = di.parse_subvolume_show(REAL_RO, "/x")
        self.assertNotEqual(sv.uuid, sv.parent_uuid)


# --------------------------------------------------------------------------
# Timestamp parsing
# --------------------------------------------------------------------------
class TestParseDt(unittest.TestCase):
    def test_info_xml_naive_utc(self):
        self.assertEqual(di._parse_dt("2026-06-27 10:00:00"),
                         datetime(2026, 6, 27, 10, 0, 0))

    def test_btrfs_with_zone_normalised_to_utc(self):
        # +0200 -> 08:00 UTC, tz dropped
        self.assertEqual(di._parse_dt("2026-06-27 10:00:00 +0200"),
                         datetime(2026, 6, 27, 8, 0, 0))

    def test_dash_and_empty(self):
        self.assertIsNone(di._parse_dt("-"))
        self.assertIsNone(di._parse_dt(""))
        self.assertIsNone(di._parse_dt(None))


# --------------------------------------------------------------------------
# Retention bucketing
# --------------------------------------------------------------------------
class TestBucketKeep(unittest.TestCase):
    def _daily_targets(self, n):
        # n consecutive days, newest first, one snapshot per day
        out = []
        for i in range(n):
            d = datetime(2026, 6, 27, 12, 0, 0)
            d = d.replace(day=27 - i)
            out.append(mksub(path=f"/d{i}", creation=d))
        return out

    def test_keep_daily_limits(self):
        targets = self._daily_targets(10)
        keep = di._bucket_keep(targets, keep_daily=3, keep_weekly=0, keep_monthly=0)
        # newest 3 distinct days kept
        self.assertEqual(keep, {"/d0", "/d1", "/d2"})

    def test_two_per_day_keeps_newest_of_day(self):
        a = mksub(path="/a", creation=datetime(2026, 6, 27, 9, 0))
        b = mksub(path="/b", creation=datetime(2026, 6, 27, 23, 0))  # newest of day
        c = mksub(path="/c", creation=datetime(2026, 6, 26, 23, 0))
        targets = sorted([a, b, c], key=lambda t: t.when, reverse=True)
        keep = di._bucket_keep(targets, keep_daily=2, keep_weekly=0, keep_monthly=0)
        self.assertIn("/b", keep)   # newest of the 27th
        self.assertIn("/c", keep)   # the 26th
        self.assertNotIn("/a", keep)

    def test_undatable_kept(self):
        t = mksub(path="/u", creation=None)
        keep = di._bucket_keep([t], keep_daily=0, keep_weekly=0, keep_monthly=0)
        self.assertIn("/u", keep)

    def test_zero_policy_keeps_nothing_datable(self):
        targets = self._daily_targets(3)
        keep = di._bucket_keep(targets, 0, 0, 0)
        self.assertEqual(keep, set())


class TestApplyRetention(unittest.TestCase):
    """apply_retention with the remote-mutating calls stubbed out."""

    def setUp(self):
        self._orig_remote = di.run_remote
        self._orig_latest = di._update_latest_symlink
        self.deleted = []

        def fake_remote(cfg, cmd, *, check=True):
            if "subvolume delete" in cmd:
                self.deleted.append(cmd)
            import subprocess as sp
            return sp.CompletedProcess(["ssh"], 0, "", "")

        di.run_remote = fake_remote
        di._update_latest_symlink = lambda *a, **k: None
        self.cfg = di.Config(server_host="h", retention={
            "default": {"keep_daily": 2, "keep_weekly": 0, "keep_monthly": 0},
        })

    def tearDown(self):
        di.run_remote = self._orig_remote
        di._update_latest_symlink = self._orig_latest

    def _targets(self, n):
        out = []
        for i in range(n):
            d = datetime(2026, 6, 27, 12, 0, 0).replace(day=27 - i)
            out.append(mksub(path=f"/recv/{n-i}-uuid{i}", uuid=f"uuid{i}",
                             received=f"src{i}", creation=d, num=(n - i)))
        return out

    def test_prunes_beyond_policy_but_pins_parent(self):
        targets = self._targets(5)  # days 27,26,25,24,23 ; nums 5..1
        # A source whose uuid == the OLDEST target's received_uuid correlates
        # with it (clause 1), so that oldest target becomes the pinned parent.
        oldest = targets[-1]
        src = mksub(path="/s", uuid=oldest.received_uuid, num=1)
        di.apply_retention(self.cfg, "home", [src], targets, "/recv")
        # keep_daily=2 keeps the 2 newest; the pinned oldest must also survive
        deleted_paths = " ".join(self.deleted)
        self.assertNotIn(targets[0].path, deleted_paths)   # newest kept
        self.assertNotIn(targets[1].path, deleted_paths)   # 2nd newest kept
        self.assertNotIn(oldest.path, deleted_paths)        # pinned kept
        self.assertIn(targets[2].path, deleted_paths)       # day 25 pruned
        self.assertIn(targets[3].path, deleted_paths)       # day 24 pruned

    def test_dry_run_deletes_nothing(self):
        cfg = di.Config(server_host="h", dry_run=True, retention={
            "default": {"keep_daily": 1, "keep_weekly": 0, "keep_monthly": 0}})
        di.apply_retention(cfg, "home", [], self._targets(4), "/recv")
        self.assertEqual(self.deleted, [])


# --------------------------------------------------------------------------
# Config loading
# --------------------------------------------------------------------------
SAMPLE_CONFIG = """\
[server]
host = "debian-server"
ssh_port = 2222
user = "snapsend"
use_mbuffer = false

[boot]
enabled = false
paths = ["/boot"]

[subvolumes.home]
mountpoint = "/home"
recv_dir = "/srv/snapshots-recv/home"

[retention.home]
keep_daily = 9
keep_weekly = 3
keep_monthly = 2
"""


class TestLoadConfig(unittest.TestCase):
    def _write(self, text):
        fd, path = tempfile.mkstemp(suffix=".toml")
        with os.fdopen(fd, "w") as f:
            f.write(text)
        self.addCleanup(os.unlink, path)
        return path

    def test_full_parse(self):
        cfg = di.Config(**di.load_config(self._write(SAMPLE_CONFIG)))
        self.assertEqual(cfg.server_host, "debian-server")
        self.assertEqual(cfg.server_ssh_port, 2222)
        self.assertFalse(cfg.use_mbuffer)
        self.assertFalse(cfg.boot_backup_enabled)
        self.assertEqual(cfg.boot_paths, ("/boot",))
        self.assertEqual(cfg.subvols["home"]["recv_dir"], "/srv/snapshots-recv/home")
        self.assertEqual(cfg.retention_for("home")["keep_daily"], 9)

    def test_retention_default_injected_when_absent(self):
        # a config with retention tables but no [retention.default]
        cfg = di.Config(**di.load_config(self._write(SAMPLE_CONFIG)))
        # retention_for an unknown subvol must fall back without KeyError
        self.assertIn("keep_daily", cfg.retention_for("unknown"))

    def test_missing_host_raises(self):
        with self.assertRaises(KeyError):
            di.load_config(self._write("[server]\nssh_port = 22\n"))

    def test_missing_file_raises(self):
        with self.assertRaises(FileNotFoundError):
            di.load_config("/no/such/config/file")


# --------------------------------------------------------------------------
# _run_pipe — the crux function: must return True iff EVERY stage exits 0,
# even when a later stage masks an earlier failure (PIPESTATUS-equivalent).
# Exercised with ordinary shell tools standing in for btrfs send / ssh receive.
# --------------------------------------------------------------------------
class TestRunPipe(unittest.TestCase):
    def test_all_stages_ok(self):
        self.assertTrue(di._run_pipe(["printf", "hello"], ["cat"], False))

    def test_first_stage_fails_is_caught(self):
        # send exits 3; the downstream `cat` still sees EOF and exits 0 — the
        # pipe must still be reported as failed (this is the whole point).
        self.assertFalse(di._run_pipe(["bash", "-c", "exit 3"], ["cat"], False))

    def test_last_stage_fails(self):
        self.assertFalse(
            di._run_pipe(["printf", "x"], ["bash", "-c", "cat >/dev/null; exit 5"], False))

    def test_missing_executable(self):
        self.assertFalse(
            di._run_pipe(["this-cmd-does-not-exist-zzz"], ["cat"], False))


# --------------------------------------------------------------------------
# send_receive — receive-in-place -> Rule-1 verify -> .latest, and the
# garble / pipe-failure cleanup paths. Remote + pipe calls are stubbed.
#
# Post-Issue-1: the subvol is received directly into its FINAL per-transfer dir
# `<recv_dir>/<num>-<short_uuid>/` (landing as `.../snapshot`) and NEVER moved —
# so no `mv` of a subvolume is ever generated (the bug that broke the live run).
# --------------------------------------------------------------------------
class TestSendReceive(unittest.TestCase):
    def setUp(self):
        import subprocess as sp
        self._orig = {k: getattr(di, k) for k in
                      ("run_remote", "_run_pipe", "show_remote", "_update_latest_symlink")}
        self.remote_cmds = []
        self.latest_updated = []
        self.pipe_ssh = []          # ssh argv captured from the stubbed _run_pipe

        def fake_remote(cfg, cmd, *, check=True):
            self.remote_cmds.append(cmd)
            return sp.CompletedProcess(["ssh"], 0, "", "")

        di.run_remote = fake_remote
        di._update_latest_symlink = lambda cfg, rd, name: self.latest_updated.append(name)
        self.cfg = di.Config(server_host="h", use_mbuffer=False)
        self.snap = mksub(path="/home/.snapshots/7/snapshot",
                          uuid="a2159d69-aaaa-bbbb-cccc-dddddddddddd", num=7)

    def tearDown(self):
        for k, v in self._orig.items():
            setattr(di, k, v)

    def _capture_pipe(self, ok):
        def _pipe(send_argv, ssh_argv, use_mbuffer):
            self.pipe_ssh.append(ssh_argv)
            return ok
        di._run_pipe = _pipe

    def _count(self, needle):
        return sum(1 for c in self.remote_cmds if needle in c)

    def _no_mv_anywhere(self):
        """No `mv` of a subvolume must appear in ANY issued command — neither the
        remote run_remote calls nor the ssh receive argv passed through the pipe."""
        self.assertEqual(self._count("mv "), 0)
        for argv in self.pipe_ssh:
            self.assertFalse(any(tok == "mv" or tok.startswith("mv ") for tok in argv),
                             f"unexpected mv in pipe ssh argv: {argv}")

    def test_happy_path_receives_in_place_and_updates_latest(self):
        self._capture_pipe(True)
        di.show_remote = lambda cfg, p: mksub(path=p, uuid="srv", received=self.snap.uuid)
        ok = di.send_receive(self.cfg, self.snap, "/srv/snapshots-recv/home", None)
        self.assertTrue(ok)
        self._no_mv_anywhere()
        self.assertEqual(self._count("subvolume delete"), 1)  # only the pre-clean
        self.assertEqual(self.latest_updated, ["7-a2159d69"]) # <num>-<short_uuid>

    def test_receive_target_is_the_final_dir(self):
        # The btrfs receive (in the ssh argv handed to the pipe) targets the FINAL
        # per-transfer directory, not a staging area — the subvol lands there as
        # .../snapshot and is never moved.
        self._capture_pipe(True)
        di.show_remote = lambda cfg, p: mksub(path=p, uuid="srv", received=self.snap.uuid)
        di.send_receive(self.cfg, self.snap, "/srv/snapshots-recv/home", None)
        self.assertEqual(len(self.pipe_ssh), 1)
        joined = " ".join(self.pipe_ssh[0])
        self.assertIn("sudo btrfs receive", joined)
        self.assertIn("/srv/snapshots-recv/home/7-a2159d69", joined)
        # and the mkdir prepares exactly that final dir
        self.assertTrue(any("mkdir -p" in c and "/srv/snapshots-recv/home/7-a2159d69" in c
                            and c.endswith("7-a2159d69") for c in self.remote_cmds))

    def test_garble_is_cleaned_up_and_fails(self):
        self._capture_pipe(True)
        # writable + no received_uuid == garbled (Rule 1)
        di.show_remote = lambda cfg, p: mksub(path=p, uuid="srv", received="-", ro=False)
        ok = di.send_receive(self.cfg, self.snap, "/srv/snapshots-recv/home", None)
        self.assertFalse(ok)
        self._no_mv_anywhere()
        self.assertEqual(self._count("subvolume delete"), 2)  # pre-clean + garble cleanup
        self.assertEqual(self.latest_updated, [])

    def test_pipe_failure_discards_even_if_subvol_looks_valid(self):
        self._capture_pipe(False)                             # a stage reported error
        di.show_remote = lambda cfg, p: mksub(path=p, uuid="srv", received=self.snap.uuid)
        ok = di.send_receive(self.cfg, self.snap, "/srv/snapshots-recv/home", None)
        self.assertFalse(ok)
        self._no_mv_anywhere()
        self.assertEqual(self._count("subvolume delete"), 2)  # pre-clean + discard

    def test_dry_run_does_nothing_remote(self):
        cfg = di.Config(server_host="h", dry_run=True, use_mbuffer=False)
        di._run_pipe = lambda *a, **k: (_ for _ in ()).throw(AssertionError("pipe ran in dry-run"))
        di.show_remote = lambda *a, **k: (_ for _ in ()).throw(AssertionError("show ran"))
        ok = di.send_receive(cfg, self.snap, "/srv/snapshots-recv/home", None)
        self.assertTrue(ok)
        self.assertEqual(self.remote_cmds, [])             # no remote mutations


# --------------------------------------------------------------------------
# Target enumeration + .latest under the receive-in-place layout (Issue 1):
# each received snapshot is a subvol at `<recv_dir>/<num>-<short_uuid>/snapshot`,
# and `.latest` must resolve to that subvol, not the wrapper directory.
# --------------------------------------------------------------------------
class TestTargetLayout(unittest.TestCase):
    def setUp(self):
        import subprocess as sp
        self._orig = {k: getattr(di, k) for k in ("run_remote", "show_remote")}
        self.cfg = di.Config(server_host="h")
        self.sp = sp

    def tearDown(self):
        for k, v in self._orig.items():
            setattr(di, k, v)

    def test_enumeration_reads_name_snapshot_and_parses_num(self):
        # `ls -1 recv_dir` lists wrapper dirs + the .latest symlink.
        recv = "/srv/snapshots-recv/root"
        di.run_remote = lambda cfg, cmd, *, check=True: self.sp.CompletedProcess(
            ["ssh"], 0, "3-aabbccdd\n7-11223344\nroot.latest\n", "")
        asked = []

        def fake_show(cfg, path):
            asked.append(path)
            return mksub(path=path, uuid="x", received="src", ro=True)

        di.show_remote = fake_show
        out = di.list_target_snapshots(self.cfg, recv)

        # The subvol is read one level DOWN, at <name>/snapshot ...
        self.assertIn(f"{recv}/3-aabbccdd/snapshot", asked)
        self.assertIn(f"{recv}/7-11223344/snapshot", asked)
        # ... the .latest symlink is skipped ...
        self.assertFalse(any("latest" in p for p in asked))
        # ... and the snapper_num is recovered from the DIRECTORY name.
        self.assertEqual(sorted(s.snapper_num for s in out), [3, 7])

    def test_latest_symlink_targets_the_snapshot_subvol(self):
        recv = "/srv/snapshots-recv/root"
        issued = []
        di.run_remote = lambda cfg, cmd, *, check=True: (
            issued.append(cmd) or self.sp.CompletedProcess(["ssh"], 0, "", ""))
        di._update_latest_symlink(self.cfg, recv, "7-11223344")
        self.assertEqual(len(issued), 1)
        cmd = issued[0]
        self.assertIn("ln -sfn", cmd)
        # link target is the SUBVOL (…/snapshot), not the wrapper dir
        self.assertIn(f"{recv}/7-11223344/snapshot", cmd)
        self.assertIn(f"{recv}/root.latest", cmd)


# --------------------------------------------------------------------------
# Pre/post pairing (root nicety)
# --------------------------------------------------------------------------
class TestPrePost(unittest.TestCase):
    def test_keeping_post_keeps_its_pre(self):
        # source: #3 = pre, #4 = post(pre_num=3)
        s_pre = mksub(path="/s3", uuid="u3", num=3, typ="pre")
        s_post = mksub(path="/s4", uuid="u4", num=4, typ="post", pre=3)
        t_pre = mksub(path="/recv/3-u3", uuid="t3", received="u3", num=3)
        t_post = mksub(path="/recv/4-u4", uuid="t4", received="u4", num=4)
        # keep only the post; expect the pre's target to be added
        extra = di._keep_prepost_partners({t_post.path}, [s_pre, s_post], [t_pre, t_post])
        self.assertIn(t_pre.path, extra)


# ==========================================================================
# MANUAL INTEGRATION CHECKS (cannot be unit-tested — need a real two-host
# btrfs send/receive; run on the VM pair after any transfer-path change).
# Verified 2026-06-27 on millionaire-test -> debian-server-test (Trixie, 6.12,
# real btrfs). These back-stop the stubbed send_receive tests above.
#
# (A) Incremental chain (Issue 2) — on a run that starts with an EMPTY server:
#     sudo di-snapsend --subvol root --skip-boot
#     expect the log to show:
#         sending #1 (FULL)
#         sending #2 (incremental, parent #1)
#         sending #3 (incremental, parent #2)
#     then on the SERVER confirm the received_uuid + parent_uuid chain:
#         sudo btrfs subvolume show <recv>/root/1-*/snapshot   # received_uuid = src#1 uuid; parent_uuid -
#         sudo btrfs subvolume show <recv>/root/2-*/snapshot   # received_uuid = src#2 uuid; parent_uuid = #1's UUID
#         sudo btrfs subvolume show <recv>/root/3-*/snapshot   # received_uuid = src#3 uuid; parent_uuid = #2's UUID
#     (If #2/#3 show FULL after #1 has landed, the post-send target_snaps refresh
#     in replicate_subvol or is_correlated is broken — investigate there.)
#     NOTE: when all source snapshots share one calendar day, GFS target
#     retention collapses them to the newest (pinned) one — same-day snapshots
#     will not all persist on the server. Inspect the chain in a scratch dir, or
#     across snapshots created on different days, to see all links coexist.
#
# (B) Garble detection (Issue 3) — interrupt a real receive mid-stream:
#     btrfs send /.snapshots/1/snapshot | ssh ... 'sudo btrfs receive <dir>' &
#     sleep 6; pkill -9 -f 'btrfs send /.snapshots/1/snapshot'
#     then: sudo btrfs subvolume show <dir>/snapshot
#     CONFIRMED signature on Trixie's btrfs-progs:  Flags: -   (readonly FALSE)
#     and  Received UUID: -  -> exactly Subvol.is_garbled. Re-running di-snapsend
#     then pre-cleans (subvolume delete + rmdir) the partial and re-sends cleanly.
#     If a future btrfs-progs leaves a DIFFERENT signature (e.g. no subvol, or
#     readonly set), adjust Subvol.is_garbled / is_valid_received to match.
# ==========================================================================


if __name__ == "__main__":
    unittest.main()
