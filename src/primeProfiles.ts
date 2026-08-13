import type { PrimeProfileOption } from "./types";

export interface ProfilePickerRow {
  index: number;
  name: string;
  /** True when the label was filled in (not saved in the profile file). */
  inferred: boolean;
  kids: boolean;
}

const KIDS_NAME_RE = /^(kids?|child|children)$/i;
const KIDS_WORD_RE = /\b(kids?|child|children)\b/i;
const NEW_SLOT_RE = /^(new|add(?: profile)?|create)$/i;

export function isKidsProfileName(name: string): boolean {
  const text = name.trim();
  if (!text) return false;
  return KIDS_NAME_RE.test(text) || KIDS_WORD_RE.test(text);
}

export function isNewProfileSlotName(name: string): boolean {
  return NEW_SLOT_RE.test(name.trim());
}

/**
 * Build the Settings picker list: one row per real Prime profile slot.
 *
 * The last TV tile is "+" / "New" (add a profile). That is not a profile and
 * is never listed. Two-step adult/kid indices are not vertical slots.
 *
 * The combined list almost always puts Kids in the first gap after the primary
 * adult (usually index 1). If that hole has no saved name, label it Kids.
 */
export function buildPrimeProfilePickerRows(
  options: PrimeProfileOption[],
  currentIndex: number,
): ProfilePickerRow[] {
  const savedByIndex = new Map<number, string>();
  for (const p of options) {
    if (p.profile_type !== "none") continue;
    const name = (p.name || "").trim();
    if (!name || savedByIndex.has(p.index)) continue;
    if (isNewProfileSlotName(name) || name === "+") continue;
    savedByIndex.set(p.index, name);
  }

  const maxSaved = [...savedByIndex.keys()].reduce((m, i) => Math.max(m, i), 0);
  // Real profiles only. Do not pad out to the trailing "+ / New" tile.
  let last = Math.max(maxSaved, 2);
  if (currentIndex > last && savedByIndex.has(currentIndex)) {
    last = currentIndex;
  }

  const hasKidsName = [...savedByIndex.values()].some(isKidsProfileName);
  if (!hasKidsName && !savedByIndex.has(1)) {
    savedByIndex.set(1, "Kids");
  }

  const rows: ProfilePickerRow[] = [];
  for (let i = 0; i <= last; i++) {
    const name = savedByIndex.get(i) ?? "";
    if (isNewProfileSlotName(name) || name === "+") continue;
    const saved = options.some(
      (p) =>
        p.profile_type === "none" &&
        p.index === i &&
        (p.name || "").trim() &&
        !isNewProfileSlotName(p.name) &&
        p.name.trim() !== "+",
    );
    rows.push({
      index: i,
      name,
      inferred: Boolean(name) && !saved,
      kids: isKidsProfileName(name),
    });
  }
  return rows;
}
