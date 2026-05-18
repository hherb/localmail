import { render } from "@testing-library/svelte";
import { describe, it, expect } from "vitest";
import DebugChunks from "./DebugChunks.svelte";

describe("DebugChunks", () => {
  it("renders 'no matched chunks' placeholder when matchedChunks is undefined", () => {
    const { container } = render(DebugChunks, { props: {} });
    expect(container.querySelector("details")).toBeFalsy();
    const placeholder = container.querySelector(".debug-chunks-empty");
    expect(placeholder).toBeTruthy();
    expect(placeholder?.textContent).toBe("no matched chunks");
  });

  it("renders 'no matched chunks' placeholder when matchedChunks is empty array", () => {
    const { container } = render(DebugChunks, { props: { matchedChunks: [] } });
    expect(container.querySelector("details")).toBeFalsy();
    expect(container.querySelector(".debug-chunks-empty")?.textContent).toBe("no matched chunks");
  });

  it("renders one <li> per chunk", () => {
    const { container } = render(DebugChunks, {
      props: {
        matchedChunks: [
          { kind: "body", text: "hello", score: 0.9 },
          { kind: "subject", text: "world", score: 0.8 },
        ],
      },
    });
    expect(container.querySelectorAll("li").length).toBe(2);
  });

  it("renders the chunk kind and score for each chunk", () => {
    const { container } = render(DebugChunks, {
      props: {
        matchedChunks: [
          { kind: "body", text: "hello world", score: 0.7531 },
        ],
      },
    });
    const kind = container.querySelector(".kind");
    expect(kind?.textContent).toBe("body");
    const score = container.querySelector(".score");
    expect(score?.textContent).toBe("0.753");
    const pre = container.querySelector("pre");
    expect(pre?.textContent).toBe("hello world");
  });

  it("omits the score chip when chunk.score is undefined", () => {
    const { container } = render(DebugChunks, {
      props: {
        matchedChunks: [{ kind: "body", text: "no score here" }],
      },
    });
    expect(container.querySelectorAll("li").length).toBe(1);
    expect(container.querySelector(".score")).toBeFalsy();
    expect(container.querySelector(".kind")?.textContent).toBe("body");
    expect(container.querySelector("pre")?.textContent).toBe("no score here");
  });

  it("uses singular 'chunk' in the summary for a single chunk", () => {
    const { container } = render(DebugChunks, {
      props: {
        matchedChunks: [{ kind: "body", text: "only one", score: 0.5 }],
      },
    });
    const summary = container.querySelector("summary");
    expect(summary?.textContent).toBe("1 matched chunk");
  });

  it("uses plural 'chunks' in the summary for multiple chunks", () => {
    const { container } = render(DebugChunks, {
      props: {
        matchedChunks: [
          { kind: "body", text: "a", score: 0.5 },
          { kind: "body", text: "b", score: 0.4 },
        ],
      },
    });
    const summary = container.querySelector("summary");
    expect(summary?.textContent).toBe("2 matched chunks");
  });
});
