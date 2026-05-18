import { render } from "@testing-library/svelte";
import { describe, it, expect } from "vitest";
import DebugBadges from "./DebugBadges.svelte";

describe("DebugBadges", () => {
  it("renders score rounded to three decimals", () => {
    const { getByTitle } = render(DebugBadges, {
      props: { score: 0.7531, matchedArms: ["bm25_messages", "vector_chunks"] },
    });
    expect(getByTitle("Fused score").textContent).toBe("0.753");
  });

  it("renders one chip per matched arm", () => {
    const { getByText } = render(DebugBadges, {
      props: { score: 0.5, matchedArms: ["bm25_messages", "vector_chunks"] },
    });
    expect(getByText("bm25_messages")).toBeTruthy();
    expect(getByText("vector_chunks")).toBeTruthy();
  });

  it("renders only the score when matchedArms is empty", () => {
    const { container, getByTitle } = render(DebugBadges, {
      props: { score: 0.1, matchedArms: [] },
    });
    expect(getByTitle("Fused score").textContent).toBe("0.100");
    expect(container.querySelectorAll(".arm").length).toBe(0);
  });
});
