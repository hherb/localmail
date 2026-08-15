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

  it("is null for an unknown or absent source", () => {
    // A server predating the field must not be rendered as broken.
    expect(versionWarning(null)).toBeNull();
    expect(versionWarning("something_new")).toBeNull();
  });
});
