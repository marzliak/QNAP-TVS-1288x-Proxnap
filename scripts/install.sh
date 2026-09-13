#!/usr/bin/env bash
set -euo pipefail

usage() {
  printf '%s\n' \
    'Usage: scripts/install.sh [MODE] [OPTIONS]' \
    '' \
    'Modes:' \
    '  --preflight                 inspect prerequisites only' \
    '  --dry-run                   print the installation without changing the host' \
    '  --uninstall                 remove code/units; preserve configuration' \
    '  --purge-config              with --uninstall, also remove /etc/proxnap' \
    '' \
    'Optional services:' \
    '  --enable-disk-leds          requires configured bay mappings' \
    '  --enable-copy-button        enable USB Copy event listener' \
    '  --enable-power-button       enable guarded power-key listener and logind ignore' \
    '  --enable-panel-breathing    use breathing instead of static panel brightness' \
    '' \
    'Destructive-action opt-ins (all disabled by default):' \
    '  --allow-apt-refresh         permit apt-get update from the Copy button' \
    '  --allow-dist-upgrade        permit noninteractive dist-upgrade' \
    '  --allow-reboot              permit confirmed reboot actions' \
    '  --allow-poweroff            permit confirmed poweroff actions' \
    '' \
    'The installer never runs apt update, dist-upgrade, reboot, or poweroff.'
}

mode=install
dry_run=false
purge=false
enable_disk=false
enable_copy=false
enable_power=false
enable_breath=false
allow_refresh=false
allow_upgrade=false
allow_reboot=false
allow_poweroff=false

for arg in "$@"; do
  case "$arg" in
    --preflight) mode=preflight ;;
    --dry-run) dry_run=true ;;
    --uninstall) mode=uninstall ;;
    --purge-config) purge=true ;;
    --enable-disk-leds) enable_disk=true ;;
    --enable-copy-button) enable_copy=true ;;
    --enable-power-button) enable_power=true ;;
    --enable-panel-breathing) enable_breath=true ;;
    --allow-apt-refresh) allow_refresh=true ;;
    --allow-dist-upgrade) allow_upgrade=true ;;
    --allow-reboot) allow_reboot=true ;;
    --allow-poweroff) allow_poweroff=true ;;
    -h|--help) usage; exit 0 ;;
    *) printf 'Unknown argument: %s\n' "$arg" >&2; usage >&2; exit 2 ;;
  esac
done

if [[ "$purge" == true && "$mode" != uninstall ]]; then
  printf '%s\n' '--purge-config is valid only with --uninstall' >&2
  exit 2
fi

script_dir="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
project_dir="$(cd -- "${script_dir}/.." && pwd)"

print_cmd() {
  printf '+ '
  printf '%q ' "$@"
  printf '\n'
}

run() {
  if [[ "$dry_run" == true ]]; then
    print_cmd "$@"
  else
    "$@"
  fi
}

check_command() {
  if command -v "$1" >/dev/null 2>&1; then
    printf 'OK command: %s\n' "$1"
  else
    printf 'MISSING command: %s\n' "$1" >&2
    return 1
  fi
}

preflight() {
  local failed=0
  for command in python3 systemctl modprobe modinfo install; do
    check_command "$command" || failed=1
  done
  if PYTHONPATH="${project_dir}/src" python3 -c \
    'from proxnap.common import model_is_supported; raise SystemExit(0 if model_is_supported() else 1)'; then
    printf 'OK model: TVS-1288X\n'
  else
    printf 'UNSUPPORTED or unverified model: require TVS-1288X DMI or qnap8528 VPD Q05W0/Q05K0\n' >&2
    printf 'VPD fallback is accepted only when DMI is empty or generic; explicit unsupported DMI is refused.\n' >&2
    failed=1
  fi
  if modinfo qnap8528 >/dev/null 2>&1 || [[ -d /sys/module/qnap8528 ]]; then
    printf 'OK module: qnap8528 available\n'
  else
    printf 'MISSING module: qnap8528 must be installed before Proxnap\n' >&2
    failed=1
  fi
  if [[ -e /dev/ttyS1 ]]; then
    printf 'OK LCD port: /dev/ttyS1\n'
  else
    printf 'MISSING LCD port: /dev/ttyS1\n' >&2
    failed=1
  fi
  if [[ "$failed" -ne 0 ]]; then
    return 1
  fi
}

set_config_bool() {
  local file="$1"
  local key="$2"
  local value="$3"
  if [[ "$dry_run" == true ]]; then
    printf '+ set %q %q=%q\n' "$file" "$key" "$value"
    return
  fi
  PYTHONPATH="${project_dir}/src" python3 -c \
    'from proxnap.common import set_config_value; import sys; set_config_value(*sys.argv[1:])' \
    "$file" "$key" "$value"
}

install_config() {
  local name="$1"
  if [[ -e "/etc/proxnap/$name" ]]; then
    printf 'KEEP config: /etc/proxnap/%s\n' "$name"
  else
    run install -m 0644 "${project_dir}/configs/${name}" "/etc/proxnap/${name}"
  fi
}

uninstall_all() {
  local units=(
    proxnap-power-button.service proxnap-copy-button.service
    proxnap-disk-leds.service proxnap-panel-breath.service
    proxnap-panel-static.service proxnap-status-led.service
    proxnap-lcd.service proxnap-fan-control.service
    qnap8528-load-module.service
  )
  if [[ "$dry_run" == true ]]; then
    print_cmd systemctl disable --now "${units[@]}"
  else
    systemctl disable --now "${units[@]}" >/dev/null 2>&1 || true
    PYTHONPATH=/opt/proxnap /usr/bin/python3 -m proxnap.fan_control --failsafe >/dev/null 2>&1 || true
  fi
  for unit in "${units[@]}"; do
    run rm -f "/etc/systemd/system/${unit}"
  done
  run rm -rf /opt/proxnap
  run rm -f /etc/systemd/logind.conf.d/proxnap-power-button.conf
  if [[ "$purge" == true ]]; then
    run rm -rf /etc/proxnap
  fi
  run systemctl daemon-reload
  if [[ "$dry_run" == false ]]; then
    systemctl try-restart systemd-logind.service >/dev/null 2>&1 || true
  fi
  printf 'Uninstall complete; fan full-speed fallback was requested before code removal.\n'
}

if [[ "$mode" == preflight ]]; then
  preflight
  exit $?
fi

if [[ "$mode" == uninstall ]]; then
  if [[ ${EUID:-$(id -u)} -ne 0 && "$dry_run" == false ]]; then
    printf 'Run uninstall as root.\n' >&2
    exit 1
  fi
  uninstall_all
  exit 0
fi

if [[ "$dry_run" == true ]]; then
  preflight || printf 'DRY-RUN: prerequisite failures reported; no changes made.\n' >&2
else
  if [[ ${EUID:-$(id -u)} -ne 0 ]]; then
    printf 'Run installation as root.\n' >&2
    exit 1
  fi
  preflight
fi

has_disk_mapping() {
  local line
  while IFS= read -r line; do
    if [[ "$line" =~ ^(hdd[1-8]|ssd[1-4]|m2ssd[12])= ]]; then
      return 0
    fi
  done < "${project_dir}/configs/disk-leds.conf"
  return 1
}

if [[ "$enable_disk" == true ]] && ! has_disk_mapping; then
  printf 'Disk LED enable requested, but no slot mapping exists in configs/disk-leds.conf.\n' >&2
  exit 1
fi

run install -d -m 0755 /opt/proxnap /opt/proxnap/proxnap /etc/proxnap /etc/systemd/system
for source in "${project_dir}"/src/proxnap/*.py; do
  run install -m 0644 "$source" "/opt/proxnap/proxnap/$(basename "$source")"
done
for config in fan-control.conf lcd.conf status-led.conf panel-led.conf disk-leds.conf copy-button.conf power-button.conf; do
  install_config "$config"
done
for unit in "${project_dir}"/systemd/*.service; do
  run install -m 0644 "$unit" "/etc/systemd/system/$(basename "$unit")"
done

set_config_bool /etc/proxnap/copy-button.conf enabled "$enable_copy"
set_config_bool /etc/proxnap/copy-button.conf apt_refresh_enabled "$allow_refresh"
set_config_bool /etc/proxnap/copy-button.conf apt_upgrade_enabled "$allow_upgrade"
set_config_bool /etc/proxnap/copy-button.conf reboot_enabled "$allow_reboot"
set_config_bool /etc/proxnap/copy-button.conf poweroff_enabled "$allow_poweroff"
set_config_bool /etc/proxnap/power-button.conf enabled "$enable_power"
set_config_bool /etc/proxnap/power-button.conf reboot_enabled "$allow_reboot"
set_config_bool /etc/proxnap/power-button.conf poweroff_enabled "$allow_poweroff"

run systemctl daemon-reload
base_units=(qnap8528-load-module.service proxnap-fan-control.service proxnap-lcd.service proxnap-status-led.service)
if [[ "$enable_breath" == true ]]; then
  base_units+=(proxnap-panel-breath.service)
  run systemctl disable --now proxnap-panel-static.service
else
  base_units+=(proxnap-panel-static.service)
  run systemctl disable --now proxnap-panel-breath.service
fi
run systemctl enable --now "${base_units[@]}"

for item in \
  "${enable_disk}:proxnap-disk-leds.service" \
  "${enable_copy}:proxnap-copy-button.service"; do
  state="${item%%:*}"
  unit="${item#*:}"
  if [[ "$state" == true ]]; then
    run systemctl enable --now "$unit"
  else
    run systemctl disable --now "$unit"
  fi
done

if [[ "$enable_power" == true ]]; then
  run install -d -m 0755 /etc/systemd/logind.conf.d
  if [[ "$dry_run" == true ]]; then
    printf '+ write %q with HandlePowerKey=ignore and HandlePowerKeyLongPress=ignore\n' /etc/systemd/logind.conf.d/proxnap-power-button.conf
  else
    printf '[Login]\nHandlePowerKey=ignore\nHandlePowerKeyLongPress=ignore\n' > /etc/systemd/logind.conf.d/proxnap-power-button.conf
  fi
  run systemctl try-restart systemd-logind.service
  run systemctl enable --now proxnap-power-button.service
else
  run systemctl disable --now proxnap-power-button.service
fi

printf 'Installation complete. Destructive actions: refresh=%s upgrade=%s reboot=%s poweroff=%s\n' \
  "$allow_refresh" "$allow_upgrade" "$allow_reboot" "$allow_poweroff"
