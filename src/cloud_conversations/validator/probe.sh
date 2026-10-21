# Runs on every instance Cloud Conversations boots, over SSH with its system key.
# Keep this script in the code. A copy loaded from outside the product, such as a
# separately shipped bundle, would let whoever supplies it run commands on every
# user's instance: a supply chain risk.
# Prints key=value lines, every value a bare scalar.
echo "cpu_arch=$(uname -m)"
echo "cpu_count=$(nproc)"
# Online memory in GiB, and the largest disk in GB.
mem=$(lsmem -b --summary=only 2>/dev/null | awk '/^Total online memory/ {printf "%.0f", $4}')
[ -n "$mem" ] || mem=$(awk '/^MemTotal/ {printf "%.0f", $2 * 1024}' /proc/meminfo)
echo "mem_gb=$((mem / 1073741824))"
disk=$(lsblk -b -d -n -o SIZE,TYPE 2>/dev/null | awk '$2 == "disk" && $1 > max {max = $1} END {printf "%.0f", max}')
echo "disk_gb=$((disk / 1000000000))"
echo "kernel=$(uname -r)"
echo "os_release=$(. /etc/os-release 2>/dev/null; echo "${NAME:-unknown} ${VERSION_ID:-}")"
echo "dns_resolves=$(getent hosts {dns_host} >/dev/null 2>&1 && echo true || echo false)"
gpu=$(lspci 2>/dev/null | grep -i '3d controller' | head -1)
[ -n "$gpu" ] || gpu=$(lspci 2>/dev/null | grep -i 'vga compatible controller' | head -1)
echo "gpu_model=$(printf '%s' "$gpu" | sed -e 's/.*controller: //' -e 's/ (rev [^)]*)$//')"
# VGA and 3D controllers from NVIDIA (10de) and AMD (1002).
gpus=$(lspci -n 2>/dev/null | awk '$2 ~ /^030[02]:$/ && $3 ~ /^(10de|1002):/' | wc -l)
echo "gpu_count=$((gpus))"
