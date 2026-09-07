#!/usr/bin/env bash

# Shared with space.py; mkdir is portable to macOS without Python or flock.
# Never steal a stale lock: another user can inspect it and recover explicitly.
SPACE_LOCK_HELD=0
SPACE_STATE_DIR=""

acquire_space_lock() {
  local directory
  local attempts=0
  [ "$SPACE_LOCK_HELD" -eq 0 ] || return 0
  SPACE_STATE_DIR="${HOST_HOME:-$HOME}/.local/state/dclaude/space"
  for directory in "${HOST_HOME:-$HOME}/.local" "${HOST_HOME:-$HOME}/.local/state" \
    "${HOST_HOME:-$HOME}/.local/state/dclaude" "$SPACE_STATE_DIR"; do
    [ ! -L "$directory" ] || die "Docker space state must not be a symlink: $directory"
  done
  (umask 077; mkdir -p "$SPACE_STATE_DIR") || die "cannot create host Docker space state at $SPACE_STATE_DIR"
  chmod 700 "$SPACE_STATE_DIR" || die "cannot secure host Docker space state at $SPACE_STATE_DIR"
  while ! (umask 077; mkdir "$SPACE_STATE_DIR/operation.lock") 2>/dev/null; do
    if [ "$attempts" -ge 30 ]; then
      die "Docker space operation is busy: $SPACE_STATE_DIR/operation.lock (owner PID in owner); retry after the operation finishes. Stale locks require manual inspection."
    fi
    attempts=$((attempts + 1))
    sleep 1
  done
  if ! printf '%s\n' "$$" > "$SPACE_STATE_DIR/operation.lock/owner"; then
    rmdir "$SPACE_STATE_DIR/operation.lock" 2>/dev/null || true
    die "cannot record Docker space lock owner"
  fi
  SPACE_LOCK_HELD=1
  trap 'release_space_lock' EXIT
  trap 'exit 130' INT
  trap 'exit 143' TERM
}

release_space_lock() {
  [ "$SPACE_LOCK_HELD" -eq 1 ] || return 0
  if [ "$(cat "$SPACE_STATE_DIR/operation.lock/owner" 2>/dev/null)" = "$$" ]; then
    if ! rm -f "$SPACE_STATE_DIR/operation.lock/owner" ||
      ! rmdir "$SPACE_STATE_DIR/operation.lock" 2>/dev/null; then
      echo "warning: could not release Docker space lock at $SPACE_STATE_DIR/operation.lock; inspect it before the next storage operation" >&2
    fi
  fi
  SPACE_LOCK_HELD=0
}
