"""
test_snapperrestore — pytest suite for snapperrestore (and the cross-file
contracts it shares with snappersend).

Run:  SNAPPERRESTORE_QUIET=1 python3 -m pytest test_snapperrestore.py -q

Covers the restore's load-bearing constraints: the fstab/crypttab ANTI-DRIFT
guarantee (UUIDs rewritten, options/columns byte-identical), receive-vs-empty
DERIVED classification plus the explicit @snapshots/@swap always-empty rule,
`--plan` running ZERO local commands, a transport failure aborting cleanly,
epoch-matched snapshot selection, and the chroot-time `initramfs` crypttab
option injection. Two tests deliberately import BOTH scripts: the manifest
round-trip (snappersend WRITES the manifest, snapperrestore PARSES it) and the
provision-script pin (the sudoers grant defines the transport security
boundary and must be byte-identical in both files). The full
partition→boot-verify path is validated on real VMs (see the build report),
not mocked here.
"""

import importlib.machinery
import importlib.util
import os
import sys

import pytest

os.environ.setdefault("SNAPPERRESTORE_QUIET", "1")
os.environ.setdefault("SNAPPERSEND_QUIET", "1")


# --- load the extensionless scripts as modules --------------------------------
def _load(modname):
    if modname in sys.modules:
        return sys.modules[modname]
    loader = importlib.machinery.SourceFileLoader(
        modname, os.path.join(os.path.dirname(__file__), modname))
    spec = importlib.util.spec_from_loader(modname, loader)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[modname] = mod   # register before exec so @dataclass resolves
    loader.exec_module(mod)
    return mod


sr = _load("snapperrestore")
ss = _load("snappersend")      # for the cross-file contracts only


class _CP:
    def __init__(self, rc, out="", err=""):
        self.returncode = rc; self.stdout = out; self.stderr = err


# ============================================================================
# Templates (mirroring the validation VM's real fstab/crypttab/manifest)
# ============================================================================

_FSTAB_TEMPLATE = """\
UUID=f66fb040-cc4e-4af8-b5c0-cebc76152195  /                    btrfs  defaults,noatime,compress=zstd:3,subvol=@                    0 0
UUID=f66fb040-cc4e-4af8-b5c0-cebc76152195  /home                btrfs  defaults,noatime,compress=zstd:3,subvol=@home                0 0
UUID=f66fb040-cc4e-4af8-b5c0-cebc76152195  /var/cache           btrfs  defaults,noatime,compress=zstd:3,subvol=@var_cache           0 0
# nodatacow is set via chattr +C on these subvolume directories instead.
UUID=f66fb040-cc4e-4af8-b5c0-cebc76152195  /var/lib/docker      btrfs  defaults,noatime,subvol=@var_lib_docker                      0 0
UUID=f66fb040-cc4e-4af8-b5c0-cebc76152195  /swap                btrfs  defaults,noatime,subvol=@swap                                0 0
/swap/swapfile                             none                 swap   defaults                                                     0 0
UUID=791f491f-3bba-4ab9-90f4-0b13212c7fa2  /boot                ext4   defaults                                                     0 2
UUID=95AE-CA9B                             /boot/efi            vfat   umask=0077                                                   0 1
tmpfs                                      /tmp                 tmpfs  defaults,noatime,mode=1777                                   0 0
UUID=f66fb040-cc4e-4af8-b5c0-cebc76152195  /.snapshots  btrfs  rw,noatime,compress=zstd:3,subvol=@snapshots  0  0
UUID=f66fb040-cc4e-4af8-b5c0-cebc76152195  /.bootmirror  btrfs  rw,noatime,compress=zstd:3,subvol=@bootmirror  0  0
"""

_CRYPTTAB_TEMPLATE = ("luks-2a13 UUID=f1929157-bf52-47d7-b4b9-e6153aafd6e6 "
                      "none luks,discard,x-initrd.attach\n")

_MANIFEST_TEMPLATE = """\
# comment
version 1
parenttree /.snappersend
btrfslabel luks-btrfs
subvol root /
subvol home /home
subvol bootmirror /.bootmirror
nested home .snapshots nocow=0
nested home james/.cache nocow=1
toplevel @ / nocow=0
toplevel @swap /swap nocow=1
toplevel @var_lib_docker /var/lib/docker nocow=1
"""

_LABEL = "20260703-140008+1000-144-b6f513a0"


def _restore_args(**kw):
    import argparse
    ns = argparse.Namespace(esp_size="512MiB", boot_size="2GiB", swap_size="2g",
                            dry_run=True, disk=None, source_host=None, snapshot=None,
                            server=None, port=None, user=None, ssh_key=None,
                            login=None, password=False,
                            recv_base=None, no_mbuffer=False, config="/nonexistent")
    for k, v in kw.items():
        setattr(ns, k, v)
    return ns


def _build_plan(chosen, manifest_text=_MANIFEST_TEMPLATE, fstab=_FSTAB_TEMPLATE,
                crypttab=_CRYPTTAB_TEMPLATE):
    return sr._restore_build_plan(_restore_args(), "host1", chosen, "root",
                                  fstab, crypttab, manifest_text)


# --- constraint 1: fstab/crypttab are templates; UUIDs rewritten, rest verbatim

def test_uuid_rewrite_only_touches_uuids():
    uuid_map = {
        "f66fb040-cc4e-4af8-b5c0-cebc76152195": "11111111-2222-3333-4444-555555555555",
        "791f491f-3bba-4ab9-90f4-0b13212c7fa2": "66666666-7777-8888-9999-000000000000",
        "95AE-CA9B": "AAAA-BBBB",
    }
    out = sr.rewrite_uuids(_FSTAB_TEMPLATE, uuid_map)
    for old in uuid_map:
        assert old not in out                       # only new UUIDs remain
    for new in uuid_map.values():
        assert new in out
    # Options and column layout unchanged: strip the UUIDs back out of both
    # sides and the texts must be identical byte-for-byte.
    restore_back = out
    for old, new in uuid_map.items():
        restore_back = restore_back.replace(new, old)
    assert restore_back == _FSTAB_TEMPLATE
    # crypttab: same property
    ct = sr.rewrite_uuids(_CRYPTTAB_TEMPLATE, {
        "f1929157-bf52-47d7-b4b9-e6153aafd6e6": "abcdabcd-0000-1111-2222-333333333333"})
    assert "luks-2a13" in ct and "luks,discard,x-initrd.attach" in ct
    assert "f1929157" not in ct


def test_fstab_template_parsing():
    entries = sr.parse_fstab_template(_FSTAB_TEMPLATE)
    root = next(e for e in entries if e.mountpoint == "/")
    assert root.subvol == "@" and root.fstype == "btrfs"
    assert root.uuid == "f66fb040-cc4e-4af8-b5c0-cebc76152195"
    snap = next(e for e in entries if e.mountpoint == "/.snapshots")
    assert snap.subvol == "@snapshots"
    # comments and tmpfs lines survive parsing without becoming subvols
    assert all(e.mountpoint != "#" for e in entries)
    name, uuid, keyspec, options, _ = sr.parse_crypttab_template(_CRYPTTAB_TEMPLATE)
    assert name == "luks-2a13"
    assert uuid == "f1929157-bf52-47d7-b4b9-e6153aafd6e6"
    assert keyspec == "none" and "x-initrd.attach" in options


# --- constraint 2: receive-vs-empty is DERIVED (fstab ∖ server tree), with the
#     explicit always-empty rule for the snapshot store + swap container

def test_classification_is_derived_from_server_tree():
    chosen = {"root": _LABEL, "home": _LABEL, "bootmirror": _LABEL}
    plan = _build_plan(chosen)
    assert plan.receive == {"@": "root", "@home": "home",
                            "@bootmirror": "bootmirror"}
    # Everything else in fstab is recreated empty — derived, not hardcoded.
    assert set(plan.create_empty) == {"@var_cache", "@var_lib_docker", "@swap",
                                      "@snapshots"}
    # A subvol later added to SUBVOLUMES self-adjusts: server holding var_cache
    # moves it to the receive set with no code change.
    plan2 = _build_plan({**chosen, "var_cache": _LABEL})
    assert plan2.receive["@var_cache"] == "var_cache"
    assert "@var_cache" not in plan2.create_empty


def test_snapshots_and_swap_always_recreated_empty():
    # Even when the server holds them, the snapshot store and swap container are
    # recreated empty (old history is parented to the dead fs; swapfile is new).
    chosen = {"root": _LABEL, "home": _LABEL, "bootmirror": _LABEL,
              "snapshots": _LABEL, "swap": _LABEL}
    plan = _build_plan(chosen)
    assert "@snapshots" not in plan.receive
    assert "@swap" not in plan.receive
    assert "@snapshots" in plan.create_empty and "@swap" in plan.create_empty
    assert "@snapshots" in plan.forced_empty and "@swap" in plan.forced_empty
    # swapfile facts derived from the template
    assert plan.swapfile == "/swap/swapfile"
    assert plan.swap_subvol == "@swap" and plan.swap_mountpoint == "/swap"


def test_plan_boot_tier_and_luks_derivation():
    plan = _build_plan({"root": _LABEL, "bootmirror": _LABEL})
    assert plan.esp_entry.mountpoint == "/boot/efi"
    assert plan.boot_entry.mountpoint == "/boot"
    assert plan.bootmirror_subvol == "@bootmirror"
    assert [e.mountpoint for e in plan.boot_mounts] == ["/boot", "/boot/efi"]
    assert plan.crypt_name == "luks-2a13"
    assert plan.crypt_options == "luks,discard,x-initrd.attach"
    assert plan.old_luks_uuid == "f1929157-bf52-47d7-b4b9-e6153aafd6e6"
    assert plan.btrfs_label == "luks-btrfs"        # from the manifest
    assert plan.epoch == "20260703-140008+1000"


def test_plan_without_manifest_warns_and_continues():
    # Old backups predating the manifest: nested subvols aren't recreated, the
    # convention fallback maps server names, and the plan still builds.
    plan = _build_plan({"root": _LABEL, "home": _LABEL}, manifest_text=None)
    assert plan.manifest is None
    assert plan.nested == []
    assert plan.receive == {"@": "root", "@home": "home"}


# --- constraint 4: manifest round-trip ACROSS THE TWO SCRIPTS — snappersend
#     builds the manifest from a synthetic subvol listing, snapperrestore
#     parses it, and the recreation plan matches paths + No-COW flags.

def _fake_manifest_world(monkeypatch):
    def fake_run_local(argv, check=True):
        if argv[:3] == ["btrfs", "subvolume", "show"]:
            top = {"/": "@", "/home": "@home", "/.bootmirror": "@bootmirror"}
            return _CP(0, top.get(argv[3], "@x") + "\n")
        if argv[:4] == ["btrfs", "subvolume", "list", "-o"]:
            if argv[4] == "/":
                return _CP(0,
                    "ID 604 gen 1 top level 256 path @/.snappersend/root/x/snapshot\n")
            if argv[4] == "/home":
                return _CP(0,
                    "ID 265 gen 1 top level 257 path @home/.snapshots\n"
                    "ID 300 gen 1 top level 257 path @home/james/.cache\n")
            return _CP(0, "")
        if argv[:2] == ["lsattr", "-d"]:
            nocow = argv[2] in ("/home/james/.cache", "/swap", "/var/lib/docker")
            return _CP(0, ("---------------C------ " if nocow
                           else "---------------------- ") + argv[2] + "\n")
        if argv[:3] == ["btrfs", "filesystem", "label"]:
            return _CP(0, "luks-btrfs\n")
        return _CP(0, "")
    monkeypatch.setattr(ss, "run_local", fake_run_local)


def test_manifest_round_trip(monkeypatch):
    _fake_manifest_world(monkeypatch)
    cfg2 = ss.Config(server_host="d", subvols={
        "root": {"mountpoint": "/"}, "home": {"mountpoint": "/home"},
        "bootmirror": {"mountpoint": "/.bootmirror"}})
    text = ss.build_restore_manifest(cfg2, fstab_text=_FSTAB_TEMPLATE)
    mf = sr.parse_restore_manifest(text)
    assert mf.subvol_map == {"root": "/", "home": "/home",
                             "bootmirror": "/.bootmirror"}
    # nested: recorded with No-COW flags; the parent tree is EXCLUDED.
    assert ("home", ".snapshots", False) in mf.nested
    assert ("home", "james/.cache", True) in mf.nested
    assert not any(".snappersend" in rel for _n, rel, _c in mf.nested)
    # top-level No-COW map from the live fstab
    assert mf.toplevel_nocow["@swap"] is True
    assert mf.toplevel_nocow["@var_lib_docker"] is True
    assert mf.toplevel_nocow["@"] is False
    assert mf.parent_tree_base == "/.snappersend"
    assert mf.btrfs_label == "luks-btrfs"
    # header states the by-design contract
    assert "NOT in the backup by design" in text

    # ...and the recreation plan derived from it matches paths + flags.
    plan = sr._restore_build_plan(
        _restore_args(), "host1", {"root": _LABEL, "home": _LABEL}, "root",
        _FSTAB_TEMPLATE, _CRYPTTAB_TEMPLATE, text)
    assert ("@home", ".snapshots", False) in plan.nested
    assert ("@home", "james/.cache", True) in plan.nested


# --- constraint 5: --plan performs zero writes ------------------------------

def _canned_run_remote(recv_base="/srv/snapshots-recv", host="host1",
                       subvols=("root", "home", "bootmirror"),
                       manifest=_MANIFEST_TEMPLATE):
    def fake(cfg, cmd, check=True):
        if f"ls -1d {recv_base}" in cmd:
            return _CP(0, recv_base + "\n")
        if cmd.endswith(f"ls -1 {recv_base}"):
            return _CP(0, host + "\n")
        if cmd.endswith(f"ls -1 {recv_base}/{host}"):
            return _CP(0, "\n".join(subvols) + "\n")
        for sv in subvols:
            if cmd.endswith(f"ls -1 {recv_base}/{host}/{sv}"):
                return _CP(0, f"{_LABEL}\n{sv}.latest\n")
        if cmd.endswith("etc/fstab"):
            return _CP(0, _FSTAB_TEMPLATE)
        if cmd.endswith("etc/crypttab"):
            return _CP(0, _CRYPTTAB_TEMPLATE)
        if cmd.endswith("restore-manifest"):
            return _CP(0, manifest) if manifest else _CP(1, "", "No such file")
        return _CP(1, "", f"unexpected remote command: {cmd}")
    return fake


def test_restore_plan_is_read_only(monkeypatch, tmp_path, capsys):
    key = tmp_path / "key"
    key.write_text("fake")
    monkeypatch.setattr(sr.os, "geteuid", lambda: 0)
    monkeypatch.setattr(sr, "run_remote", _canned_run_remote())
    # THE assertion: a --plan run may not execute a single local command —
    # no sgdisk, no mkfs, no cryptsetup, no mount, not even a read probe.
    monkeypatch.setattr(sr, "run_local",
                        lambda *a, **k: pytest.fail(f"--plan ran a local command: {a}"))
    rc = sr.cmd_restore(_restore_args(server="dest", ssh_key=str(key)))
    out = capsys.readouterr().out
    assert rc == 0
    assert "restore plan" in out
    assert "<- receive root/" in out
    assert "recreate EMPTY" in out
    assert "@home/.snapshots" in out               # nested subvols shown


def test_restore_plan_shows_no_manifest_warning(monkeypatch, tmp_path, capsys):
    key = tmp_path / "key"
    key.write_text("fake")
    warns = []
    monkeypatch.setattr(sr, "log_warn", lambda m: warns.append(m))
    monkeypatch.setattr(sr.os, "geteuid", lambda: 0)
    monkeypatch.setattr(sr, "run_remote", _canned_run_remote(manifest=None))
    monkeypatch.setattr(sr, "run_local",
                        lambda *a, **k: pytest.fail("--plan ran a local command"))
    rc = sr.cmd_restore(_restore_args(server="dest", ssh_key=str(key)))
    assert rc == 0
    assert any("no restore-manifest" in w for w in warns)


# --- constraint 3: transport-guard reuse — a blip aborts cleanly -------------

def test_restore_unreachable_destination_aborts_cleanly(monkeypatch, tmp_path):
    key = tmp_path / "key"
    key.write_text("fake")
    monkeypatch.setattr(sr.os, "geteuid", lambda: 0)
    calls = []

    def down(cfg, cmd, check=True):
        calls.append(cmd)
        return _CP(255, "", "ssh: connect to host dest port 22: Connection refused")
    monkeypatch.setattr(sr, "run_remote", down)
    # No local fallout at all — the failure is surfaced, nothing is touched.
    # (_pin_host_key retries the transport once; its ssh-keygen/ssh-keyscan
    # probes are read-only against the SERVER and never touch the target disk —
    # allow only those.)
    def guard_local(argv, check=True):
        assert argv[0] in ("ssh-keygen", "ssh-keyscan"), \
            f"unexpected local command after transport failure: {argv}"
        return _CP(1, "", "")
    monkeypatch.setattr(sr, "run_local", guard_local)
    rc = sr.cmd_restore(_restore_args(server="dest", ssh_key=str(key)))
    assert rc == 1                                  # clean abort, not a crash


# --- cross-file pin: the provision scripts must never drift ------------------

def test_provision_scripts_stay_in_sync():
    # The sudoers grant is the transport-user security boundary AND both
    # directions' capability list: `btrfs receive` for snappersend's forward
    # sends, `btrfs send` for snapperrestore streaming back. snapperrestore
    # carries its own copy (self-contained by design) — a drifted copy would
    # provision a destination that breaks one direction or the other.
    args = ("snappersend", "ssh-ed25519 AAAAKEY c", "/srv/snapshots-recv")
    assert sr._provision_script(*args) == ss._provision_script(*args)
    s = sr._provision_script(*args)
    assert "$BTRFS send *" in s
    assert "$BTRFS receive *" in s


# --- cross-file pin: the flat-config parser must never drift -----------------

def test_parse_flat_config_stays_in_sync():
    # Both tools carry their own copy of the pure-stdlib KEY="value" parser (no
    # python3-dotenv on a live rescue ISO). A drifted copy would read the SAME
    # config file differently in the two directions — the source is pinned
    # byte-identical, exactly like _provision_script.
    import inspect
    assert (inspect.getsource(sr._parse_flat_config)
            == inspect.getsource(ss._parse_flat_config))


def test_parse_flat_config_parses_example():
    # The shipped config.example must parse to the values it documents.
    example = os.path.join(os.path.dirname(__file__), "config.example")
    d = sr._parse_flat_config(example)
    assert d["SERVER_HOST"] == "dest-host"      # inline comment stripped
    assert d["SSH_PORT"] == "22"
    assert d["SERVER_USER"] == "snappersend"
    assert d["RECV_BASE"] == "/srv/snapshots-recv"
    assert d["USE_MBUFFER"] == "yes"
    assert d["SUBVOLUMES"] == "root:/ home:/home bootmirror:/.bootmirror"
    assert d["TIMELINE_LIMIT_DAILY"] == "14"
    assert d["ROOT_TIMELINE_LIMIT_DAILY"] == "30"


def test_parse_flat_config_edge_cases(tmp_path):
    p = tmp_path / "cfg"
    p.write_text(
        "# a full-line comment\n"
        "\n"
        'SERVER_HOST="10.0.0.9"   # inline comment after quoted value\n'
        "EMPTY=\n"                              # empty value must not crash
        'QUOTED_EMPTY=""\n'
        'HASHVAL="a#b"\n'                       # hash inside quotes preserved
        "SPACED = value here\n"                 # spaces around '='
        "UNQUOTED=plain # trailing comment\n"   # inline comment on unquoted value
        'EQVAL="a=b=c"\n'                       # '=' inside the value
        "NAKED\n"                               # no '=' -> skipped
        "BAD-KEY=1\n"                           # non-identifier key -> skipped
        'DUP="first"\n'
        'DUP="second"\n')                       # last assignment wins
    d = sr._parse_flat_config(str(p))
    assert d["SERVER_HOST"] == "10.0.0.9"
    assert d["EMPTY"] == ""
    assert d["QUOTED_EMPTY"] == ""
    assert d["HASHVAL"] == "a#b"
    assert d["SPACED"] == "value here"
    assert d["UNQUOTED"] == "plain"
    assert d["EQVAL"] == "a=b=c"
    assert "NAKED" not in d
    assert "BAD-KEY" not in d
    assert d["DUP"] == "second"


def test_parse_flat_config_unterminated_quote_keeps_key(tmp_path):
    # A DR must not silently lose SERVER_HOST to a stray quote typo.
    p = tmp_path / "cfg"
    p.write_text('SERVER_HOST="10.0.0.9\n')     # missing closing quote
    d = sr._parse_flat_config(str(p))
    assert d["SERVER_HOST"] == "10.0.0.9"


# --- login-mode resolution: a surviving config must not force setup-dest ------

def _transport_args(cfg_path, **kw):
    return _restore_args(config=str(cfg_path), dry_run=False, **kw)


def test_key_missing_shows_login_menu_even_with_config(monkeypatch, tmp_path):
    # The bug this guards against: a config file can survive on the rescue media
    # while the transport key died with the machine. Interactive, no auth flag,
    # key absent -> the LOGIN MENU must appear (not the setup-dest dead-end).
    import types
    cfg_file = tmp_path / "config"
    cfg_file.write_text('SERVER_HOST="d"\nSSH_KEY="%s"\n' % (tmp_path / "gone-key"))
    monkeypatch.setattr(sr.sys, "stdin", types.SimpleNamespace(isatty=lambda: True))
    seen = {}
    monkeypatch.setattr(sr, "_login_menu",
                        lambda host: seen.setdefault("host", host) or "1")
    monkeypatch.setattr(sr, "_password_login",
                        lambda a, k, lu, inter: sr.Config(
                            server_host=k["server_host"], auth_mode="password"))
    monkeypatch.setattr(sr, "_verify_transport", lambda cfg: (True, "ok"))
    cfg = sr._restore_transport(_transport_args(cfg_file))
    assert seen.get("host") == "d"            # menu was shown
    assert cfg.auth_mode == "password"        # choice 1 -> password mode


def test_present_key_stays_silent_key_mode(monkeypatch, tmp_path):
    # The normal path: config + a real key present -> no menu, silent key mode.
    import types
    key = tmp_path / "key"; key.write_text("k")
    cfg_file = tmp_path / "config"
    cfg_file.write_text('SERVER_HOST="d"\nSSH_KEY="%s"\n' % key)
    monkeypatch.setattr(sr.sys, "stdin", types.SimpleNamespace(isatty=lambda: True))
    monkeypatch.setattr(sr, "_login_menu",
                        lambda host: pytest.fail("menu shown despite a usable key"))
    monkeypatch.setattr(sr, "_verify_transport", lambda cfg: (True, "ok"))
    cfg = sr._restore_transport(_transport_args(cfg_file))
    assert cfg.auth_mode == "key"


# --- transport config: the snappersend file is read for transport keys ONLY --

def test_transport_config_reads_subset_of_snappersend_config(tmp_path):
    p = tmp_path / "config"
    p.write_text('SERVER_HOST="10.0.0.9"\nSSH_PORT="2222"\n'
                 'SERVER_USER="xferuser"\nRECV_BASE="/srv/recv"\n'
                 'USE_MBUFFER="no"\n'
                 # full snappersend keys must be silently ignored:
                 'SUBVOLUMES="root:/ home:/home"\nTIMELINE_LIMIT_DAILY="14"\n'
                 'PARENT_TREE_BASE="/.snappersend"\n')
    kwargs = sr.load_config(str(p))
    cfg = sr.Config(**kwargs)     # would TypeError if non-transport keys leaked
    assert cfg.server_host == "10.0.0.9" and cfg.server_ssh_port == 2222
    assert cfg.server_user == "xferuser" and cfg.recv_base == "/srv/recv"
    assert cfg.use_mbuffer is False
    with pytest.raises(KeyError):
        q = tmp_path / "nohost"
        q.write_text('SSH_PORT="22"\n')
        sr.load_config(str(q))


def test_cli_parses():
    a = sr.build_parser().parse_args(["--dry-run"])
    assert a.dry_run is True
    a = sr.build_parser().parse_args([
        "--disk", "/dev/nvme0n1", "--esp-size", "1GiB",
        "--boot-size", "1GiB", "--swap-size", "4g", "--ssh-key", "/x/key",
        "--server", "10.0.0.1", "--snapshot", "20260703-140008+1000"])
    assert a.disk == "/dev/nvme0n1" and a.esp_size == "1GiB"
    assert a.ssh_key == "/x/key" and a.snapshot == "20260703-140008+1000"
    a = sr.build_parser().parse_args(["--config", "/tmp/c", "--dry-run"])
    assert a.config == "/tmp/c"
    # password / own-login flags
    a = sr.build_parser().parse_args(["--login", "james", "--server", "d"])
    assert a.login == "james" and a.password is False
    a = sr.build_parser().parse_args(["--password"])
    assert a.password is True


def test_crypttab_initramfs_option_injection():
    # The chroot-time crypttab must carry Debian's `initramfs` option (forced
    # inclusion — the hook cannot detect the target's devices from inside a
    # chroot). Idempotent; comments untouched; 3-field entries get an options
    # column.
    out = sr._crypttab_with_initramfs_option(_CRYPTTAB_TEMPLATE)
    fields = out.strip().split()
    assert fields[0] == "luks-2a13" and fields[2] == "none"
    assert fields[3] == "luks,discard,x-initrd.attach,initramfs"
    assert sr._crypttab_with_initramfs_option(out) == out          # idempotent
    out3 = sr._crypttab_with_initramfs_option("# c\nname UUID=x none\n")
    assert "name  UUID=x  none  initramfs" in out3
    assert out3.startswith("# c\n")


def test_epoch_matching_defaults_and_span_warning(monkeypatch):
    subvols = {
        "root": ["20260703-120021+1000-142-aaaa1111",
                 "20260703-140008+1000-144-bbbb2222"],
        "home": ["20260703-120021+1000-106-cccc3333",
                 "20260703-140008+1000-107-dddd4444"],
        "bootmirror": ["20260703-120021+1000-6-eeee5555"],  # missing the new epoch
    }
    warns = []
    monkeypatch.setattr(sr, "log_warn", lambda m: warns.append(m))
    chosen = sr._restore_choose_snapshots(subvols, "root",
                                          _restore_args(), interactive=False)
    # root + home take the newest shared epoch; bootmirror falls back to latest.
    assert chosen["root"].startswith("20260703-140008")
    assert chosen["home"].startswith("20260703-140008")
    assert chosen["bootmirror"].startswith("20260703-120021")
    assert any("SPAN EPOCHS" in w for w in warns)
    # An explicit epoch prefix selects the older matched set (no span warning).
    warns.clear()
    chosen = sr._restore_choose_snapshots(
        subvols, "root", _restore_args(snapshot="20260703-120021"),
        interactive=False)
    assert all(l.startswith("20260703-120021") for l in chosen.values())
    assert not any("SPAN EPOCHS" in w for w in warns)


def test_reconstruct_nested_symlink_targets(monkeypatch, tmp_path):
    """Dangling symlinks into a recreated-empty nested subvol get their target
    dirs rebuilt (mkdir -p, chowned to the link's owner) — including targets
    reached only VIA another symlink (the deep Brave CacheStorage case).
    Healthy links and links pointing outside any nested root are untouched."""
    import types
    home = tmp_path / "@home" / "u"
    nocow = home / ".nocow"          # stands in for the recreated-empty subvol
    nocow.mkdir(parents=True)
    # dangling: ~/.cache -> .nocow/cache
    (home / ".cache").symlink_to(".nocow/cache")
    # dangling, two hops + depth: resolves through ~/.cache into .nocow/cache
    sw = home / ".config/BraveSoftware/Brave-Browser/Default/Service Worker"
    sw.mkdir(parents=True)
    (sw / "CacheStorage").symlink_to(
        "../../../../../.cache/BraveSoftware/Brave-Browser/Default/CacheStorage")
    # NOT dangling: target already exists — must be left alone
    (nocow / "iso").mkdir()
    (home / "ISO").symlink_to(".nocow/iso")
    # dangling but OUTSIDE any nested root — must be left alone
    (home / "stray").symlink_to("elsewhere/place")

    monkeypatch.setattr(sr, "_RESTORE_TOP_MNT", str(tmp_path))
    chowns = []
    monkeypatch.setattr(sr.os, "chown",
                        lambda path, uid, gid: chowns.append((path, uid, gid)))
    plan = types.SimpleNamespace(
        nested=[("@home", "u/.nocow", True)],
        entries=[types.SimpleNamespace(fstype="btrfs", subvol="@home"),
                 types.SimpleNamespace(fstype="btrfs", subvol=None),
                 types.SimpleNamespace(fstype="ext4", subvol=None)])
    sr._reconstruct_nested_symlink_targets(plan)

    real_nocow = nocow.resolve()
    assert (real_nocow / "cache").is_dir()
    deep = real_nocow / "cache/BraveSoftware/Brave-Browser/Default/CacheStorage"
    assert deep.is_dir()
    # every created dir (leaf + intermediates up to, not past, the nested root)
    # chowned to the link owner's uid/gid
    st = os.lstat(home / ".cache")
    chowned = {p for p, _, _ in chowns}
    want = {str(deep)}
    for parent in [deep.parent, deep.parent.parent, deep.parent.parent.parent,
                   real_nocow / "cache"]:
        want.add(str(parent))
    assert want <= chowned
    assert all(u == st.st_uid and g == st.st_gid for _, u, g in chowns)
    assert str(real_nocow) not in chowned          # nested root itself untouched
    # healthy link's target untouched, stray link still dangling, nothing made
    assert (real_nocow / "iso").is_dir()
    assert not (home / "elsewhere").exists()

    # idempotent: a second pass (resumed restore) is a no-op
    chowns.clear()
    sr._reconstruct_nested_symlink_targets(plan)
    assert chowns == []


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-q"]))
