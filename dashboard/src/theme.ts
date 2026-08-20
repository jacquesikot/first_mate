/* Theme selection. Three states, matching the CSS in styles.css:
   "dark" and "light" are explicit choices and set data-theme on <html>;
   "system" stores nothing and removes the attribute, letting the
   prefers-color-scheme rules decide.

   The stored choice is applied by applyStoredTheme() before React mounts, so
   the first paint is already in the right theme — no flash of dark on a
   light-mode machine. */

export type Theme = "dark" | "light" | "system";

const KEY = "fm.theme";

export function storedTheme(): Theme {
  const v = localStorage.getItem(KEY);
  return v === "dark" || v === "light" ? v : "system";
}

/** The theme actually on screen — "system" resolved against the OS. */
export function resolveTheme(t: Theme): "dark" | "light" {
  if (t !== "system") return t;
  return window.matchMedia("(prefers-color-scheme: light)").matches ? "light" : "dark";
}

export function applyTheme(t: Theme): void {
  const root = document.documentElement;
  if (t === "system") root.removeAttribute("data-theme");
  else root.setAttribute("data-theme", t);
}

export function saveTheme(t: Theme): void {
  if (t === "system") localStorage.removeItem(KEY);
  else localStorage.setItem(KEY, t);
  applyTheme(t);
}

/** Call once, before render. */
export function applyStoredTheme(): void {
  applyTheme(storedTheme());
}
