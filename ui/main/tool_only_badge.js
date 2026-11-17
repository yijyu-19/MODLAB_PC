// Readable tag for games that are tools, not full MOD managers.
// Game grid: after setting the card name, call applyToolOnlyBadge(nameEl, game).

export function applyToolOnlyBadge(nameEl, game) {
  if (!nameEl || !game || !game.tool_only) return;
  const suffix = "（仅工具）";
  const text = nameEl.textContent || "";
  if (!text.includes(suffix)) nameEl.textContent = text + suffix;
}
