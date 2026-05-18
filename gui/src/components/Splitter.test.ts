import { render, fireEvent } from "@testing-library/svelte";
import { describe, it, expect, vi, beforeAll } from "vitest";
import Splitter from "./Splitter.svelte";

// jsdom 25 does not implement PointerEvent. Polyfill with a MouseEvent subclass
// so fireEvent.pointer* dispatches carry clientX / pointerId through to handlers.
beforeAll(() => {
  if (typeof (globalThis as unknown as { PointerEvent?: unknown }).PointerEvent === "undefined") {
    class PointerEventPolyfill extends MouseEvent {
      pointerId: number;
      constructor(type: string, init: PointerEventInit = {}) {
        super(type, init);
        this.pointerId = init.pointerId ?? 0;
      }
    }
    (globalThis as unknown as { PointerEvent: typeof PointerEventPolyfill }).PointerEvent =
      PointerEventPolyfill;
  }
});

describe("Splitter", () => {
  it("renders a vertical drag handle with role=separator", () => {
    const { getByRole } = render(Splitter, { props: { onResize: vi.fn() } });
    expect(getByRole("separator")).toBeTruthy();
  });

  it("calls onResize with deltaX on pointer drag", async () => {
    const onResize = vi.fn();
    const { getByRole } = render(Splitter, { props: { onResize } });
    const handle = getByRole("separator");
    await fireEvent.pointerDown(handle, { clientX: 100, pointerId: 1 });
    await fireEvent.pointerMove(window, { clientX: 130, pointerId: 1 });
    await fireEvent.pointerUp(window, { clientX: 130, pointerId: 1 });
    expect(onResize).toHaveBeenCalled();
    const totalDelta = onResize.mock.calls.reduce((sum, [d]) => sum + d, 0);
    expect(totalDelta).toBe(30);
  });

  it("ignores pointer events when disabled", async () => {
    const onResize = vi.fn();
    const { getByRole } = render(Splitter, { props: { onResize, disabled: true } });
    const handle = getByRole("separator");
    await fireEvent.pointerDown(handle, { clientX: 100, pointerId: 1 });
    await fireEvent.pointerMove(window, { clientX: 130, pointerId: 1 });
    await fireEvent.pointerUp(window, { clientX: 130, pointerId: 1 });
    expect(onResize).not.toHaveBeenCalled();
  });
});
