# Packaging snappersend for Debian

snappersend is a single Python script plus config and systemd units — no compiled
build — so it packages into one architecture-independent `.deb` that installs on the
**source** only. The destination needs no package: `snappersend setup-dest` provisions
it over SSH.

This produces a `snappersend` package that installs:

| File | Destination |
|------|-------------|
| `snappersend` | `/usr/bin/snappersend` |
| `snappersend.service` | `/lib/systemd/system/snappersend.service` |
| `bootmirror-sync.service` | `/lib/systemd/system/bootmirror-sync.service` |
| `10-snappersend.conf` (timeline drop-in) | `/usr/lib/systemd/system/snapper-timeline.service.d/` |
| `20-bootmirror.conf` (timeline drop-in) | `/usr/lib/systemd/system/snapper-timeline.service.d/` |
| `config.example` | `/usr/share/doc/snappersend/examples/` |
| `README.md` | `/usr/share/doc/snappersend/` |

The live config (`/etc/snappersend/config`) is **not** shipped — `setup-dest` creates it,
or copy the example. That keeps the package free of `conffile` prompts and secrets.

---

## 1. One-time: install the build tools

```sh
sudo apt-get install -y build-essential debhelper devscripts dpkg-dev
```

## 2. Create the `debian/` tree

Run this from the top of the snappersend checkout. It writes the whole `debian/`
directory in one go (edit the `Maintainer`/name to taste afterwards).

```sh
cd /path/to/snappersend            # the checkout containing the `snappersend` script
mkdir -p debian/source debian/snapper-timeline.service.d

# --- source format ---------------------------------------------------------
echo '3.0 (native)' > debian/source/format

# --- control ---------------------------------------------------------------
cat > debian/control <<'EOF'
Source: snappersend
Section: admin
Priority: optional
Maintainer: Your Name <you@example.com>
Build-Depends: debhelper-compat (= 13)
Standards-Version: 4.6.2
Rules-Requires-Root: no
Homepage: https://github.com/jelloir/snappersend

Package: snappersend
Architecture: all
Depends: ${misc:Depends},
         python3,
         python3-dotenv,
         btrfs-progs,
         openssh-client,
         rsync,
         snapper
Recommends: mbuffer
Description: Snapper-native Btrfs send/receive replication with a parent tree
 Replicates Snapper-managed Btrfs snapshots from a source host to a destination
 over SSH (btrfs send | ssh btrfs receive), keeping its own read-only parent
 clones so incrementals survive arbitrary offline gaps. Retention on the
 destination is pure grandfather-father-son (WYSIWYG).
 .
 Installs on the source only. The destination is provisioned over SSH by
 `snappersend setup-dest` and runs no snappersend code. Requires Snapper to be
 installed and taking timeline snapshots on the source.
EOF

# --- changelog (uses the current date; bump the version as you release) -----
cat > debian/changelog <<EOF
snappersend (1.0.0-1) unstable; urgency=medium

  * Initial Debian packaging.

 -- Your Name <you@example.com>  $(date -R)
EOF

# --- copyright (minimal; adjust the licence to yours) -----------------------
cat > debian/copyright <<'EOF'
Format: https://www.debian.org/doc/packaging-manuals/copyright-format/1.0/
Upstream-Name: snappersend

Files: *
Copyright: Your Name <debian.88@luck888.win>
License: MIT
EOF

# --- rules -----------------------------------------------------------------
cat > debian/rules <<'EOF'
#!/usr/bin/make -f
%:
	dh $@

# Neither unit has an [Install] section — both are triggered by the timeline
# drop-ins — so never enable or start them at install time. The second
# dh_installsystemd call picks up debian/snappersend.bootmirror-sync.service.
override_dh_installsystemd:
	dh_installsystemd --no-enable --no-start
	dh_installsystemd --no-enable --no-start --name=bootmirror-sync
EOF
chmod +x debian/rules

# --- what goes where (dh_install) ------------------------------------------
cat > debian/install <<'EOF'
snappersend usr/bin
config.example usr/share/doc/snappersend/examples
debian/snapper-timeline.service.d/10-snappersend.conf usr/lib/systemd/system/snapper-timeline.service.d
debian/snapper-timeline.service.d/20-bootmirror.conf usr/lib/systemd/system/snapper-timeline.service.d
EOF

# --- README into the doc dir -----------------------------------------------
echo 'README.md' > debian/docs

# --- systemd units: same as systemd/*.service but with the packaged /usr/bin
#     path (and doc path) instead of /usr/local. dh_installsystemd picks up
#     debian/<pkg>.service automatically and debian/<pkg>.bootmirror-sync.service
#     via the --name call in debian/rules. ----------------------------------
sed -e 's#/usr/local/bin/snappersend#/usr/bin/snappersend#' \
    -e 's#/usr/local/share/doc/snappersend#/usr/share/doc/snappersend#' \
    systemd/snappersend.service > debian/snappersend.service
sed -e 's#/usr/local/bin/snappersend#/usr/bin/snappersend#' \
    systemd/bootmirror-sync.service > debian/snappersend.bootmirror-sync.service

# --- timeline drop-ins (verbatim from the repo) ----------------------------
cp systemd/snapper-timeline.service.d/10-snappersend.conf \
   debian/snapper-timeline.service.d/10-snappersend.conf
cp systemd/snapper-timeline.service.d/20-bootmirror.conf \
   debian/snapper-timeline.service.d/20-bootmirror.conf
```

## 3. Build the package

```sh
dpkg-buildpackage -us -uc -b        # -b = binary only, -us -uc = don't sign
```

The `.deb` lands in the **parent** directory:

```sh
ls ../snappersend_*_all.deb
```

(Optional) sanity-check with lintian — a few pedantic warnings are fine for a
personal package:

```sh
lintian ../snappersend_*_all.deb
```

## 4. Install and verify

```sh
sudo apt-get install -y ../snappersend_*_all.deb   # pulls in the dependencies
snappersend --help
systemctl cat snappersend.service | grep ExecStart  # -> /usr/bin/snappersend
```

Then configure and provision exactly as in the README:

```sh
sudo snappersend setup-dest admin@backup-host       # creates config if absent,
                                                    # provisions the destination
sudo snappersend --dry-run
sudo snappersend
```

For the boot tier, provision `@bootmirror` on the source (one-time, per host — a
subvolume/fstab/snapper change the package deliberately can't make for you; see the
README's "Provisioning `@bootmirror`" section):

```sh
# top-level subvol mounted at /.bootmirror + its own snapper config
sudo btrfs subvolume create <top-level>/@bootmirror   # + fstab entry, then mount
sudo snapper -c bootmirror create-config /.bootmirror
# declare it in /etc/snappersend/config:
#   SUBVOLUMES="root:/ home:/home bootmirror:/.bootmirror"
sudo snappersend --bootmirror-sync                    # seed the mirror once
```

The packaged `bootmirror-sync.service` + `20-bootmirror.conf` drop-in then refresh it
before every snapper timeline tick automatically.

## 5. Upgrades and removal

- **New version:** bump `debian/changelog` (`dch -i`, or edit the version), rebuild,
  and `sudo apt-get install ./snappersend_<new>_all.deb`.
- **Remove (keep config):** `sudo apt-get remove snappersend`.
- **Purge (also remove config):** `sudo apt-get purge snappersend`. This does **not**
  touch the destination — decommission that first with
  `sudo snappersend decom-dest admin@backup-host` (see the README).

---

### Notes

- **Architecture `all`** — pure Python + shell, no compiled code, so one `.deb` runs on
  any Debian architecture.
- **The config is not a `conffile`.** It's created by `setup-dest` (or copied from
  `/usr/share/doc/snappersend/examples/config.example`), so upgrades never prompt about
  it and no secrets live in the package.
- **systemd wiring is automatic.** The packaged `snappersend.service` plus the
  `snapper-timeline.service.d/10-snappersend.conf` drop-in mean snappersend runs right
  after each Snapper timeline snapshot; `dh_installsystemd` runs `daemon-reload` for you.
  The service is deliberately never enabled on its own timer.
- **Building in a clean chroot** (recommended for release-quality builds) — install
  `sbuild`/`pbuilder` and run `sbuild -d unstable` instead of `dpkg-buildpackage`; it
  catches missing `Build-Depends` and dependency issues the local build can mask.
