#!/bin/bash
set -euo pipefail
# Run on the dedicated rootful Docker HOST, never in a container.
# The delegated stage tree and outer container have distinct, explicitly bounded accounting.
if [[ $EUID -ne 0 ]]; then echo 'Run this host provisioning script as root' >&2; exit 1; fi
unit=checker-delegation.service
if [[ ${1:-} == --remove ]]; then
  systemctl stop "$unit"
  systemctl reset-failed "$unit" 2>/dev/null || true
  exit 0
fi
slots=${1:-4}
memory_mib=${2:-256}
pids=${3:-64}
if [[ ! $slots =~ ^[1-9][0-9]*$ || ! $memory_mib =~ ^[1-9][0-9]*$ || ! $pids =~ ^[1-9][0-9]*$ ]]; then
  echo 'Usage: provision_runner_host.sh [N [stage-memory-MiB [stage-pids]]] | --remove' >&2; exit 1
fi
[[ $(stat -fc %T /sys/fs/cgroup) == cgroup2fs ]]
[[ -r /sys/module/apparmor/parameters/enabled ]]
IFS= read -r apparmor_enabled < /sys/module/apparmor/parameters/enabled
[[ $apparmor_enabled == Y ]]
security=$(docker info --format '{{json .SecurityOptions}}')
[[ $security != *rootless* && $security == *apparmor* && $security == *seccomp* ]]
base=$(dirname "$(readlink -f "$0")")
apparmor_parser -r "$base/security/runner-apparmor.profile"
# A long-lived host anchor keeps the delegated cgroup owned and managed by systemd.
# Stage aggregate = N*limit; the parent also bounds 64MiB/16PIDs of anchor overhead.
if systemctl is-active --quiet "$unit"; then
  echo 'Delegation already active. Stop Compose, remove delegation, then provision again.' >&2; exit 1
fi
systemd-run --quiet --unit="$unit" --service-type=exec \
  --property='Delegate=cpu memory pids' --property="MemoryMax=$((slots * memory_mib + 64))M" \
  --property=MemorySwapMax=0 --property="TasksMax=$((slots * pids + 16))" \
  --property="CPUQuota=$((slots * 100))%" --property=Restart=no /usr/bin/sleep infinity
group=$(systemctl show "$unit" --property=ControlGroup --value)
[[ $group == /* && $group != / ]]
root=/sys/fs/cgroup$group
mkdir "$root/anchor"
while IFS= read -r pid; do printf '%s\n' "$pid" > "$root/anchor/cgroup.procs"; done < "$root/cgroup.procs"
printf '+cpu +memory +pids\n' > "$root/cgroup.subtree_control"
mkdir "$root/stages"
printf '%s\n' "$((slots * memory_mib * 1048576))" > "$root/stages/memory.max"
printf '0\n' > "$root/stages/memory.swap.max"
printf '1\n' > "$root/stages/memory.oom.group"
printf '%s\n' "$((slots * pids))" > "$root/stages/pids.max"
printf '%s 100000\n' "$((slots * 100000))" > "$root/stages/cpu.max"
printf '+cpu +memory +pids\n' > "$root/stages/cgroup.subtree_control"
[[ -w $root/stages/cgroup.kill ]]
# Mount ONLY this subtree into runner, not the writable cgroup mount or the anchor.
printf 'CHECKER_CGROUP_HOST=%s/stages\n' "$root"
