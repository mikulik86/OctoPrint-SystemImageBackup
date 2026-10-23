#!/bin/bash
#
# One-time setup for the OctoPrint "System Image Backup" plugin. Run it over SSH with sudo.
# The plugin's tab shows the exact command, including the right --user and --job-file.
#
#   sudo bash install.sh --user pi --job-file /home/pi/.octoprint/data/systemimagebackup/job.conf [--with-image-utils]
#   sudo bash install.sh --uninstall
#
# Installs:
#   /usr/local/sbin/octopi-image-backup                 helper script (the backup job)
#   /etc/systemd/system/octopi-image-backup.service     systemd unit that runs it
#   /etc/default/octopi-image-backup                    where the plugin's job file is
#   /etc/sudoers.d/octopi-image-backup                  lets OctoPrint start that unit, test the
#                                                       destination and save the SMB password -
#                                                       nothing else
#   /etc/octopi-image-backup/                           SMB password (file readable by root only)
#   /var/lib/octopi-image-backup/                       status + log of the last backup
#   rsync, gdisk, cifs-utils, nfs-common (apt) if missing
#   image-utils (with --with-image-utils)               /usr/local/sbin/image-*

set -euo pipefail

HELPER_DST=/usr/local/sbin/octopi-image-backup
UNIT_NAME=octopi-image-backup.service
UNIT_DST=/etc/systemd/system/$UNIT_NAME
ENV_DST=/etc/default/octopi-image-backup
SUDOERS_DST=/etc/sudoers.d/octopi-image-backup
STATE_DIR=/var/lib/octopi-image-backup
RUN_DIR=/run/octopi-image-backup        # progress view while a backup runs (RAM)
CRED_DIR=/etc/octopi-image-backup
NET_MNT=/mnt/octopi-image-backup

# image-utils by RonR (Raspberry Pi forums), via the seamusdemora GitHub mirror, pinned to a
# known commit and checked against these hashes.
IU_BASE=https://raw.githubusercontent.com/seamusdemora/RonR-RPi-image-utils/0ee5757f43eca29c581ccb6d7ee8818e6ed2cb98
IU_FILES=(
    "image-backup  636728a813b9ee1621adf2b59ce1b831a0be5deb5cbc814366c8746da63e9699"
    "image-check   2b7a5e28898ffc7f15318955e1c61f0141baf4ef66bf653fb694e02aeaccfb19"
    "image-info    7202868356b64ed02927d7d68093cd533e210ad55bb2e871c45e34e37c05a78e"
    "image-mount   c7d282b440edc34473b081e760aa9605f05c10ae6dcdcfda1dd74a80a21e41b0"
    "image-shrink  d9c30e495e63bb32d569148af58f60cac03fa0fddea3f56a5dc18ea28c36762f"
    "image-compare 4cb0dcb9e58e65281f7b7c50440c38e54b39cd60130d455719e380fa7c3becca"
)

SRC_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
OP_USER="${SUDO_USER:-}"
JOB_FILE=""
OP_SERVICE=octoprint.service
WITH_IU=0
UNINSTALL=0

say()  { printf '\033[1m==>\033[0m %s\n' "$*"; }
warn() { printf '\033[1;33mWARNING:\033[0m %s\n' "$*" >&2; }
die()  { printf '\033[1;31mERROR:\033[0m %s\n' "$*" >&2; exit 1; }

usage() {
    sed -n '2,9p' "$0" | sed 's/^# \{0,1\}//'
    exit "${1:-0}"
}

while (( $# )); do
    case "$1" in
        --user)             OP_USER="${2:-}"; shift 2 ;;
        --job-file)         JOB_FILE="${2:-}"; shift 2 ;;
        --service)          OP_SERVICE="${2:-}"; shift 2 ;;
        --with-image-utils) WITH_IU=1; shift ;;
        --uninstall)        UNINSTALL=1; shift ;;
        -h|--help)          usage 0 ;;
        *)                  warn "Unknown option: $1"; usage 1 ;;
    esac
done

[[ $EUID -eq 0 ]] || die "Run this with sudo: sudo bash $0 ..."

# ------------------------------------------------------------ uninstall ---
if (( UNINSTALL )); then
    if systemctl is-active --quiet "$UNIT_NAME"; then
        die "A backup is running right now. Try again when it has finished."
    fi
    if mountpoint -q "$NET_MNT" 2>/dev/null; then
        umount "$NET_MNT" 2>/dev/null || umount -l "$NET_MNT" 2>/dev/null || true
    fi
    rmdir "$NET_MNT" 2>/dev/null || true
    say "Removing helper, service, sudoers rule, saved SMB password and status folder"
    rm -f "$HELPER_DST" "$UNIT_DST" "$ENV_DST" "$SUDOERS_DST"
    rm -rf "$STATE_DIR" "$CRED_DIR" "$RUN_DIR"
    systemctl daemon-reload
    say "Done. image-utils (/usr/local/sbin/image-*), rsync, gdisk, cifs-utils and nfs-common were left installed."
    exit 0
fi

# -------------------------------------------------------------- checks ----
[[ -n "$OP_USER" ]] || die "Missing --user (the user OctoPrint runs as, usually pi)."
[[ "$OP_USER" =~ ^[a-z_][a-z0-9_-]*$ ]] || die "Invalid user name '$OP_USER'."
id "$OP_USER" >/dev/null 2>&1 || die "User '$OP_USER' does not exist."
[[ "$OP_USER" != root ]] || die "--user must be the user OctoPrint runs as, not root."

[[ -n "$JOB_FILE" ]] || die "Missing --job-file (copy the full command from the plugin's tab)."
[[ "$JOB_FILE" == /* ]] || die "--job-file must be an absolute path."
case "$JOB_FILE" in
    *[\"\$\`\\]* | *[[:cntrl:]]* | *" "*) die "--job-file contains unsupported characters." ;;
esac
[[ -d "$(dirname "$JOB_FILE")" ]] || warn "$(dirname "$JOB_FILE") does not exist yet - is the plugin installed and OctoPrint restarted?"

[[ "$OP_SERVICE" == *.service ]] || OP_SERVICE="$OP_SERVICE.service"
[[ "$OP_SERVICE" =~ ^[A-Za-z0-9@._:-]+\.service$ ]] || die "Invalid service name '$OP_SERVICE'."
systemctl cat "$OP_SERVICE" >/dev/null 2>&1 \
    || die "systemd does not know $OP_SERVICE. Pass the OctoPrint service name with --service."

for f in octopi-image-backup octopi-image-backup.service; do
    [[ -f "$SRC_DIR/$f" ]] || die "$SRC_DIR/$f is missing - run this script from the plugin's helper folder."
done

SYSTEMCTL="$(command -v systemctl)"
[[ -n "$SYSTEMCTL" ]] || die "systemctl not found."
command -v visudo >/dev/null 2>&1 || die "visudo not found (is sudo installed?)."

# --------------------------------------------------------- apt packages ---
missing=()
command -v rsync  >/dev/null 2>&1 || missing+=(rsync)
command -v sgdisk >/dev/null 2>&1 || missing+=(gdisk)
command -v mount.cifs >/dev/null 2>&1 || missing+=(cifs-utils)
command -v mount.nfs >/dev/null 2>&1 || missing+=(nfs-common)
if (( ${#missing[@]} )); then
    say "Installing ${missing[*]}"
    if ! DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends "${missing[@]}"; then
        apt-get update
        DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends "${missing[@]}"
    fi
fi

# ----------------------------------------------------------- image-utils ---
if (( WITH_IU )); then
    say "Installing image-utils from $IU_BASE"
    tmp="$(mktemp -d)"
    trap 'rm -rf "$tmp"' EXIT
    for entry in "${IU_FILES[@]}"; do
        read -r name sum <<< "$entry"
        if command -v curl >/dev/null 2>&1; then
            curl -fsSL -o "$tmp/$name" "$IU_BASE/$name"
        else
            wget -q -O "$tmp/$name" "$IU_BASE/$name"
        fi
        echo "$sum  $tmp/$name" | sha256sum -c --quiet - || die "Checksum mismatch for $name - not installing it."
    done
    for entry in "${IU_FILES[@]}"; do
        read -r name _ <<< "$entry"
        install -m 0755 "$tmp/$name" "/usr/local/sbin/$name"
        echo "    /usr/local/sbin/$name"
    done
elif [[ ! -x /usr/local/sbin/image-backup ]] && ! command -v image-backup >/dev/null 2>&1; then
    warn "image-backup (image-utils) is not installed. Re-run with --with-image-utils, or install it yourself."
fi

# --------------------------------------------------------------- helper ---
say "Installing helper and systemd service"
install -m 0755 "$SRC_DIR/octopi-image-backup" "$HELPER_DST"
install -m 0644 "$SRC_DIR/octopi-image-backup.service" "$UNIT_DST"

cat > "$ENV_DST" <<EOF
# Written by the System Image Backup plugin installer ($(date '+%F %T')).
# Job file written by the OctoPrint plugin (settings of the next backup):
JOB_FILE="$JOB_FILE"
# OctoPrint's systemd service, stopped during the backup:
OCTOPRINT_SERVICE=$OP_SERVICE
EOF
chmod 0644 "$ENV_DST"

mkdir -p "$STATE_DIR" "$CRED_DIR"
chmod 0755 "$STATE_DIR" "$CRED_DIR"

# ---------------------------------------------------------------- sudo ----
say "Allowing $OP_USER to start the backup, test the destination and save the SMB password"
rule="$OP_USER ALL=(root) NOPASSWD: $SYSTEMCTL start --no-block $UNIT_NAME"
rule_test="$OP_USER ALL=(root) NOPASSWD: $HELPER_DST test-destination"
rule_cred="$OP_USER ALL=(root) NOPASSWD: $HELPER_DST set-credentials"
tmp_rule="$(mktemp)"
{
    echo "# Allows the OctoPrint System Image Backup plugin to start its backup job,"
    echo "# check the backup destination and store the SMB password. Nothing else."
    echo "$rule"
    echo "$rule_test"
    echo "$rule_cred"
} > "$tmp_rule"
visudo -cf "$tmp_rule" >/dev/null || { rm -f "$tmp_rule"; die "Generated sudoers rules are invalid."; }
install -m 0440 -o root -g root "$tmp_rule" "$SUDOERS_DST"
rm -f "$tmp_rule"

systemctl daemon-reload

say "Done."
echo "    helper:   $HELPER_DST (version $("$HELPER_DST" --version))"
echo "    service:  $UNIT_DST"
echo "    job file: $JOB_FILE"
echo "    sudo:     $rule"
echo "              $rule_test"
echo "              $rule_cred"
echo
echo "Back in OctoPrint, open the Image Backup tab and click 'Check again'."
