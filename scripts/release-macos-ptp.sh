#!/usr/bin/env bash
set -u

# This helper never reads a password. Configure the exact NOPASSWD sudoers
# commands documented in README.md, or run it manually from an authorized shell.
for service in ptpcamerad icdd mscamerad-xpc; do
  /usr/bin/sudo -n /usr/bin/killall -TERM "$service" 2>/dev/null || true
done
