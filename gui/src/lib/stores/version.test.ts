import { describe, it, expect, vi, beforeEach } from "vitest";

const mocks = vi.hoisted(() => ({
  getVersionMock: vi.fn(),
}));

vi.mock("../api/version", () => ({
  getVersion: mocks.getVersionMock,
}));

import { version } from "./version.svelte";

describe("version store", () => {
  beforeEach(() => {
    mocks.getVersionMock.mockReset();
    version.reset();
  });

  it("starts with all fields nulled out", () => {
    expect(version.snapshot.info).toBeNull();
    expect(version.snapshot.compatible).toBeNull();
    expect(version.snapshot.errorMessage).toBeNull();
    expect(version.snapshot.checking).toBe(false);
  });

  it("sets compatible=true on matching api_major", async () => {
    mocks.getVersionMock.mockResolvedValueOnce({
      api_major: 1,
      api_minor: 0,
      server_version: null,
      build_hash: null,
    });
    await version.check();
    expect(version.snapshot.compatible).toBe(true);
    expect(version.snapshot.info?.api_major).toBe(1);
    expect(version.snapshot.errorMessage).toBeNull();
    expect(version.snapshot.checking).toBe(false);
  });

  it("sets compatible=false on mismatched api_major", async () => {
    mocks.getVersionMock.mockResolvedValueOnce({
      api_major: 2,
      api_minor: 0,
      server_version: "9.9.9",
      build_hash: "deadbeef",
    });
    await version.check();
    expect(version.snapshot.compatible).toBe(false);
    expect(version.snapshot.info?.api_major).toBe(2);
    expect(version.snapshot.errorMessage).toBeNull();
  });

  it("sets errorMessage and compatible=null when getVersion throws", async () => {
    mocks.getVersionMock.mockRejectedValueOnce(new Error("boom"));
    await version.check();
    expect(version.snapshot.errorMessage).toContain("boom");
    expect(version.snapshot.compatible).toBeNull();
    expect(version.snapshot.checking).toBe(false);
  });

  it("reset clears all fields back to initial", async () => {
    mocks.getVersionMock.mockResolvedValueOnce({
      api_major: 1,
      api_minor: 0,
      server_version: null,
      build_hash: null,
    });
    await version.check();
    expect(version.snapshot.compatible).toBe(true);
    version.reset();
    expect(version.snapshot.info).toBeNull();
    expect(version.snapshot.compatible).toBeNull();
    expect(version.snapshot.errorMessage).toBeNull();
    expect(version.snapshot.checking).toBe(false);
  });
});
