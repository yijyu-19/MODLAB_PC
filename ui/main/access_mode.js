// Single client view of the access mode returned by get_user_profile /
// guest_install. Guest quota UI runs only when the mode is "guest".
// "open" is the shipping no-login full-permission state.

export function accessModeOf(info) {
  const source = info && info.guest_install ? info.guest_install : info;
  const raw = source && source.access_mode;
  if (raw === "open" || raw === "full" || raw === "guest") return raw;
  if (source && source.login_enabled === false) return "open";
  if (source && source.is_guest) return "guest";
  return "full";
}

export function guestQuotaActive(info) {
  return accessModeOf(info) === "guest";
}
