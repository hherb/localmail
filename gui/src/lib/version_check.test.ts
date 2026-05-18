import { describe, it, expect } from "vitest";
import { isMajorCompatible, EXPECTED_API_MAJOR } from "./version_check";

describe("isMajorCompatible", () => {
  it("returns true when major matches", () => {
    expect(isMajorCompatible({ api_major: EXPECTED_API_MAJOR, api_minor: 0 })).toBe(true);
  });

  it("returns true when major matches and minor differs", () => {
    expect(isMajorCompatible({ api_major: EXPECTED_API_MAJOR, api_minor: 99 })).toBe(true);
  });

  it("returns false when major is one greater", () => {
    expect(isMajorCompatible({ api_major: EXPECTED_API_MAJOR + 1, api_minor: 0 })).toBe(false);
  });

  it("returns false when major is one less", () => {
    expect(isMajorCompatible({ api_major: EXPECTED_API_MAJOR - 1, api_minor: 0 })).toBe(false);
  });

  it("returns false for zero", () => {
    expect(isMajorCompatible({ api_major: 0, api_minor: 0 })).toBe(false);
  });
});
