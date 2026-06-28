#!/usr/bin/env bash
# di-snapsend.sh — installer for di-snapsend (the Python engine is the tool;
# this .sh provisions the two ends, di-* suite convention).
#
#   di-snapsend.sh --server [PUBKEY_FILE]   provision the receive end (server)
#   di-snapsend.sh --laptop                 install the engine + timer (laptop)
#
# Server role: dedicated least-privilege transport user, receive area, scoped
# sudoers, and an optional forced-command ssh filter — reusable, tool-agnostic
# (mirrors di-btrbk-send.sh's server role).
# Laptop role: install /usr/local/bin/di-snapsend, write /etc/snapsend/config,
# generate the transport ssh key, install + enable the systemd timer + watchdog.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

# ---- logging (di-*.sh house style: [STEP]/[INFO]/[OK]/[WARN]/[ERROR]) --------
if [[ -t 2 ]]; then
    C_STEP=$'\033[1;36m'; C_INFO=$'\033[0;34m'; C_OK=$'\033[0;32m'
    C_WARN=$'\033[0;33m'; C_ERR=$'\033[0;31m'; C_RST=$'\033[0m'
else
    C_STEP=; C_INFO=; C_OK=; C_WARN=; C_ERR=; C_RST=
fi
step() { echo "${C_STEP}[STEP]${C_RST} $*" >&2; }
info() { echo "${C_INFO}[INFO]${C_RST} $*" >&2; }
ok()   { echo "${C_OK}[OK]${C_RST} $*" >&2; }
warn() { echo "${C_WARN}[WARN]${C_RST} $*" >&2; }
err()  { echo "${C_ERR}[ERROR]${C_RST} $*" >&2; }
die()  { err "$*"; exit 1; }

# ---- config knobs (override via env) ----------------------------------------
SNAPSEND_USER="${SNAPSEND_USER:-snapsend}"
RECV_BASE="${RECV_BASE:-/srv/snapshots-recv}"
ETC_DIR="${ETC_DIR:-/etc/snapsend}"
KEY_PATH="${KEY_PATH:-$ETC_DIR/ssh/id_ed25519}"
BIN_DIR="${BIN_DIR:-/usr/local/bin}"
DOC_DIR="${DOC_DIR:-/usr/local/share/doc/di-snapsend}"
UNIT_DIR="${UNIT_DIR:-/etc/systemd/system}"

require_root() { [[ ${EUID:-$(id -u)} -eq 0 ]] || die "must run as root"; }

usage() {
    cat >&2 <<EOF
di-snapsend.sh — installer (provisions the two ends of a replication link)

Usage:
  sudo $0 --dest   [PUBKEY_FILE]   provision the receive end (DESTINATION host)
  sudo $0 --source                 install the engine + timer (SOURCE host)

  Aliases: --dest = --destination = --server ;  --source = --laptop
  (--server/--laptop are the historical spellings, kept for back-compat.)

Destination role (idempotent) — run on the receiving host:
  - creates the '${SNAPSEND_USER}' transport user
  - creates ${RECV_BASE} (must be on a Btrfs filesystem)
  - installs a scoped sudoers rule (/etc/sudoers.d/snapsend)
  - installs the forced-command ssh filter
  - if PUBKEY_FILE is given, authorizes that key (locked down); else prints how

Source role (idempotent) — run on the sending host:
  - installs ${BIN_DIR}/di-snapsend and ${BIN_DIR}/snapsend-watchdog
  - writes ${ETC_DIR}/config (from config.example.toml) if absent
  - generates the transport key at ${KEY_PATH} if absent, prints the pubkey line
  - pins the destination SSH host key, installs + enables snapsend timers

Env overrides: SNAPSEND_USER RECV_BASE ETC_DIR KEY_PATH BIN_DIR UNIT_DIR
EOF
    exit "${1:-1}"
}

need_file() { [[ -f "$1" ]] || die "missing expected file next to installer: $1"; }

# ============================================================================
# SERVER ROLE
# ============================================================================
cmd_server() {
    require_root
    local pubkey_file="${1:-}"
    step "Provisioning di-snapsend receive end (user=${SNAPSEND_USER}, recv=${RECV_BASE})"

    command -v btrfs >/dev/null || die "btrfs-progs not installed (needed for receive)"

    # 1) transport user (key-only; password locked; real shell so the ssh
    #    forced-command can execute — restriction comes from the key + sudoers).
    if id "$SNAPSEND_USER" >/dev/null 2>&1; then
        info "user ${SNAPSEND_USER} already exists"
    else
        useradd --system --create-home --shell /bin/bash "$SNAPSEND_USER"
        passwd -l "$SNAPSEND_USER" >/dev/null
        ok "created system user ${SNAPSEND_USER} (password locked)"
    fi

    # 2) receive area — must live on Btrfs (it holds received subvolumes).
    mkdir -p "$RECV_BASE"
    local fstype; fstype="$(findmnt -no FSTYPE --target "$RECV_BASE" 2>/dev/null || echo '?')"
    if [[ "$fstype" != "btrfs" ]]; then
        warn "${RECV_BASE} is on '${fstype}', not btrfs — btrfs receive WILL FAIL there."
        warn "Point RECV_BASE at a Btrfs filesystem and re-run."
    else
        ok "${RECV_BASE} is on btrfs"
    fi
    # The tool creates per-subvol dirs via 'sudo mkdir -p' at runtime; receives
    # land as root. Keep the base root-owned; snapsend only ever touches it via
    # the scoped sudoers grant below.

    # 3) scoped sudoers — exactly the commands di-snapsend issues remotely.
    local sudoers="/etc/sudoers.d/snapsend"
    local tmp; tmp="$(mktemp)"
    cat > "$tmp" <<EOF
# Installed by di-snapsend.sh --server. Scoped to the commands di-snapsend runs.
Cmnd_Alias SNAPSEND_CMDS = \\
    /usr/bin/btrfs receive *, \\
    /usr/bin/btrfs subvolume show *, \\
    /usr/bin/btrfs subvolume delete *, \\
    /usr/bin/btrfs property get *, \\
    /usr/bin/mkdir -p *, \\
    /usr/bin/rmdir *, \\
    /usr/bin/ls *, \\
    /usr/bin/ln -sfn *, \\
    /usr/bin/rsync *
${SNAPSEND_USER} ALL=(root) NOPASSWD: SNAPSEND_CMDS
Defaults!SNAPSEND_CMDS !requiretty
EOF
    if visudo -c -f "$tmp" >/dev/null; then
        install -m 0440 "$tmp" "$sudoers"
        rm -f "$tmp"
        ok "installed scoped sudoers rule ${sudoers}"
    else
        rm -f "$tmp"
        die "generated sudoers failed validation — not installing"
    fi

    # 4) forced-command ssh filter (defense in depth on top of sudoers).
    need_file "$SCRIPT_DIR/snapsend-ssh-filter"
    install -m 0755 "$SCRIPT_DIR/snapsend-ssh-filter" "$BIN_DIR/snapsend-ssh-filter"
    ok "installed ${BIN_DIR}/snapsend-ssh-filter"

    # 5) authorize the laptop key (if provided), locked down with the filter.
    local ssh_dir="/home/${SNAPSEND_USER}/.ssh"
    local auth="${ssh_dir}/authorized_keys"
    local opts='command="/usr/local/bin/snapsend-ssh-filter",no-pty,no-agent-forwarding,no-port-forwarding,no-X11-forwarding'
    install -d -m 0700 -o "$SNAPSEND_USER" -g "$SNAPSEND_USER" "$ssh_dir"
    if [[ -n "$pubkey_file" ]]; then
        [[ -f "$pubkey_file" ]] || die "pubkey file not found: $pubkey_file"
        local key; key="$(< "$pubkey_file")"
        touch "$auth"
        if grep -qF "$key" "$auth" 2>/dev/null; then
            info "laptop key already authorized"
        else
            printf '%s %s\n' "$opts" "$key" >> "$auth"
            ok "authorized laptop key (forced command + no forwarding)"
        fi
        chown "$SNAPSEND_USER:$SNAPSEND_USER" "$auth"
        chmod 0600 "$auth"
    else
        warn "no PUBKEY_FILE given — add the laptop's key to ${auth} as:"
        echo "  ${opts} ssh-ed25519 AAAA...laptop-snapsend" >&2
    fi

    ok "server provisioning complete"
}

# ============================================================================
# LAPTOP ROLE
# ============================================================================
cmd_laptop() {
    require_root
    step "Installing di-snapsend on the laptop"

    # 1) dependencies
    command -v btrfs >/dev/null || die "btrfs-progs not installed"
    command -v ssh   >/dev/null || die "openssh-client not installed"
    command -v rsync >/dev/null || warn "rsync not installed — boot tier will fail"
    # mbuffer smooths throughput + gives a live rate readout (config default
    # use_mbuffer=true). It's in Debian main, so install it rather than nag every
    # run with a [WARN] (batteries-included, consistent with the rest of the suite).
    if command -v mbuffer >/dev/null; then
        info "mbuffer present"
    elif command -v apt-get >/dev/null; then
        info "installing mbuffer (Debian main; config defaults to use_mbuffer=true)"
        if DEBIAN_FRONTEND=noninteractive apt-get install -y mbuffer >/dev/null 2>&1; then
            ok "installed mbuffer"
        else
            warn "could not install mbuffer — the tool still runs without it"
            warn "(set use_mbuffer=false in ${ETC_DIR}/config to silence the runtime warning)"
        fi
    else
        warn "mbuffer not installed and no apt-get — set use_mbuffer=false to silence the runtime warning"
    fi
    python3 - <<'PY' || die "need Python >= 3.11 (for tomllib)"
import sys; sys.exit(0 if sys.version_info >= (3, 11) else 1)
PY
    ok "dependencies present"

    # 2) engine + watchdog
    need_file "$SCRIPT_DIR/di-snapsend"
    need_file "$SCRIPT_DIR/systemd/snapsend-watchdog"
    install -m 0755 "$SCRIPT_DIR/di-snapsend" "$BIN_DIR/di-snapsend"
    install -m 0755 "$SCRIPT_DIR/systemd/snapsend-watchdog" "$BIN_DIR/snapsend-watchdog"
    ok "installed ${BIN_DIR}/di-snapsend and ${BIN_DIR}/snapsend-watchdog"

    # 3) config
    install -d -m 0755 "$ETC_DIR"
    if [[ -f "$ETC_DIR/config" ]]; then
        info "${ETC_DIR}/config exists — leaving it untouched"
    else
        need_file "$SCRIPT_DIR/config.example.toml"
        install -m 0600 "$SCRIPT_DIR/config.example.toml" "$ETC_DIR/config"
        ok "wrote default ${ETC_DIR}/config — EDIT [server].host before first run"
    fi

    # 4) transport ssh key
    install -d -m 0700 "$(dirname "$KEY_PATH")"
    if [[ -f "$KEY_PATH" ]]; then
        info "ssh key ${KEY_PATH} exists"
    else
        ssh-keygen -t ed25519 -N "" -C "snapsend@$(hostname)" -f "$KEY_PATH" >/dev/null
        chmod 0600 "$KEY_PATH"
        ok "generated transport key ${KEY_PATH}"
    fi
    step "Register this key on the server:"
    echo "  sudo ./di-snapsend.sh --server ${KEY_PATH}.pub   (run on the server with this file copied over)" >&2
    echo "  --- ${KEY_PATH}.pub ---" >&2
    cat "${KEY_PATH}.pub" >&2

    # 4b) pin the server's SSH host key so the FIRST transfer doesn't die with
    #     "Host key verification failed". The service runs as ROOT, so the pin
    #     must land in ROOT's known_hosts (not the invoking user's). ssh-keyscan
    #     only fetches the host key — it runs no remote command, so it is NOT
    #     rejected by the forced-command filter (a command probe would be).
    local cfg_host cfg_port kh
    cfg_host="$(sed -n 's/^[[:space:]]*host[[:space:]]*=[[:space:]]*"\([^"]*\)".*/\1/p' "$ETC_DIR/config" | head -n1)"
    cfg_port="$(sed -n 's/^[[:space:]]*ssh_port[[:space:]]*=[[:space:]]*\([0-9]\+\).*/\1/p' "$ETC_DIR/config" | head -n1)"
    cfg_port="${cfg_port:-22}"
    kh="/root/.ssh/known_hosts"
    install -d -m 0700 /root/.ssh
    touch "$kh" && chmod 0600 "$kh"
    step "Pinning ${cfg_host}:${cfg_port} host key into ${kh} (service runs as root)"
    local scanned
    if scanned="$(ssh-keyscan -p "$cfg_port" -t ed25519 "$cfg_host" 2>/dev/null)" && [[ -n "$scanned" ]]; then
        while IFS= read -r line; do
            [[ -z "$line" ]] && continue
            grep -qF "$line" "$kh" 2>/dev/null || printf '%s\n' "$line" >> "$kh"
        done <<< "$scanned"
        ok "pinned ${cfg_host} host key"
    else
        warn "could not fetch ${cfg_host}:${cfg_port} host key (server unreachable, or [server].host not set yet)"
        warn "after setting [server].host, run once as root:"
        echo "  sudo ssh-keyscan -p ${cfg_port} -t ed25519 ${cfg_host} >> ${kh}" >&2
    fi

    # 5) runtime dirs + log
    install -d -m 0755 /var/lib/snapsend
    touch /var/log/snapsend.log && chmod 0640 /var/log/snapsend.log
    ok "created /var/lib/snapsend and /var/log/snapsend.log"

    # 6) docs (best-effort)
    if [[ -f "$SCRIPT_DIR/README.md" ]]; then
        install -d -m 0755 "$DOC_DIR"
        install -m 0644 "$SCRIPT_DIR/README.md" "$DOC_DIR/README.md"
    fi

    # 7) systemd units
    for u in snapsend.service snapsend.timer snapsend-watchdog.service snapsend-watchdog.timer; do
        need_file "$SCRIPT_DIR/systemd/$u"
        install -m 0644 "$SCRIPT_DIR/systemd/$u" "$UNIT_DIR/$u"
    done
    if command -v systemctl >/dev/null; then
        systemctl daemon-reload
        systemctl enable --now snapsend.timer snapsend-watchdog.timer
        ok "enabled snapsend.timer + snapsend-watchdog.timer"
        info "first run:  sudo di-snapsend --dry-run     (verify), then:  sudo systemctl start snapsend.service"
    else
        warn "systemctl not found — units installed but not enabled"
    fi

    ok "laptop installation complete"
}

# ============================================================================
main() {
    [[ $# -ge 1 ]] || usage 1
    # Roles are source (the sending side) and dest (the receiving side). The
    # historical spellings --laptop/--server are kept as aliases so existing
    # scripts/muscle-memory keep working; --source/--dest are the generic names.
    local role
    case "$1" in
        --source|--laptop)        role=source ;;
        --dest|--destination|--server) role=dest ;;
        -h|--help) usage 0 ;;
        *) err "unknown role: $1"; usage 1 ;;
    esac
    # Test-only introspection: with SNAPSEND_DISPATCH_TEST set, report the resolved
    # role and exit before doing any work (so alias dispatch is unit-testable
    # without a real install). Inert in normal use.
    if [[ -n "${SNAPSEND_DISPATCH_TEST:-}" ]]; then
        echo "role=${role}"
        exit 0
    fi
    shift
    case "$role" in
        source) cmd_laptop ;;
        dest)   cmd_server "${1:-}" ;;
    esac
}
main "$@"
