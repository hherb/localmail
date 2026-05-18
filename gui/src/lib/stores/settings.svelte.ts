/**
 * GUI preference singleton. Persists user-tweakable UI/UX knobs to
 * localStorage so settings survive a reload (Tauri webview keeps the
 * origin's storage across restarts). Mirrors the rune-store pattern of
 * `auth.svelte.ts` / `mail.svelte.ts`: `$state` field, `snapshot` getter,
 * narrow `set*` mutators.
 *
 * `loadInitial` defensively coerces every field — the persisted blob may
 * predate a schema change, be hand-edited, or be missing keys we added
 * later, so we never trust the raw JSON shape.
 */

export type Density = "comfortable" | "compact";
export type ImagePolicy = "block" | "ask" | "allow";
export type DateFormat = "relative" | "absolute";

export interface SettingsSnapshot {
  density: Density;
  imagePolicy: ImagePolicy;
  dateFormat: DateFormat;
  pageSize: number;
  defaultLanguage: string | null;
  debug: boolean;
}

export const DEFAULTS: SettingsSnapshot = {
  density: "comfortable",
  imagePolicy: "block",
  dateFormat: "relative",
  pageSize: 50,
  defaultLanguage: null,
  debug: false,
};

const STORAGE_KEY = "localmail.gui.settings";
const MAX_PAGE_SIZE = 200;

function hasLocalStorage(): boolean {
  return typeof window !== "undefined" && typeof window.localStorage !== "undefined";
}

function coerceDensity(v: unknown): Density {
  return v === "compact" ? "compact" : "comfortable";
}

function coerceImagePolicy(v: unknown): ImagePolicy {
  if (v === "allow") return "allow";
  if (v === "ask") return "ask";
  return "block";
}

function coerceDateFormat(v: unknown): DateFormat {
  return v === "absolute" ? "absolute" : "relative";
}

function coercePageSize(v: unknown): number {
  if (typeof v !== "number" || !Number.isFinite(v) || v <= 0) return DEFAULTS.pageSize;
  return Math.min(Math.floor(v), MAX_PAGE_SIZE);
}

function coerceDefaultLanguage(v: unknown): string | null {
  if (typeof v !== "string") return null;
  const trimmed = v.trim().toLowerCase();
  return trimmed.length > 0 ? trimmed : null;
}

function loadInitial(): SettingsSnapshot {
  if (!hasLocalStorage()) return { ...DEFAULTS };
  const raw = window.localStorage.getItem(STORAGE_KEY);
  if (raw === null) return { ...DEFAULTS };
  try {
    const parsed = JSON.parse(raw) as Partial<SettingsSnapshot>;
    return {
      density: coerceDensity(parsed.density),
      imagePolicy: coerceImagePolicy(parsed.imagePolicy),
      dateFormat: coerceDateFormat(parsed.dateFormat),
      pageSize: coercePageSize(parsed.pageSize),
      defaultLanguage: coerceDefaultLanguage(parsed.defaultLanguage),
      debug: parsed.debug === true,
    };
  } catch {
    return { ...DEFAULTS };
  }
}

function persist(s: SettingsSnapshot): void {
  if (!hasLocalStorage()) return;
  try {
    window.localStorage.setItem(STORAGE_KEY, JSON.stringify(s));
  } catch {
    // quota / private-mode failures are non-fatal for in-memory state
  }
}

class SettingsStore {
  #state: SettingsSnapshot = $state(loadInitial());

  get snapshot(): SettingsSnapshot {
    return this.#state;
  }

  setDensity(d: Density): void {
    this.#state.density = d;
    persist(this.#state);
  }

  setImagePolicy(p: ImagePolicy): void {
    this.#state.imagePolicy = p;
    persist(this.#state);
  }

  setDateFormat(d: DateFormat): void {
    this.#state.dateFormat = d;
    persist(this.#state);
  }

  setPageSize(n: number): void {
    if (typeof n !== "number" || !Number.isFinite(n) || n <= 0) return;
    this.#state.pageSize = Math.min(Math.floor(n), MAX_PAGE_SIZE);
    persist(this.#state);
  }

  setDefaultLanguage(s: string | null): void {
    this.#state.defaultLanguage = coerceDefaultLanguage(s);
    persist(this.#state);
  }

  setDebug(b: boolean): void {
    this.#state.debug = b === true;
    persist(this.#state);
  }

  /** Test-only: reset to defaults and clear persisted blob. */
  resetForTest(): void {
    this.#state = { ...DEFAULTS };
    if (hasLocalStorage()) {
      try {
        window.localStorage.removeItem(STORAGE_KEY);
      } catch {
        // ignore
      }
    }
  }
}

export const settings = new SettingsStore();
export const SETTINGS_STORAGE_KEY = STORAGE_KEY;
