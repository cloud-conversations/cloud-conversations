# Runs after probe.sh for a rubric's software checks.
# Reads "<kind> <target>" lines, "-" for no target, and prints "<kind> <target>=<value>".
venv="$HOME/{venv_dir}"
while read -r kind target <&3; do
  case "$kind" in
    package_installed)
      if command -v dpkg-query >/dev/null 2>&1; then
        dpkg-query -W -f='${Status}' "$target" 2>/dev/null | grep -q 'ok installed' && v=true || v=false
      else
        rpm -q "$target" >/dev/null 2>&1 && v=true || v=false
      fi ;;
    python_version)
      v=$("$venv/bin/python" -c 'import platform; print(platform.python_version())' 2>/dev/null) ;;
    pip_package)
      v=$("$venv/bin/python" -c 'import importlib.metadata as m, sys; print(m.version(sys.argv[1]))' "$target" 2>/dev/null) ;;
    node_version)
      v=$(node --version 2>/dev/null); v=${v#v} ;;
    npm_package)
      v=$(node -e 'const p = require("path"); console.log(require(p.join(process.argv[1], process.argv[2], "package.json")).version)' "$(npm root -g 2>/dev/null)" "$target" 2>/dev/null) ;;
    docker_running)
      systemctl is-active --quiet docker && v=true || v=false ;;
    docker_gpu)
      sudo -n docker info --format '{{json .Runtimes}}' 2>/dev/null | grep -q nvidia && v=true || v=false ;;
    container_running)
      [ -n "$(sudo -n docker ps -q --filter "ancestor=$target" --filter status=running 2>/dev/null)" ] && v=true || v=false ;;
    port_listening)
      [ -n "$(ss -Hltn "sport = :$target" 2>/dev/null)" ] && v=true || v=false ;;
    git_head)
      v=$(git -C "$HOME/$target" rev-parse HEAD 2>/dev/null) ;;
    *)
      continue ;;
  esac
  v=$(printf '%s' "$v" | head -n 1)
  if [ "$target" = "-" ]; then echo "$kind=$v"; else echo "$kind $target=$v"; fi
done
