import { beforeEach, describe, expect, it } from "vitest";

// jsdom in this project's vitest setup does not provide window.localStorage,
// so install a minimal in-memory shim on `window` (and `globalThis`, since
// the runtime reads via `window.localStorage`) before the store module is
// imported and instantiated.
function installLocalStorageShim(): Storage {
  const map = new Map<string, string>();
  const shim: Storage = {
    get length() { return map.size; },
    clear() { map.clear(); },
    getItem(key: string): string | null { return map.has(key) ? (map.get(key) as string) : null; },
    key(index: number): string | null { return Array.from(map.keys())[index] ?? null; },
    removeItem(key: string): void { map.delete(key); },
    setItem(key: string, value: string): void { map.set(key, String(value)); },
  };
  Object.defineProperty(window, "localStorage", {
    value: shim,
    configurable: true,
    writable: true,
  });
  Object.defineProperty(globalThis, "localStorage", {
    value: shim,
    configurable: true,
    writable: true,
  });
  return shim;
}

installLocalStorageShim();

const { DEFAULTS, SETTINGS_STORAGE_KEY, settings } = await import("./settings.svelte");

beforeEach(() => {
  window.localStorage.clear();
  settings.resetForTest();
});

describe("settings store", () => {
  it("starts with defaults", () => {
    expect(settings.snapshot).toEqual(DEFAULTS);
  });

  it("setDensity updates state and persists", () => {
    settings.setDensity("compact");
    expect(settings.snapshot.density).toBe("compact");
    const raw = window.localStorage.getItem(SETTINGS_STORAGE_KEY);
    expect(raw).not.toBeNull();
    expect(JSON.parse(raw as string).density).toBe("compact");
  });

  it("setImagePolicy accepts block / ask / allow", () => {
    settings.setImagePolicy("ask");
    expect(settings.snapshot.imagePolicy).toBe("ask");
    settings.setImagePolicy("allow");
    expect(settings.snapshot.imagePolicy).toBe("allow");
    settings.setImagePolicy("block");
    expect(settings.snapshot.imagePolicy).toBe("block");
  });

  it("setDateFormat toggles relative / absolute", () => {
    settings.setDateFormat("absolute");
    expect(settings.snapshot.dateFormat).toBe("absolute");
    settings.setDateFormat("relative");
    expect(settings.snapshot.dateFormat).toBe("relative");
  });

  it("clamps page size to 200 max", () => {
    settings.setPageSize(1000);
    expect(settings.snapshot.pageSize).toBe(200);
  });

  it("rejects non-positive page size and leaves prior value intact", () => {
    settings.setPageSize(75);
    settings.setPageSize(0);
    expect(settings.snapshot.pageSize).toBe(75);
    settings.setPageSize(-5);
    expect(settings.snapshot.pageSize).toBe(75);
    settings.setPageSize(Number.NaN);
    expect(settings.snapshot.pageSize).toBe(75);
  });

  it("floors fractional page size", () => {
    settings.setPageSize(42.9);
    expect(settings.snapshot.pageSize).toBe(42);
  });

  it("normalises defaultLanguage to lowercase or null", () => {
    settings.setDefaultLanguage("EN");
    expect(settings.snapshot.defaultLanguage).toBe("en");
    settings.setDefaultLanguage("  De  ");
    expect(settings.snapshot.defaultLanguage).toBe("de");
    settings.setDefaultLanguage("");
    expect(settings.snapshot.defaultLanguage).toBeNull();
    settings.setDefaultLanguage(null);
    expect(settings.snapshot.defaultLanguage).toBeNull();
  });

  it("toggles debug", () => {
    settings.setDebug(true);
    expect(settings.snapshot.debug).toBe(true);
    settings.setDebug(false);
    expect(settings.snapshot.debug).toBe(false);
  });

  it("persists all mutations to localStorage", () => {
    settings.setDensity("compact");
    settings.setImagePolicy("allow");
    settings.setDateFormat("absolute");
    settings.setPageSize(25);
    settings.setDefaultLanguage("es");
    settings.setDebug(true);
    const raw = window.localStorage.getItem(SETTINGS_STORAGE_KEY);
    expect(raw).not.toBeNull();
    const parsed = JSON.parse(raw as string);
    expect(parsed).toEqual({
      density: "compact",
      imagePolicy: "allow",
      dateFormat: "absolute",
      pageSize: 25,
      defaultLanguage: "es",
      debug: true,
    });
  });

  it("resetForTest clears persisted blob", () => {
    settings.setDensity("compact");
    expect(window.localStorage.getItem(SETTINGS_STORAGE_KEY)).not.toBeNull();
    settings.resetForTest();
    expect(window.localStorage.getItem(SETTINGS_STORAGE_KEY)).toBeNull();
    expect(settings.snapshot).toEqual(DEFAULTS);
  });
});
