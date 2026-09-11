#!/usr/bin/env bash
# Synchronise the offline TacCap host (.4) with the TRON2 workstation.
#
# Run this on the workstation:
#   ./sync_tron2_time.sh
#
# It performs an immediate correction over SSH, then configures .4 to keep
# synchronising from the workstation after every reboot. Passwords are read
# only into shell memory and are never written to disk by this script.

set -Eeuo pipefail
IFS=$'\n\t'

REMOTE_USER="guest"
REMOTE_HOST="10.192.1.4"
LOCAL_NTP_IP="10.192.1.110"
SSH_CONNECT_TIMEOUT_S="8"
OFFSET_SAMPLE_COUNT="5"
MAX_OFFSET_SAMPLE_RTT_MS="50"

usage() {
    cat <<'EOF'
Usage: sync_tron2_time.sh [options]

Synchronise guest@10.192.1.4 with the workstation's 10.192.1.110 clock.

Options:
  --remote-user USER   SSH user on .4 (default: guest)
  --remote-host HOST   .4 address (default: 10.192.1.4)
  --local-ip IP        workstation address used as NTP server (default: 10.192.1.110)
  -h, --help           show this help

Stop teleoperation/recording before running this script because the first
correction can make a sub-second clock step on .4.
EOF
}

while (($# > 0)); do
    case "$1" in
        --remote-user)
            (($# >= 2)) || { echo "missing value for --remote-user" >&2; exit 2; }
            REMOTE_USER="$2"
            shift 2
            ;;
        --remote-host)
            (($# >= 2)) || { echo "missing value for --remote-host" >&2; exit 2; }
            REMOTE_HOST="$2"
            shift 2
            ;;
        --local-ip)
            (($# >= 2)) || { echo "missing value for --local-ip" >&2; exit 2; }
            LOCAL_NTP_IP="$2"
            shift 2
            ;;
        -h|--help)
            usage
            exit 0
            ;;
        *)
            echo "unknown option: $1" >&2
            usage >&2
            exit 2
            ;;
    esac
done

valid_ipv4() {
    [[ "$1" =~ ^([0-9]{1,3}\.){3}[0-9]{1,3}$ ]] || return 1
    local octet
    local -a octets
    IFS='.' read -r -a octets <<< "$1"
    for octet in "${octets[@]}"; do
        ((10#$octet <= 255)) || return 1
    done
}

valid_ipv4 "$LOCAL_NTP_IP" || {
    echo "invalid --local-ip: $LOCAL_NTP_IP" >&2
    exit 2
}

REMOTE="${REMOTE_USER}@${REMOTE_HOST}"
SSH_CONTROL_DIR="$(mktemp -d -t tron2-time-sync.XXXXXX)"
SSH_SOCKET="$SSH_CONTROL_DIR/socket"
cleanup() {
    if [[ -S "$SSH_SOCKET" ]]; then
        ssh "${SSH_OPTS[@]}" -O exit "$REMOTE" >/dev/null 2>&1 || true
    fi
    rm -rf -- "$SSH_CONTROL_DIR"
}
trap cleanup EXIT

SSH_OPTS=(
    -o ConnectTimeout="$SSH_CONNECT_TIMEOUT_S"
    -o ServerAliveInterval=5
    -o ServerAliveCountMax=2
    -o ControlMaster=auto
    -o ControlPersist=60
    -o ControlPath="$SSH_SOCKET"
    -o BatchMode=no
    -o PubkeyAuthentication=no
    -o PreferredAuthentications=password,keyboard-interactive
)

if ((EUID == 0)); then
    SUDO=()
else
    SUDO=(sudo)
    "${SUDO[@]}" -v
fi

command -v ssh >/dev/null || { echo "ssh is required" >&2; exit 1; }
command -v sshpass >/dev/null || {
    echo "sshpass is required to automate SSH password authentication" >&2
    echo "install it on the workstation with: sudo apt-get install -y sshpass" >&2
    exit 1
}
command -v ip >/dev/null || { echo "iproute2 (ip) is required" >&2; exit 1; }

if ! ip -4 -o addr show | awk -v wanted="$LOCAL_NTP_IP" '
    { split($4, address, "/"); if (address[1] == wanted) found = 1 }
    END { exit !found }
'; then
    echo "the workstation does not have IPv4 address $LOCAL_NTP_IP" >&2
    echo "check with: ip -4 -brief address" >&2
    exit 1
fi

if ! command -v chronyc >/dev/null || ! command -v chronyd >/dev/null; then
    echo "chrony is not installed on the workstation; installing it..."
    "${SUDO[@]}" apt-get install -y chrony
fi

LOCAL_DROPIN="/etc/chrony/conf.d/tron2-local-server.conf"
STAMP="$(date +%Y%m%d-%H%M%S)"
"${SUDO[@]}" mkdir -p /etc/chrony/conf.d
if "${SUDO[@]}" test -e "$LOCAL_DROPIN"; then
    "${SUDO[@]}" cp -a "$LOCAL_DROPIN" "${LOCAL_DROPIN}.bak.${STAMP}"
fi

printf '%s\n' \
    '# Managed by sync_tron2_time.sh' \
    'port 123' \
    'allow 10.192.1.4/32' \
    'local stratum 8' | "${SUDO[@]}" tee "$LOCAL_DROPIN" >/dev/null

"${SUDO[@]}" chronyd -p >/dev/null
"${SUDO[@]}" systemctl enable --now chrony >/dev/null
"${SUDO[@]}" systemctl restart chrony

if ! "${SUDO[@]}" ss -lun 2>/dev/null | grep -Eq '(^|[[:space:]])[^[:space:]]*:123([[:space:]]|$)'; then
    echo "chrony is not listening on UDP port 123" >&2
    echo "check: sudo journalctl -u chrony -n 50 --no-pager" >&2
    exit 1
fi

echo "workstation NTP service is listening on $LOCAL_NTP_IP:123"

# A normal interactive SSH login on .4 shows a ROS selector. The command mode
# used here has no TTY, so .bashrc is not sourced and that menu is skipped
# (equivalent to choosing 3 / No ROS). First establish the SSH master so its
# connection setup latency is not included in the time correction.
if [[ ! -r /dev/tty ]]; then
    echo "a controlling terminal is required for password input" >&2
    exit 1
fi
REMOTE_PASSWORD=""
IFS= read -r -s -p "password for $REMOTE (SSH and sudo): " REMOTE_PASSWORD </dev/tty
printf '\n' >/dev/tty

echo "connecting to $REMOTE ..."
if ! timeout 60s sshpass -d 4 ssh "${SSH_OPTS[@]}" -fN "$REMOTE" 4< <(printf '%s\n' "$REMOTE_PASSWORD"); then
    unset REMOTE_PASSWORD
    echo "SSH connection failed; no clock changes were made on $REMOTE" >&2
    exit 1
fi

# Warm up the first multiplexed SSH session.  Its startup/PAM scheduling can
# make one request hundreds of milliseconds slower than subsequent requests.
timeout 15s ssh -S "$SSH_SOCKET" "${SSH_OPTS[@]}" "$REMOTE" ':' >/dev/null

# Estimate the clock offset with several midpoint samples and keep the sample
# with the shortest SSH round trip.  A long or asymmetric request would turn
# half of its transport latency into a false clock offset, so do not change the
# remote clock when every sample exceeds the safety limit.
measure_remote_offset() {
    local best_rtt=""
    local best_offset=""
    local i local_before remote_sample local_after stats offset rtt
    for ((i = 0; i < OFFSET_SAMPLE_COUNT; i++)); do
        local_before="$(date +%s.%N)"
        if ! remote_sample="$(timeout 15s ssh -S "$SSH_SOCKET" "${SSH_OPTS[@]}" "$REMOTE" 'date +%s.%N' 2>/dev/null)"; then
            continue
        fi
        local_after="$(date +%s.%N)"
        stats="$(LC_ALL=C awk -v before="$local_before" -v after="$local_after" -v remote="$remote_sample" 'BEGIN {
            midpoint = (before + after) / 2.0
            printf "%.9f %.3f", remote - midpoint, (after - before) * 1000.0
        }')"
        IFS=' ' read -r offset rtt <<< "$stats"
        if [[ -z "$best_rtt" ]] || awk -v candidate="$rtt" -v current="$best_rtt" 'BEGIN { exit !(candidate < current) }'; then
            best_offset="$offset"
            best_rtt="$rtt"
        fi
        sleep 0.05
    done
    [[ -n "$best_rtt" ]] || return 1
    printf '%s %s\n' "$best_offset" "$best_rtt"
}

if ! INITIAL_STATS="$(measure_remote_offset)"; then
    unset REMOTE_PASSWORD
    echo "could not read the .4 clock for offset measurement" >&2
    exit 1
fi
IFS=' ' read -r INITIAL_OFFSET INITIAL_RTT_MS <<< "$INITIAL_STATS"
if awk -v rtt="$INITIAL_RTT_MS" -v limit="$MAX_OFFSET_SAMPLE_RTT_MS" 'BEGIN { exit !(rtt > limit) }'; then
    unset REMOTE_PASSWORD
    echo "SSH round trip is too high for safe clock correction: ${INITIAL_RTT_MS} ms" >&2
    echo "no clock changes were made on $REMOTE; retry when the network is idle" >&2
    exit 1
fi
printf 'initial offset (.4 - workstation): %+.2f ms (SSH round trip %.2f ms)\n' \
    "$(awk -v value="$INITIAL_OFFSET" 'BEGIN { printf "%.2f", value * 1000.0 }')" \
    "$INITIAL_RTT_MS"
echo "workstation time at sync measurement: $(date --iso-8601=ns)"

# SSH reads its login password from fd 4. sudo consumes the first line of the
# remote command's stdin; bash receives the following heredoc. No pseudo-TTY
# is allocated, so the remote ROS menu cannot run.
if ! {
    printf '%s\n' "$REMOTE_PASSWORD"
    cat <<'REMOTE_SCRIPT'
set -Eeuo pipefail
LOCAL_NTP_IP="$1"
INITIAL_OFFSET="$2"

# Stop the remote time client before the manual correction.  Otherwise
# systemd-timesyncd/chrony can apply its previously measured offset after the
# `date -s` call, making an accurate correction look wrong in the final check.
if command -v chronyd >/dev/null && [[ -f /etc/chrony/chrony.conf ]]; then
    REMOTE_TIME_SERVICE=chrony
    systemctl stop chrony 2>/dev/null || systemctl stop chronyd 2>/dev/null || true
elif command -v timedatectl >/dev/null && systemctl cat systemd-timesyncd.service >/dev/null 2>&1; then
    REMOTE_TIME_SERVICE=systemd-timesyncd
    timedatectl set-ntp false >/dev/null 2>&1 || true
    systemctl stop systemd-timesyncd.service >/dev/null 2>&1 || true
else
    echo "the .4 host has neither chrony nor systemd-timesyncd" >&2
    echo "the immediate correction was not attempted" >&2
    exit 3
fi

echo "remote time before correction: $(date --iso-8601=ns)"
# Apply the measured offset to the remote clock's current value.  This keeps
# the correction valid even if sudo/PAM took time between measurement and the
# start of this script.
REMOTE_NOW="$(date +%s.%N)"
TARGET_TIME="$(LC_ALL=C awk -v now="$REMOTE_NOW" -v offset="$INITIAL_OFFSET" 'BEGIN { printf "%.9f", now - offset }')"
date -s "@$TARGET_TIME" >/dev/null
echo "remote time after correction:  $(date --iso-8601=ns)"

if [[ "$REMOTE_TIME_SERVICE" == chrony ]]; then
    dropin=/etc/chrony/conf.d/tron2-workstation.conf
    mkdir -p /etc/chrony/conf.d
    stamp=$(date +%Y%m%d-%H%M%S)
    if [[ -e "$dropin" ]]; then
        cp -a "$dropin" "$dropin.bak.$stamp"
    fi
    printf '%s\n' \
        '# Managed by sync_tron2_time.sh' \
        "server $LOCAL_NTP_IP iburst prefer minpoll 3 maxpoll 4" \
        'makestep 0.1 3' \
        'rtcsync' > "$dropin"
    chronyd -p >/dev/null
    systemctl enable --now chrony 2>/dev/null || systemctl enable --now chronyd
    systemctl restart chrony 2>/dev/null || systemctl restart chronyd
    chronyc burst 4/4 || true
    sleep 3
    chronyc makestep || true
    echo "remote time service: chrony"
    chronyc sources -v || true
    chronyc tracking || true
elif [[ "$REMOTE_TIME_SERVICE" == systemd-timesyncd ]]; then
    conf=/etc/systemd/timesyncd.conf
    stamp=$(date +%Y%m%d-%H%M%S)
    if [[ -e "$conf" ]]; then
        cp -a "$conf" "$conf.bak.$stamp"
    fi
    cat > "$conf" <<EOF
[Time]
NTP=$LOCAL_NTP_IP
FallbackNTP=
RootDistanceMaxSec=5
PollIntervalMinSec=8
PollIntervalMaxSec=32
EOF
    systemctl unmask systemd-timesyncd.service >/dev/null 2>&1 || true
    systemctl enable systemd-timesyncd.service >/dev/null
    # The clock correction above used a midpoint offset measurement, so it is
    # safe to start the NTP client now.  It will keep the clock synchronized
    # during this session and at subsequent boots.
    systemctl enable --now systemd-timesyncd.service
    # Allow the first request to complete before printing status and measuring
    # the final offset below.
    sleep 2
    echo "remote time service: systemd-timesyncd"
    timedatectl timesync-status || true
else
    echo "remote time service detection failed" >&2
    exit 3
fi
REMOTE_SCRIPT
} | timeout 60s ssh -S "$SSH_SOCKET" "${SSH_OPTS[@]}" "$REMOTE" \
    "sudo -S -p '' bash --noprofile --norc -s -- '$LOCAL_NTP_IP' '$INITIAL_OFFSET'" 4< <(printf '%s\n' "$REMOTE_PASSWORD")
then
    unset REMOTE_PASSWORD
    echo "remote clock configuration failed or timed out" >&2
    exit 1
fi
echo
echo "checking the clock difference after correction..."
if ! FINAL_STATS="$(measure_remote_offset)"; then
    unset REMOTE_PASSWORD
    echo "could not read the .4 clock after correction" >&2
    exit 1
fi
IFS=' ' read -r REMOTE_OFFSET FINAL_RTT_MS <<< "$FINAL_STATS"
unset REMOTE_PASSWORD
LC_ALL=C awk -v offset="$REMOTE_OFFSET" -v rtt_ms="$FINAL_RTT_MS" 'BEGIN {
    printf "estimated offset (.4 - workstation): %+.2f ms\n", offset * 1000.0
    printf "SSH measurement round trip (best of samples): %.2f ms\n", rtt_ms
}'

echo
echo "done. .4 is configured to use $LOCAL_NTP_IP as its persistent time source."
echo "systemd-timesyncd is enabled and running; it will also start automatically at the next .4 boot."
echo "restart taccap-websocket after this correction, then start teleoperation."
