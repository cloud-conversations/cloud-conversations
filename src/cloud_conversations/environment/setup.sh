#!/bin/bash
# Installs a run's software as root, under systemd. $1 is the run's state folder.
# Reads "<name> <value>" settings from $1/settings and "<key> <value...>" items from
# $1/items. Writes running, done, or "failed <unknown|error> <key> <value>" to $1/status.
# An item that is already in place is left alone.
set -u
dir=$1
exec >>"$dir/log" 2>&1

while read -r name value; do
  case "$name" in
    user) user=$value ;;
    venv_dir) venv_dir=$value ;;
    uv_url) uv_url=$value ;;
    node_dist_url) node_dist_url=$value ;;
    docker_rpm_repo_url) docker_rpm_repo_url=$value ;;
    nvidia_toolkit_url) nvidia_toolkit_url=$value ;;
    install_lock_s) lock_s=$value ;;
  esac
done <"$dir/settings"

home=$(getent passwd "$user" | cut -d: -f6)
venv="$home/$venv_dir"
uv="$home/.local/bin/uv"
work=$(mktemp -d)
trap 'rm -rf "$work"' EXIT
case "$(uname -m)" in
  aarch64) uv_arch=aarch64; node_arch=arm64 ;;
  *) uv_arch=x86_64; node_arch=x64 ;;
esac
if command -v apt-get >/dev/null; then pm=apt; elif command -v dnf >/dev/null; then pm=dnf; else pm=yum; fi
apt_updated=
rpm_cached=

# Return 0 when done, 1 when the item does not exist, 2 on any other failure.

as_user() { runuser -u "$user" -- env HOME="$home" PATH="$home/.local/bin:/usr/local/bin:/usr/bin:/bin" "$@"; }

fetch() {
  curl -fsSL --retry 3 -o "$2" "$1"
  case $? in 0) return 0 ;; 22) return 1 ;; *) return 2 ;; esac
}

installed() {
  if [ "$pm" = apt ]; then
    dpkg-query -W -f='${Status}' "$1" 2>/dev/null | grep -q 'ok installed'
  else
    rpm -q "$1" >/dev/null 2>&1
  fi
}

install_system_package() {
  installed "$1" && return 0
  if [ "$pm" = apt ]; then
    if [ -z "$apt_updated" ]; then
      apt-get -o "DPkg::Lock::Timeout=$lock_s" update || return 2
      apt_updated=1
    fi
    apt-cache show "$1" >/dev/null 2>&1 || return 1
    DEBIAN_FRONTEND=noninteractive apt-get -o "DPkg::Lock::Timeout=$lock_s" install -y "$1" \
      || return 2
  else
    if [ -z "$rpm_cached" ]; then "$pm" -q makecache || return 2; rpm_cached=1; fi
    "$pm" -q -C info "$1" >/dev/null 2>&1 || return 1
    "$pm" install -y "$1" || return 2
  fi
}

git_present() { command -v git >/dev/null || install_system_package git; }

install_repository() {
  git_present || return 2
  dest="$home/$3"
  if [ ! -d "$dest/.git" ]; then as_user git clone "$1" "$dest" || return 2; fi
  as_user git -C "$dest" fetch --quiet origin "$2" 2>/dev/null
  as_user git -C "$dest" checkout --quiet "$2" || return 1
}

install_python_version() {
  if [ ! -x "$uv" ]; then
    name="uv-$uv_arch-unknown-linux-gnu.tar.gz"
    fetch "$uv_url/$name" "$work/$name" || return 2
    fetch "$uv_url/$name.sha256" "$work/$name.sha256" || return 2
    (cd "$work" && sha256sum -c "$name.sha256") || return 2
    tar -xzf "$work/$name" -C "$work" || return 2
    as_user mkdir -p "$home/.local/bin" || return 2
    install -o "$user" -m 755 "$work/uv-$uv_arch-unknown-linux-gnu/uv" "$uv" || return 2
  fi
  if [ -x "$venv/bin/python" ] && as_user "$venv/bin/python" -c \
      'import sys; sys.exit("%d.%d" % sys.version_info[:2] != sys.argv[1])' "$1"; then
    return 0
  fi
  as_user "$uv" venv --clear --python-preference system --python "$1" "$venv" || return 2
}

install_pip_package() {
  [ -x "$venv/bin/python" ] || return 2
  case "$1" in *git+https://*) git_present || return 2 ;; esac
  as_user "$uv" pip install --python "$venv/bin/python" "$1" || return 2
}

install_node_version() {
  if command -v node >/dev/null && [ "$(node --version | sed 's/^v//; s/\..*//')" = "$1" ]; then
    return 0
  fi
  base="$node_dist_url/latest-v$1.x"
  fetch "$base/SHASUMS256.txt" "$work/SHASUMS256.txt" || return $?
  name=$(awk -v want="-linux-$node_arch.tar.gz" 'substr($2, length($2) - length(want) + 1) == want { print $2; exit }' "$work/SHASUMS256.txt")
  [ -n "$name" ] || return 1
  fetch "$base/$name" "$work/$name" || return 2
  (cd "$work" && grep "  $name\$" SHASUMS256.txt | sha256sum -c -) || return 2
  tar -xzf "$work/$name" -C /usr/local --strip-components=1 --no-same-owner \
    --exclude CHANGELOG.md --exclude LICENSE --exclude README.md || return 2
}

install_npm_package() {
  command -v npm >/dev/null || return 2
  npm ls -g --depth=0 "$1" >/dev/null 2>&1 && return 0
  case "$1" in *git+https://*) git_present || return 2 ;; esac
  npm install -g "$1" || return 2
}

install_docker() {
  if ! command -v docker >/dev/null; then
    if [ "$pm" = apt ]; then
      install_system_package docker.io || return 2
    else
      fetch "$docker_rpm_repo_url" /etc/yum.repos.d/docker-ce.repo || return 2
      rpm_cached=
      install_system_package docker-ce || return 2
    fi
  fi
  systemctl enable --now docker || return 2
  usermod -aG docker "$user" || return 2
  [ "$1" = 1 ] || return 0
  if ! command -v nvidia-ctk >/dev/null; then
    if [ "$pm" = apt ]; then
      keyring=/usr/share/keyrings/nvidia-container-toolkit-keyring.gpg
      fetch "$nvidia_toolkit_url/gpgkey" "$work/gpgkey" || return 2
      gpg --batch --yes --dearmor -o "$keyring" "$work/gpgkey" || return 2
      fetch "$nvidia_toolkit_url/deb/nvidia-container-toolkit.list" "$work/list" || return 2
      sed "s#deb https://#deb [signed-by=$keyring] https://#" "$work/list" \
        >/etc/apt/sources.list.d/nvidia-container-toolkit.list || return 2
      apt_updated=
    else
      fetch "$nvidia_toolkit_url/rpm/nvidia-container-toolkit.repo" \
        /etc/yum.repos.d/nvidia-container-toolkit.repo || return 2
      rpm_cached=
    fi
    install_system_package nvidia-container-toolkit || return 2
  fi
  nvidia-ctk runtime configure --runtime=docker || return 2
  systemctl restart docker || return 2
}

install_container() {
  name="cc-$(printf '%s' "$1" | sha256sum | cut -c1-12)"
  [ -n "$(docker ps -q --filter "name=^$name\$" --filter status=running)" ] && return 0
  docker rm -f "$name" >/dev/null 2>&1
  docker pull "$1" || return 2
  args=(run -d --name "$name" --restart unless-stopped)
  [ "$2" = 1 ] && args+=(--gpus all)
  if [ "$3" != - ]; then
    IFS=, read -ra ports <<<"$3"
    for port in "${ports[@]}"; do args+=(-p "$port:$port"); done
  fi
  docker "${args[@]}" "$1" || return 2
}

echo running >"$dir/status"
if command -v cloud-init >/dev/null; then
  echo "== waiting for cloud-init"
  cloud-init status --wait >/dev/null
fi
while read -r key a b c <&3; do
  echo "== $key $a"
  case "$key" in
    system_package) install_system_package "$a" ;;
    repository) install_repository "$a" "$b" "$c" ;;
    python_version) install_python_version "$a" ;;
    pip_package) install_pip_package "$a" ;;
    node_version) install_node_version "$a" ;;
    npm_package) install_npm_package "$a" ;;
    docker) install_docker "$a" ;;
    container) install_container "$a" "$b" "$c" ;;
    *) false ;;
  esac
  case $? in
    0) ;;
    1) echo "failed unknown $key $a" >"$dir/status"; exit 1 ;;
    *) echo "failed error $key $a" >"$dir/status"; exit 1 ;;
  esac
done 3<"$dir/items"
echo done >"$dir/status"
