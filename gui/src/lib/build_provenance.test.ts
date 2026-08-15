import { describe, expect, it } from "vitest";
import { buildLabel, versionWarning } from "./build_provenance";

describe("buildLabel", () => {
  it("shows the hash when there is one", () => {
    expect(buildLabel("eec8e09", "git_checkout")).toBe("eec8e09");
    expect(buildLabel("eec8e09-dirty", "git_checkout")).toBe("eec8e09-dirty");
  });

  it("explains an absent hash rather than showing a bare placeholder", () => {
    expect(buildLabel(null, "not_a_repo")).toBe("— not a repository");
    expect(buildLabel(null, "git_unavailable")).toBe("— git unavailable");
    expect(buildLabel(null, "git_failed")).toBe("— could not read the repository");
  });

  it("falls back to the placeholder for a source it does not know", () => {
    // An older or newer server; the row must not render "undefined".
    expect(buildLabel(null, "something_new")).toBe("?");
    expect(buildLabel(null, null)).toBe("?");
  });

  it("does not resolve inherited Object properties as reasons", () => {
    // `"toString" in {}` is true and `{}["constructor"]` is a function, so an
    // `in` test or a bare index would render `function toString() { … }` into
    // the row — the "must not render undefined" contract, failing louder.
    for (const key of ["toString", "constructor", "valueOf", "__proto__"]) {
      expect(buildLabel(null, key)).toBe("?");
    }
  });
});

describe("versionWarning", () => {
  it("is null for a healthy install", () => {
    expect(versionWarning("installed")).toBeNull();
  });

  it("names the fault for each unresolvable source", () => {
    expect(versionWarning("not_installed")).toBe("not installed");
    expect(versionWarning("metadata_incomplete")).toBe("install damaged");
    expect(versionWarning("metadata_unreadable")).toBe("metadata unreadable");
  });

  it("is null for an absent source", () => {
    // A server predating the field claims nothing and must not read as broken.
    expect(versionWarning(null)).toBeNull();
    expect(versionWarning(undefined)).toBeNull();
  });

  it("still reports a fault this client is too old to name", () => {
    // The half that `?? null` got wrong: every source but `installed` is a
    // fault by construction server-side, so a newer server reporting a new one
    // must not render as healthy on the one screen that reports this.
    expect(versionWarning("something_new")).toBe("version unresolved");
  });

  it("does not resolve inherited Object properties as faults", () => {
    // `VERSION_FAULTS["constructor"]` is a function — truthy — so a bare index
    // would paint a red marker, and its text, onto a healthy install.
    for (const key of ["toString", "constructor", "valueOf", "__proto__"]) {
      expect(versionWarning(key)).toBe("version unresolved");
    }
  });
});
