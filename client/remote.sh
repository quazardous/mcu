#!/usr/bin/env bash
# SPDX-License-Identifier: MIT
# run a job on an mcu box and fetch its results; the branch is pushed
# first, since the box checks branches out from the project's repositories.
#
# usage, Benchfile projects:
#        remote.sh <job> [key=value...] [-o <results dir>]
#          the branch pushed is the value of branch=, when there is one
#        repository mode (a repo and a manifest in it):
#        remote.sh <branch> <job> [<results dir>]
#        remote.sh --check      is the box there, and what can it do now?
#        remote.sh --benchfile [<file>]   upload the Benchfile (default:
#          tools/bench/Benchfile) as the current git branch's; on main or
#          master, as the default one. A job run with branch=<that branch>
#          then uses it
#   MCU_HOST (BENCH_HOST, for one release): the box's project user, user@host; read from .env.local next
#     to this script (see .env.example) unless set
#   MCU_KEY (BENCH_KEY): the project's private key, anywhere (mode 600); ssh's default
#     keys when unset
#   MCU_MODE (BENCH_MODE): benchfile | repo; asked from the box (config show) when unset
#   MCU_NO_PUSH=1 (BENCH_NO_PUSH): the branch is already pushed
#
# Exit 75 (EX_TEMPFAIL) means the box could not serve the job: unreachable,
# in maintenance, its GPU absent, or restarted under the job. Retry later or
# run locally; it is never a test failure. Exit 78 (EX_CONFIG): the job asks
# for something the box cannot give; fix the Benchfile, retrying cannot help.

set -uo pipefail

here="$(cd "$(dirname "$0")" && pwd)"
# shellcheck source=/dev/null
[ -z "${MCU_HOST:-}${BENCH_HOST:-}" ] && [ -f "$here/.env.local" ] && . "$here/.env.local"
# the older BENCH_* names, read for one release
MCU_HOST="${MCU_HOST:-${BENCH_HOST:-}}" MCU_KEY="${MCU_KEY:-${BENCH_KEY:-}}"
MCU_MODE="${MCU_MODE:-${BENCH_MODE:-}}" MCU_NO_PUSH="${MCU_NO_PUSH:-${BENCH_NO_PUSH:-}}"
host="${MCU_HOST:?set MCU_HOST or write $here/.env.local (see .env.example)}"
ssh_opts=(-o ConnectTimeout=10 -o BatchMode=yes)
if [ -n "${MCU_KEY:-}" ]; then
  [ -r "$MCU_KEY" ] || { echo "remote.sh: cannot read MCU_KEY=$MCU_KEY" >&2; exit 2; }
  ssh_opts+=(-i "$MCU_KEY" -o IdentitiesOnly=yes)
fi

unreachable() {
  echo "remote.sh: mcu box unreachable ($host): retry later or run locally" >&2
  exit 75
}

if [ "${1:-}" = --check ]; then
  out="$(ssh "${ssh_opts[@]}" -o ConnectTimeout=5 "$host" box)"
  rc=$?
  [ $rc -eq 255 ] && unreachable
  echo "$out"
  # ok: 0; degraded or maintenance: 75, with the details printed
  head -1 <<< "$out" | grep -qx ok && exit 0 || exit 75
fi

if [ "${1:-}" = --benchfile ]; then
  file="${2:-tools/bench/Benchfile}"
  [ -r "$file" ] || { echo "remote.sh: cannot read $file" >&2; exit 2; }
  current="$(git rev-parse --abbrev-ref HEAD 2>/dev/null)"
  case "$current" in
    ""|HEAD|main|master) label=() ;;
    *) label=("$current") ;;
  esac
  ssh "${ssh_opts[@]}" "$host" config benchfile "${label[@]}" < "$file"
  rc=$?
  [ $rc -eq 255 ] && unreachable
  exit $rc
fi

mode="${MCU_MODE:-}"
if [ -z "$mode" ]; then
  conf="$(ssh "${ssh_opts[@]}" "$host" config show)"
  [ $? -eq 255 ] && unreachable
  if grep -q '"benchfile": [1-9]' <<< "$conf"; then mode=benchfile; else mode=repo; fi
fi

results="" branch=""
if [ "$mode" = benchfile ]; then
  usage="usage: remote.sh <job> [key=value...] [-o <results dir>]"
  job="${1:?$usage}"
  shift
  params=()
  while [ $# -gt 0 ]; do
    case "$1" in
      -o) results="${2:?$usage}"; shift 2 ;;
      *=*) params+=("$1"); [ "${1%%=*}" = branch ] && branch="${1#*=}"; shift ;;
      *) echo "remote.sh: unexpected argument $1; $usage" >&2; exit 2 ;;
    esac
  done
  request=(run "$job" "${params[@]}")
else
  usage="usage: remote.sh <branch> <job> [<results dir>]"
  branch="${1:?$usage}"
  job="${2:?$usage}"
  results="${3:-}"
  request=(run "$branch" "$job")
fi

if [ -n "$branch" ] && [ -z "${MCU_NO_PUSH:-}" ]; then
  git -c credential.helper= -c credential.helper='!gh auth git-credential' \
    push -q origin "$branch" || { echo "remote.sh: push of $branch failed (MCU_NO_PUSH=1 if it lives elsewhere)" >&2; exit 1; }
fi

log="$(mktemp)"
ssh "${ssh_opts[@]}" -o ServerAliveInterval=60 "$host" "${request[@]}" | tee "$log"
status=${PIPESTATUS[0]}
id="$(sed -n 's/^mcu-job: job \([0-9a-f-]*\) .*/\1/p' "$log" | head -1)"
rm -f "$log"
# 255: ssh itself failed (box off, network, lost mid-job): the job's state
# is unknown here, and `wait <id>` reattaches once the box is back
if [ "$status" -eq 255 ]; then
  [ -n "$id" ] && echo "remote.sh: lost the box during job $id; later: ssh $host wait $id" >&2
  unreachable
fi

if [ -n "$results" ] && [ -n "$id" ]; then
  mkdir -p "$results"
  ssh "${ssh_opts[@]}" "$host" fetch "$id" | tar -x -C "$results"
  echo "remote.sh: results of $id in $results"
fi
exit "$status"
