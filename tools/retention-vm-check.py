#!/usr/bin/env python3
"""
retention-vm-check.py — TEST-ONLY. Drive di-snapsend retention EXECUTION for real
against a fabricated history on the server, and verify the on-disk survivors match
the engine's own GFS decision (decision computed by di._bucket_keep, execution by
di.apply_retention — real `btrfs subvolume delete` + wrapper `rmdir` + `.latest`
over SSH).

Run ON THE LAPTOP (it has /etc/snapsend/config + the transport key). Pair with
tools/fabricate-history.sh on the server (default fake host segment fabricate-test).

  sudo SNAPSEND_ENGINE=/usr/local/bin/di-snapsend \\
       python3 tools/retention-vm-check.py home --keep-daily 14 --keep-weekly 8 --keep-monthly 2

With no source snapshots passed, retention reduces to the pure GFS + execution path
(no pin / no Option-B correlation) — exactly the execution mechanics we want to
prove on real subvolumes. The pin / Option-B / correlation DECISIONS are covered
exhaustively by the pure-logic unit suite, and their execution by the normal
end-to-end di-snapsend runs.
"""
import argparse
import importlib.util
import os
import sys
from datetime import datetime
from importlib.machinery import SourceFileLoader

ENGINE = os.environ.get("SNAPSEND_ENGINE", "/usr/local/bin/di-snapsend")
_loader = SourceFileLoader("di_snapsend", ENGINE)
_spec = importlib.util.spec_from_loader("di_snapsend", _loader)
di = importlib.util.module_from_spec(_spec)
sys.modules["di_snapsend"] = di
_loader.exec_module(di)


def _wrappers(targets):
    return {os.path.basename(os.path.dirname(t.path)) for t in targets}


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("subvol", help="subvol name (e.g. home) under the fabricate host")
    ap.add_argument("--host", default="fabricate-test", help="host segment (default fabricate-test)")
    ap.add_argument("--config", default=os.environ.get("SNAPSEND_CONFIG", "/etc/snapsend/config"))
    ap.add_argument("--keep-hourly", type=int, default=0,
                    help="finest GFS tier (default 0 = disabled, the pre-tier behaviour)")
    ap.add_argument("--keep-daily", type=int, default=14)
    ap.add_argument("--keep-weekly", type=int, default=8)
    ap.add_argument("--keep-monthly", type=int, default=6)
    ap.add_argument("--expect-noop", action="store_true",
                    help="assert retention deletes nothing (idempotent re-run check)")
    a = ap.parse_args()

    kw = di.load_config(a.config)
    kw["retention"] = {"default": {"keep_hourly": a.keep_hourly,
                                   "keep_daily": a.keep_daily,
                                   "keep_weekly": a.keep_weekly,
                                   "keep_monthly": a.keep_monthly}}
    cfg = di.Config(**kw)
    recv = os.path.join(cfg.recv_base, a.host, a.subvol)

    before = di.list_target_snapshots(cfg, recv)
    if not before:
        print(f"FAIL: no targets found under {recv} (fabricate first)")
        return 2
    nf = sorted(before, key=lambda t: (t.when or datetime.min), reverse=True)
    decision = di._bucket_keep(nf, a.keep_hourly, a.keep_daily, a.keep_weekly,
                               a.keep_monthly, cfg.retention_timezone)
    expected = {os.path.basename(os.path.dirname(t.path)) for t in before if t.path in decision}
    print(f"enumerated {len(before)} targets; GFS decision (tz={cfg.retention_timezone}, "
          f"h={a.keep_hourly}/d={a.keep_daily}/w={a.keep_weekly}/m={a.keep_monthly}) "
          f"keeps {len(expected)}")

    # EXECUTE for real (no sources -> pure GFS + execution; real ssh deletes/rmdir).
    di.apply_retention(cfg, a.subvol, [], before, recv)

    after = di.list_target_snapshots(cfg, recv)
    survivors = _wrappers(after)
    deleted = _wrappers(before) - survivors
    print(f"on-disk survivors: {len(survivors)}   deleted: {len(deleted)}")

    ok = True
    if survivors != expected:
        ok = False
        print("  MISMATCH between on-disk survivors and GFS decision:")
        print("   only-on-disk :", sorted(survivors - expected)[:8])
        print("   only-expected:", sorted(expected - survivors)[:8])
    if a.expect_noop and deleted:
        ok = False
        print(f"  EXPECTED NO-OP but deleted {len(deleted)}: {sorted(deleted)[:8]}")

    # .latest must resolve to a real …/snapshot among the survivors. NOTE: the
    # forced-command ssh filter does not permit `readlink`, so read the symlink
    # target via `ls -l` (which it does allow) and parse the "-> target" tail.
    base = os.path.basename(recv.rstrip("/"))
    link = os.path.join(recv, f"{base}.latest")
    cp = di.run_remote(cfg, f"sudo ls -l {link}", check=False)
    latest = cp.stdout.split(" -> ", 1)[1].strip() if " -> " in cp.stdout else ""
    print(f"  .latest -> {latest or '(none)'}")
    if survivors:
        if not latest.endswith("/snapshot"):
            ok = False
            print("  .latest does not resolve to a /snapshot subvol")
        elif os.path.basename(os.path.dirname(latest)) not in survivors:
            ok = False
            print("  .latest points outside the surviving set")

    print("RESULT:", "PASS" if ok else "FAIL")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
