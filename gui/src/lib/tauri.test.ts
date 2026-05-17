import { describe, expect, it, vi } from "vitest";

// Mock the Tauri invoke surface BEFORE importing the wrapper.
vi.mock("@tauri-apps/api/core", () => ({
  invoke: vi.fn(async (cmd: string, args: Record<string, unknown>) => {
    if (cmd === "greet") {
      return {
        message: `Hello, ${(args as { name: string }).name}! (from Rust)`,
        source: "tauri-cmd",
      };
    }
    throw new Error(`unknown cmd: ${cmd}`);
  }),
}));

import { greet } from "./tauri";

describe("greet wrapper", () => {
  it("forwards name and unwraps the Greeting struct", async () => {
    const out = await greet("alice");
    expect(out.message).toContain("alice");
    expect(out.source).toBe("tauri-cmd");
  });
});
