#!/usr/bin/env bash

set -euo pipefail

die() {
  echo "error: $*" >&2
  exit 1
}

read_version() {
  local version_file="${TOOL_HOME:?TOOL_HOME must be set by the launcher}/docs/VERSION"
  local version

  [ -f "$version_file" ] || die "missing VERSION file at $version_file"

  version="$(tr -d '[:space:]' < "$version_file")"
  [[ "$version" =~ ^[0-9]+\.[0-9]+\.[0-9]+$ ]] || die "invalid VERSION value: $version"

  printf '%s\n' "$version"
}

DCLAUDE_VERSION="${DCLAUDE_VERSION:-$(read_version)}"
DCLAUDE_IMAGE_NAME="${DCLAUDE_IMAGE_NAME:-dclaude:${DCLAUDE_VERSION}}"
DEFAULT_HOME_MOUNTS=()
TILDE_HOME='~'
CONFIGURED_HOME_MOUNTS=("${DEFAULT_HOME_MOUNTS[@]}")
ACTIVE_MOUNT_CONFIG=""
RESET_WARM_CONTAINER=0
STOP_WARM_CONTAINER=0
UPDATE_TOOL=0
ASSUME_YES=0
AGENT_PROFILE=""
LIST_PROFILES=0
WARM_CONTAINER_NAME=""
WARM_CONTAINER_SPEC_HASH=""
WARM_CONTAINER_STATUS=""
WARM_CONTAINER_SPEC_VERSION=4

usage() {
  local tool="$1"
  cat <<EOF
usage: $tool [--rebuild] [--reset] [--stop] [--update-tool] [--yes] [--ssh] [--] [tool args...]

Options:
  --rebuild  rebuild the Docker image and recreate the warm container
  --reset    recreate the warm container before launching
  --stop     stop and remove the warm container for the current repo
  --update-tool  update the pinned upstream CLI version for this wrapper and rebuild the image
  --yes      skip the confirmation prompt for --update-tool
  --ssh      forward the host SSH agent socket and known_hosts when available
  --profile NAME  use a named Codex profile (separate ~/.codex-NAME directory)
  --list-profiles  list available Codex profiles
  --help     show this wrapper help
  --version  show the wrapper version

Examples:
  $tool
  $tool -- --help
  $tool --reset
  $tool --stop
  $tool --update-tool
  $tool --update-tool --yes
  $tool --ssh
  $tool --profile magi
  $tool --list-profiles
EOF
}

print_version() {
  local tool="$1"
  printf '%s %s\n' "$tool" "$DCLAUDE_VERSION"
}

ensure_command() {
  command -v "$1" >/dev/null 2>&1 || die "missing required command: $1"
}

ensure_docker() {
  ensure_command docker
  docker info >/dev/null 2>&1 || die "docker daemon is not available"
}

trim_config_line() {
  local line="$1"

  line="${line%%#*}"
  line="${line#"${line%%[![:space:]]*}"}"
  line="${line%"${line##*[![:space:]]}"}"

  printf '%s\n' "$line"
}

strip_wrapping_quotes() {
  local value="$1"

  if [ "${#value}" -ge 2 ]; then
    case "$value" in
      \"*\")
        value="${value:1:${#value}-2}"
        ;;
      \'*\')
        value="${value:1:${#value}-2}"
        ;;
    esac
  fi

  printf '%s\n' "$value"
}

path_overlaps() {
  local candidate="$1"
  local sensitive_root="$2"

  if [ "$candidate" = "$sensitive_root" ]; then
    return 0
  fi

  case "$candidate/" in
    "$sensitive_root"/*)
      return 0
      ;;
  esac

  case "$sensitive_root/" in
    "$candidate"/*)
      return 0
      ;;
  esac

  return 1
}

reject_sensitive_mount_overlap() {
  local mount_path="$1"
  local raw_mount_path="$2"
  local config_path="$3"

  if path_overlaps "$mount_path" "$HOST_HOME/.ssh"; then
    die "mount entry $raw_mount_path overlaps \$HOME/.ssh in $config_path; SSH files are only available through --ssh"
  fi

  if path_overlaps "$mount_path" "/run/host-services"; then
    die "mount entry $raw_mount_path overlaps /run/host-services in $config_path; SSH agent forwarding is only available through --ssh"
  fi
}

find_mount_config() {
  local config_path="$TOOL_HOME/scripts/dclaude.yaml"

  if [ -f "$config_path" ]; then
    printf '%s\n' "$config_path"
    return 0
  fi

  return 1
}

hash_string() {
  local value="$1"

  if command -v sha256sum >/dev/null 2>&1; then
    printf '%s' "$value" | sha256sum | awk '{print $1}'
  elif command -v shasum >/dev/null 2>&1; then
    printf '%s' "$value" | shasum -a 256 | awk '{print $1}'
  elif command -v openssl >/dev/null 2>&1; then
    printf '%s' "$value" | openssl dgst -sha256 -r | awk '{print $1}'
  else
    printf '%s' "$value" | cksum | awk '{print $1}'
  fi
}

hash_file() {
  local path="$1"

  [ -f "$path" ] || die "missing required file for hashing: $path"

  if command -v sha256sum >/dev/null 2>&1; then
    sha256sum "$path" | awk '{print $1}'
  elif command -v shasum >/dev/null 2>&1; then
    shasum -a 256 "$path" | awk '{print $1}'
  elif command -v openssl >/dev/null 2>&1; then
    openssl dgst -sha256 -r "$path" | awk '{print $1}'
  else
    cksum "$path" | awk '{print $1}'
  fi
}

image_identity() {
  local image_id

  image_id="$(docker image inspect --format '{{.Id}}' "$DCLAUDE_IMAGE_NAME" 2>/dev/null || true)"
  [ -n "$image_id" ] || die "failed to inspect image id for $DCLAUDE_IMAGE_NAME"

  printf '%s\n' "$image_id"
}

sanitize_name_component() {
  local value="${1:-repo}"

  value="$(printf '%s' "$value" | tr '[:upper:]' '[:lower:]')"
  value="$(printf '%s' "$value" | tr -cs 'a-z0-9' '-')"
  value="${value#-}"
  value="${value%-}"

  [ -n "$value" ] || value="repo"

  printf '%s\n' "$value"
}

validate_profile_name() {
  local name="$1"
  [[ "$name" =~ ^[a-zA-Z0-9]([a-zA-Z0-9_-]*[a-zA-Z0-9])?$ ]] || die "invalid profile name: $name (must start and end with alphanumeric; only alphanumeric, hyphens, underscores allowed)"
}

codex_home_dir() {
  if [ -n "$AGENT_PROFILE" ]; then
    printf '%s\n' "$HOST_HOME/.codex-$AGENT_PROFILE"
  else
    printf '%s\n' "$HOST_HOME/.codex"
  fi
}

list_profiles() {
  local dir dir_name profile_name

  printf '  (default)\t%s/.codex\n' "$HOST_HOME"
  for dir in "$HOST_HOME"/.codex-*/; do
    [ -d "$dir" ] || continue
    dir_name="$(basename "$dir")"
    profile_name="${dir_name#.codex-}"
    printf '  %s\t%s\n' "$profile_name" "$dir"
  done
}

set_warm_container_name() {
  local tool="$1"
  local repo_name
  local stable_hash
  local stable_identity

  repo_name="$(sanitize_name_component "$(basename "$TARGET_REPO_ROOT")")"
  stable_identity="$(printf '%s\n' "$tool" "$TARGET_REPO_ROOT" "$HOST_HOME" "$HOST_UID" "$HOST_GID" "$AGENT_PROFILE")"
  stable_hash="$(hash_string "$stable_identity")"
  stable_hash="${stable_hash:0:12}"

  if [ -n "$AGENT_PROFILE" ]; then
    WARM_CONTAINER_NAME="dclaude-${tool}-${AGENT_PROFILE}-${repo_name}-${stable_hash}"
  else
    WARM_CONTAINER_NAME="dclaude-${tool}-${repo_name}-${stable_hash}"
  fi
}

set_warm_container_spec_hash() {
  local tool="$1"
  local container_launch_hash
  local image_id
  local known_hosts_present=0
  local spec_identity
  local -a spec_fields

  container_launch_hash="$(hash_file "$TOOL_HOME/scripts/container-launch.sh")"
  image_id="$(image_identity)"

  if [ -f "$HOST_HOME/.ssh/known_hosts" ]; then
    known_hosts_present=1
  fi

  spec_fields=(
    "$WARM_CONTAINER_SPEC_VERSION"
    "$DCLAUDE_IMAGE_NAME"
    "$image_id"
    "$TOOL_HOME"
    "$container_launch_hash"
    "$TARGET_REPO_ROOT"
    "$HOST_HOME"
    "$HOST_UID"
    "$HOST_GID"
    "$tool"
    "$AGENT_PROFILE"
    "$ENABLE_SSH"
    "$known_hosts_present"
    "${CX_BOOTSTRAP_LANGUAGES:-bash python typescript}"
  )

  if (( ${#CONFIGURED_HOME_MOUNTS[@]} > 0 )); then
    spec_fields+=("${CONFIGURED_HOME_MOUNTS[@]}")
  fi

  spec_identity="$(printf '%s\n' "${spec_fields[@]}")"

  WARM_CONTAINER_SPEC_HASH="$(hash_string "$spec_identity")"
}

warm_container_exists() {
  docker container inspect "$WARM_CONTAINER_NAME" >/dev/null 2>&1
}

remove_warm_container() {
  if warm_container_exists; then
    docker rm -f "$WARM_CONTAINER_NAME" >/dev/null
  fi
}

create_warm_container() {
  local tool="$1"

  DOCKER_ARGS=(
    run
    -d
    --name "$WARM_CONTAINER_NAME"
    --init
    --workdir "$TARGET_REPO_ROOT"
    --user "$HOST_UID:$HOST_GID"
    --cap-drop=ALL
    --security-opt no-new-privileges:true
    --label "com.dclaude.managed=true"
    --label "com.dclaude.tool=$tool"
    --label "com.dclaude.repo=$TARGET_REPO_ROOT"
    --label "com.dclaude.spec-hash=$WARM_CONTAINER_SPEC_HASH"
  )

  append_common_mounts
  append_tool_mounts "$tool"
  append_common_env
  append_tool_env "$tool"

  if [ "$ENABLE_SSH" -eq 1 ]; then
    append_ssh_mounts
  fi

  docker "${DOCKER_ARGS[@]}" \
    "$DCLAUDE_IMAGE_NAME" \
    tail -f /dev/null >/dev/null

  if ! docker exec \
    --user "$HOST_UID:$HOST_GID" \
    --workdir "$TARGET_REPO_ROOT" \
    "$WARM_CONTAINER_NAME" \
    /usr/local/bin/dclaude-container-launch \
    --bootstrap-only \
    "$tool" >/dev/null; then
    remove_warm_container
    die "failed to bootstrap warm container $WARM_CONTAINER_NAME"
  fi
}

ensure_warm_container() {
  local tool="$1"
  local current_spec_hash
  local removed_for_reset=0
  local state

  set_warm_container_name "$tool"
  set_warm_container_spec_hash "$tool"

  if [ "$RESET_WARM_CONTAINER" -eq 1 ]; then
    if warm_container_exists; then
      remove_warm_container
      removed_for_reset=1
    fi
  fi

  if ! warm_container_exists; then
    create_warm_container "$tool"
    if [ "$removed_for_reset" -eq 1 ]; then
      WARM_CONTAINER_STATUS="recreated"
    else
      WARM_CONTAINER_STATUS="created"
    fi
    return 0
  fi

  current_spec_hash="$(docker inspect --format '{{ index .Config.Labels "com.dclaude.spec-hash" }}' "$WARM_CONTAINER_NAME" 2>/dev/null || true)"
  if [ "$current_spec_hash" != "$WARM_CONTAINER_SPEC_HASH" ]; then
    remove_warm_container
    create_warm_container "$tool"
    WARM_CONTAINER_STATUS="recreated"
    return 0
  fi

  state="$(docker inspect --format '{{.State.Status}}' "$WARM_CONTAINER_NAME")"
  case "$state" in
    running)
      WARM_CONTAINER_STATUS="reused"
      ;;
    exited|created)
      docker start "$WARM_CONTAINER_NAME" >/dev/null
      WARM_CONTAINER_STATUS="started"
      ;;
    *)
      remove_warm_container
      create_warm_container "$tool"
      WARM_CONTAINER_STATUS="recreated"
      ;;
  esac
}

stop_warm_container() {
  local tool="$1"

  set_warm_container_name "$tool"

  if warm_container_exists; then
    docker rm -f "$WARM_CONTAINER_NAME" >/dev/null
    echo "Stopped warm container $WARM_CONTAINER_NAME" >&2
  else
    echo "No warm container to stop for $TARGET_REPO_ROOT" >&2
  fi
}

tool_update_selector() {
  case "$1" in
    claude)
      printf '%s\n' "claude-code"
      ;;
    codex)
      printf '%s\n' "codex"
      ;;
    *)
      die "unsupported tool update target: $1"
      ;;
  esac
}

prompt_confirm() {
  local prompt="$1"
  local reply

  if [ "$ASSUME_YES" -eq 1 ]; then
    return 0
  fi

  if ! { exec 3<> /dev/tty; } 2>/dev/null; then
    die "confirmation required; rerun with --yes"
  fi

  printf '%s [y/N] ' "$prompt" >&3
  if ! IFS= read -r reply <&3; then
    exec 3>&-
    die "aborted"
  fi
  exec 3>&-

  case "$reply" in
    y|Y|yes|YES|Yes)
      ;;
    *)
      die "aborted"
      ;;
  esac
}

load_tool_update_status() {
  local selector="$1"
  local status_json

  status_json="$(python3 "$TOOL_HOME/scripts/sync_tool_versions.py" --tool "$selector" --json || true)"
  [ -n "$status_json" ] || die "failed to resolve upstream version for $selector"

  eval "$(
    STATUS_JSON="$status_json" python3 - "$selector" <<'PY'
import json
import os
import shlex
import sys

selector = sys.argv[1]
payload = json.loads(os.environ["STATUS_JSON"])
tool = payload["tools"][selector]

print(f'TOOL_UPDATE_CURRENT={shlex.quote(tool["current"])}')
print(f'TOOL_UPDATE_LATEST={shlex.quote(tool["latest"])}')
print(f'TOOL_UPDATE_UP_TO_DATE={"1" if tool["up_to_date"] else "0"}')
PY
  )"
}

perform_tool_update() {
  local tool="$1"
  local selector
  local dirty_status=""

  ensure_command python3
  ensure_docker

  selector="$(tool_update_selector "$tool")"
  load_tool_update_status "$selector"

  if [ "$TOOL_UPDATE_UP_TO_DATE" -eq 1 ]; then
    echo "$selector is already up to date at $TOOL_UPDATE_CURRENT" >&2
    return 0
  fi

  if command -v git >/dev/null 2>&1; then
    dirty_status="$(git -C "$TOOL_HOME" status --short 2>/dev/null || true)"
  fi

  if [ -n "$dirty_status" ]; then
    echo "Launcher repo has uncommitted changes:" >&2
    printf '%s\n' "$dirty_status" >&2
  fi

  echo "$selector current=$TOOL_UPDATE_CURRENT latest=$TOOL_UPDATE_LATEST" >&2
  prompt_confirm "Update $selector in $TOOL_HOME, rewrite the pinned docs, rebuild $DCLAUDE_IMAGE_NAME, and refresh warm containers on the next launch?"

  python3 "$TOOL_HOME/scripts/sync_tool_versions.py" --tool "$selector" --update
  build_image

  echo "Updated $selector from $TOOL_UPDATE_CURRENT to $TOOL_UPDATE_LATEST" >&2
  echo "Warm containers will recreate automatically on the next launch." >&2
}

normalize_home_mount_suffix() {
  local mount_suffix="$1"
  local config_path="$2"
  local raw_mount_suffix
  local component
  local -a components

  mount_suffix="$(strip_wrapping_quotes "$mount_suffix")"
  raw_mount_suffix="$mount_suffix"

  case "$mount_suffix" in
    "$TILDE_HOME"/*)
      mount_suffix="$HOST_HOME/${mount_suffix#"~/"}"
      ;;
    "$TILDE_HOME")
      mount_suffix="$HOST_HOME"
      ;;
    /*)
      ;;
    *)
      die "mount entries must be absolute paths or start with ~/ in $config_path: $mount_suffix"
      ;;
  esac

  while [ "$mount_suffix" != "/" ] && [[ "$mount_suffix" == */ ]]; do
    mount_suffix="${mount_suffix%/}"
  done

  [ -n "$mount_suffix" ] || die "empty mount entry in $config_path"

  [ "$mount_suffix" != "/" ] || die "mount entry / is not allowed in $config_path"

  case "$mount_suffix" in
    *'//'*)
      die "mount entries must not contain // in $config_path: $raw_mount_suffix"
      ;;
  esac

  IFS='/' read -r -a components <<< "${mount_suffix#/}"
  for component in "${components[@]}"; do
    case "$component" in
      .|..)
        die "mount entries must not contain . or .. in $config_path: $raw_mount_suffix"
        ;;
    esac
  done

  if [ "$mount_suffix" = "$HOST_HOME" ]; then
    die "mount entry $raw_mount_suffix resolves to \$HOME in $config_path; mount a subdirectory instead"
  fi

  reject_sensitive_mount_overlap "$mount_suffix" "$raw_mount_suffix" "$config_path"

  printf '%s\n' "$mount_suffix"
}

home_mount_is_listed() {
  local candidate="$1"
  local existing

  if (( ${#CONFIGURED_HOME_MOUNTS[@]} == 0 )); then
    return 1
  fi

  for existing in "${CONFIGURED_HOME_MOUNTS[@]}"; do
    if [ "$existing" = "$candidate" ]; then
      return 0
    fi
  done

  return 1
}

load_configured_home_mounts() {
  local config_path
  local line
  local trimmed
  local mount_suffix
  local default_mount
  local saw_mounts_section=0

  CONFIGURED_HOME_MOUNTS=()
  for default_mount in "${DEFAULT_HOME_MOUNTS[@]}"; do
    CONFIGURED_HOME_MOUNTS+=("$(normalize_home_mount_suffix "$default_mount" "built-in defaults")")
  done
  ACTIVE_MOUNT_CONFIG=""

  config_path="$(find_mount_config || true)"
  [ -n "$config_path" ] || return 0

  ACTIVE_MOUNT_CONFIG="$config_path"
  CONFIGURED_HOME_MOUNTS=()

  # shellcheck disable=SC2094
  while IFS= read -r line || [ -n "$line" ]; do
    trimmed="$(trim_config_line "$line")"
    [ -n "$trimmed" ] || continue

    case "$trimmed" in
      "read_only_mounts:")
        [ "$saw_mounts_section" -eq 0 ] || die "duplicate read_only_mounts section in $config_path"
        saw_mounts_section=1
        ;;
      "read_only_mounts: []")
        [ "$saw_mounts_section" -eq 0 ] || die "duplicate read_only_mounts section in $config_path"
        saw_mounts_section=1
        return 0
        ;;
      -\ *)
        [ "$saw_mounts_section" -eq 1 ] || die "mount list entry found before read_only_mounts section in $config_path"
        mount_suffix="${trimmed#-}"
        mount_suffix="${mount_suffix#"${mount_suffix%%[![:space:]]*}"}"
        mount_suffix="$(normalize_home_mount_suffix "$mount_suffix" "$config_path")"
        home_mount_is_listed "$mount_suffix" && die "duplicate mount entry in $config_path: $mount_suffix"
        CONFIGURED_HOME_MOUNTS+=("$mount_suffix")
        ;;
      *)
        die "unsupported config line in $config_path: $trimmed"
        ;;
    esac
  done < "$config_path"

  [ "$saw_mounts_section" -eq 1 ] || die "missing read_only_mounts section in $config_path"
}

ensure_required_paths() {
  local mount_path

  if (( ${#CONFIGURED_HOME_MOUNTS[@]} > 0 )); then
    for mount_path in "${CONFIGURED_HOME_MOUNTS[@]}"; do
      [ -d "$mount_path" ] || die "expected configured mount $mount_path to exist"
    done
  fi
}

ensure_host_state() {
  mkdir -p \
    "$HOST_HOME/.cache/dclaude/cx" \
    "$HOST_HOME/.cache/pip" \
    "$HOST_HOME/.cache/uv" \
    "$HOST_HOME/.config"

  case "$1" in
    claude)
      mkdir -p "$HOST_HOME/.claude" "$HOST_HOME/.config/claude-code"
      touch "$HOST_HOME/.claude.json"
      ;;
    codex)
      local codex_home
      codex_home="$(codex_home_dir)"
      mkdir -p "$codex_home" "$codex_home/skills"
      if [ -n "$AGENT_PROFILE" ]; then
        # Seed new profiles from the default profile's config (~/.codex).
        # Only specific files are copied, and only when they don't already exist
        # in the profile dir, to avoid clobbering profile-specific customizations.
        local default_codex="$HOST_HOME/.codex"
        if [ -f "$default_codex/AGENTS.md" ] && [ ! -f "$codex_home/AGENTS.md" ]; then
          cp "$default_codex/AGENTS.md" "$codex_home/AGENTS.md"
        fi
        if [ -d "$default_codex/skills/dclaude-cx-navigation" ] && [ ! -d "$codex_home/skills/dclaude-cx-navigation" ]; then
          cp -R "$default_codex/skills/dclaude-cx-navigation" "$codex_home/skills/dclaude-cx-navigation"
        fi
      fi
      ;;
    *)
      die "unsupported tool: $1"
      ;;
  esac
}

image_exists() {
  docker image inspect "$DCLAUDE_IMAGE_NAME" >/dev/null 2>&1
}

build_image() {
  echo "Building $DCLAUDE_IMAGE_NAME from $TOOL_HOME" >&2
  docker build -t "$DCLAUDE_IMAGE_NAME" "$TOOL_HOME"
}

ensure_target_repo() {
  local repo_root

  repo_root="$(git rev-parse --show-toplevel 2>/dev/null)" || {
    die "$WRAPPER_NAME must be run from inside the target git repository"
  }

  TARGET_REPO_ROOT="$(cd "$repo_root" && pwd -P)"
  TARGET_CWD="$(pwd -P)"
}

parse_wrapper_args() {
  ENABLE_SSH=0
  REBUILD_IMAGE=0
  RESET_WARM_CONTAINER=0
  STOP_WARM_CONTAINER=0
  UPDATE_TOOL=0
  ASSUME_YES=0
  TOOL_ARGS=()

  while [ "$#" -gt 0 ]; do
    case "$1" in
      --ssh)
        ENABLE_SSH=1
        ;;
      --rebuild)
        REBUILD_IMAGE=1
        ;;
      --reset)
        RESET_WARM_CONTAINER=1
        ;;
      --stop)
        STOP_WARM_CONTAINER=1
        ;;
      --update-tool)
        UPDATE_TOOL=1
        ;;
      --yes)
        ASSUME_YES=1
        ;;
      --profile)
        shift
        AGENT_PROFILE="${1:-}"
        [ -n "$AGENT_PROFILE" ] || die "--profile requires a name"
        validate_profile_name "$AGENT_PROFILE"
        ;;
      --list-profiles)
        LIST_PROFILES=1
        ;;
      --help|-h)
        usage "$WRAPPER_NAME"
        exit 0
        ;;
      --version|-v)
        print_version "$WRAPPER_NAME"
        exit 0
        ;;
      --)
        shift
        TOOL_ARGS=("$@")
        return 0
        ;;
      *)
        TOOL_ARGS+=("$1")
        ;;
    esac
    shift
  done
}

validate_wrapper_args() {
  if [ "$ASSUME_YES" -eq 1 ] && [ "$UPDATE_TOOL" -eq 0 ]; then
    die "--yes is only supported with --update-tool"
  fi

  if [ "$UPDATE_TOOL" -eq 1 ] && [ "$STOP_WARM_CONTAINER" -eq 1 ]; then
    die "--update-tool cannot be combined with --stop"
  fi

  if [ "$LIST_PROFILES" -eq 1 ] && { [ "$UPDATE_TOOL" -eq 1 ] || [ "$STOP_WARM_CONTAINER" -eq 1 ] || [ "$REBUILD_IMAGE" -eq 1 ]; }; then
    die "--list-profiles cannot be combined with --update-tool, --stop, or --rebuild"
  fi

  if [ "$LIST_PROFILES" -eq 1 ] && [ -n "$AGENT_PROFILE" ]; then
    die "--list-profiles cannot be combined with --profile"
  fi
}

append_common_mounts() {
  local mount_path

  DOCKER_ARGS+=(
    --mount "type=bind,src=$TARGET_REPO_ROOT,dst=$TARGET_REPO_ROOT"
    --mount "type=bind,src=$TOOL_HOME/scripts/container-launch.sh,dst=/usr/local/bin/dclaude-container-launch,readonly"
    --mount "type=bind,src=$HOST_HOME/.cache/dclaude/cx,dst=$HOST_HOME/.cache/cx"
    --mount "type=bind,src=$HOST_HOME/.cache/pip,dst=$HOST_HOME/.cache/pip"
    --mount "type=bind,src=$HOST_HOME/.cache/uv,dst=$HOST_HOME/.cache/uv"
  )

  if (( ${#CONFIGURED_HOME_MOUNTS[@]} > 0 )); then
    for mount_path in "${CONFIGURED_HOME_MOUNTS[@]}"; do
      DOCKER_ARGS+=(
        --mount "type=bind,src=$mount_path,dst=$mount_path,readonly"
      )
    done
  fi
}

append_tool_mounts() {
  case "$1" in
    claude)
      DOCKER_ARGS+=(
        --mount "type=bind,src=$HOST_HOME/.claude,dst=$HOST_HOME/.claude"
        --mount "type=bind,src=$HOST_HOME/.claude.json,dst=$HOST_HOME/.claude.json"
        --mount "type=bind,src=$HOST_HOME/.config/claude-code,dst=$HOST_HOME/.config/claude-code"
      )
      ;;
    codex)
      local codex_home
      codex_home="$(codex_home_dir)"
      DOCKER_ARGS+=(
        --mount "type=bind,src=$codex_home,dst=$codex_home"
      )
      ;;
    *)
      die "unsupported tool: $1"
      ;;
  esac
}

append_ssh_mounts() {
  local ssh_socket="/run/host-services/ssh-auth.sock"
  local known_hosts="$HOST_HOME/.ssh/known_hosts"

  [ -S "$ssh_socket" ] || die "SSH forwarding requested but $ssh_socket is not available"

  DOCKER_ARGS+=(
    --mount "type=bind,src=$ssh_socket,dst=$ssh_socket"
    -e "SSH_AUTH_SOCK=$ssh_socket"
  )

  if [ -f "$known_hosts" ]; then
    mkdir -p "$HOST_HOME/.ssh"
    DOCKER_ARGS+=(
      --mount "type=bind,src=$known_hosts,dst=$known_hosts,readonly"
    )
  fi
}

append_common_env() {
  DOCKER_ARGS+=(
    -e "CX_BOOTSTRAP_LANGUAGES=${CX_BOOTSTRAP_LANGUAGES:-bash python typescript}"
    -e "HOME=$HOST_HOME"
    -e "PIP_CACHE_DIR=$HOST_HOME/.cache/pip"
    -e "UV_CACHE_DIR=$HOST_HOME/.cache/uv"
    -e "XDG_CACHE_HOME=$HOST_HOME/.cache"
    -e "PROJECT_ROOT=$TARGET_REPO_ROOT"
    -e "TARGET_REPO_ROOT=$TARGET_REPO_ROOT"
    -e "TERM=${TERM:-xterm-256color}"
    -e "COLORTERM=${COLORTERM:-truecolor}"
  )
}

append_tool_env() {
  case "$1" in
    claude)
      DOCKER_ARGS+=(-e "DISABLE_AUTOUPDATER=1")
      ;;
    codex)
      local codex_home
      codex_home="$(codex_home_dir)"
      DOCKER_ARGS+=(-e "CODEX_HOME=$codex_home")
      ;;
    *)
      die "unsupported tool: $1"
      ;;
  esac
}

append_launch_command() {
  local tool="$1"
  case "$tool" in
    claude)
      LAUNCH_CMD=(claude --dangerously-skip-permissions)
      ;;
    codex)
      LAUNCH_CMD=(codex --dangerously-bypass-approvals-and-sandbox)
      ;;
    *)
      die "unsupported tool: $tool"
      ;;
  esac

  if [ "${#TOOL_ARGS[@]}" -gt 0 ]; then
    LAUNCH_CMD+=("${TOOL_ARGS[@]}")
  fi
}

emit_startup_banner() {
  local bootstrapped_this_launch=0

  if [ -n "$ACTIVE_MOUNT_CONFIG" ]; then
    echo "Using read-only configured mounts from $ACTIVE_MOUNT_CONFIG" >&2
  fi

  case "$WARM_CONTAINER_STATUS" in
    created)
      bootstrapped_this_launch=1
      echo "Created warm container $WARM_CONTAINER_NAME" >&2
      ;;
    recreated)
      bootstrapped_this_launch=1
      echo "Recreated warm container $WARM_CONTAINER_NAME" >&2
      ;;
    started)
      echo "Started warm container $WARM_CONTAINER_NAME" >&2
      ;;
    reused)
      echo "Reusing warm container $WARM_CONTAINER_NAME" >&2
      ;;
  esac

  case "$1" in
    claude)
      echo "Starting interactive Claude Code for $TARGET_CWD" >&2
      if [ "$bootstrapped_this_launch" -eq 1 ]; then
        echo "Warm container bootstrap seeded $HOST_HOME/.claude/CX.md and wired $HOST_HOME/.claude/CLAUDE.md if needed" >&2
      fi
      if [ "${#TOOL_ARGS[@]}" -eq 0 ]; then
        echo "Claude is interactive and will wait for input at its prompt." >&2
      fi
      ;;
    codex)
      local codex_home
      codex_home="$(codex_home_dir)"
      if [ -n "$AGENT_PROFILE" ]; then
        echo "Starting interactive Codex (profile: $AGENT_PROFILE) for $TARGET_CWD" >&2
      else
        echo "Starting interactive Codex for $TARGET_CWD" >&2
      fi
      if [ "$bootstrapped_this_launch" -eq 1 ]; then
        echo "Warm container bootstrap seeded $codex_home/AGENTS.md and $codex_home/skills/dclaude-cx-navigation if needed" >&2
      fi
      ;;
    *)
      die "unsupported tool: $1"
      ;;
  esac
}

launch_agent() {
  local tool="$1"
  shift

  : "${TOOL_HOME:?TOOL_HOME must be set by the launcher}"

  WRAPPER_NAME="d${tool}"
  parse_wrapper_args "$@"
  validate_wrapper_args

  TOOL_HOME="$(cd "$TOOL_HOME" && pwd -P)"
  HOST_HOME="$(cd "$HOME" && pwd -P)"
  HOST_UID="$(id -u)"
  HOST_GID="$(id -g)"

  if [ "$tool" != "codex" ]; then
    if [ -n "$AGENT_PROFILE" ]; then
      die "--profile is only supported for codex"
    fi
    if [ "$LIST_PROFILES" -eq 1 ]; then
      die "--list-profiles is only supported for codex"
    fi
  fi

  if [ "$UPDATE_TOOL" -eq 1 ]; then
    perform_tool_update "$tool"
    exit 0
  fi

  if [ "$LIST_PROFILES" -eq 1 ]; then
    list_profiles
    exit 0
  fi

  ensure_target_repo
  ensure_docker

  if [ "$STOP_WARM_CONTAINER" -eq 1 ]; then
    stop_warm_container "$tool"
    exit 0
  fi

  load_configured_home_mounts
  ensure_required_paths
  ensure_host_state "$tool"

  if [ "$REBUILD_IMAGE" -eq 1 ]; then
    build_image
    RESET_WARM_CONTAINER=1
  elif ! image_exists; then
    build_image
  fi

  ensure_warm_container "$tool"
  append_launch_command "$tool"
  emit_startup_banner "$tool"

  DOCKER_ARGS=(
    exec
    --user "$HOST_UID:$HOST_GID"
    --workdir "$TARGET_CWD"
    -e "TERM=${TERM:-xterm-256color}"
    -e "COLORTERM=${COLORTERM:-truecolor}"
  )

  if [ -t 0 ] && [ -t 1 ]; then
    DOCKER_ARGS+=(-it)
  else
    DOCKER_ARGS+=(-i)
  fi

  exec docker "${DOCKER_ARGS[@]}" \
    "$WARM_CONTAINER_NAME" \
    "${LAUNCH_CMD[@]}"
}
