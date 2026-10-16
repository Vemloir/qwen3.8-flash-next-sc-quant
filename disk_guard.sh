#!/usr/bin/env bash
# Stop the BF16 download when free space on / drops below 15 GB (hfdl resumes from its .part files)
while systemctl --user is-active -q sc-dl-bf16; do
  free=$(df --output=avail -B1G / | tail -1 | tr -d ' ')
  if [ "$free" -lt 15 ]; then
    echo "$(date '+%F %H:%M:%S') free ${free} GB, stopping sc-dl-bf16"
    systemctl --user stop sc-dl-bf16
    exit 0
  fi
  sleep 30
done
