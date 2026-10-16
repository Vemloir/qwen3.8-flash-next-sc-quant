#!/usr/bin/env bash
# capped.sh <name> <MemoryMax> <cmd...>: run cmd in its own user scope with a hard memory cap and no
# swap, so a runaway job is OOM-killed inside its own cgroup instead of squeezing the rest of the system
name=$1; mem=$2; shift 2
exec systemd-run --user --scope -q --unit="sc-$name-$(date +%s)" -p MemoryMax="$mem" -p MemorySwapMax=0 "$@"
