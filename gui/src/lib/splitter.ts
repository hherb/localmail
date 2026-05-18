export const MIN_PANE_WIDTH_PX = 160;
export const DEFAULT_LEFT_WIDTH_PX = 220;
export const DEFAULT_MIDDLE_WIDTH_PX = 340;

export interface PaneWidths {
  left: number;
  middle: number;
}

export interface ClampContext {
  containerWidth: number;
}

export function clampPaneWidths(input: PaneWidths, ctx: ClampContext): PaneWidths {
  if (ctx.containerWidth < MIN_PANE_WIDTH_PX * 3) {
    return { left: DEFAULT_LEFT_WIDTH_PX, middle: DEFAULT_MIDDLE_WIDTH_PX };
  }
  const left = Math.max(MIN_PANE_WIDTH_PX, Math.min(input.left, ctx.containerWidth - 2 * MIN_PANE_WIDTH_PX));
  const middleMax = ctx.containerWidth - left - MIN_PANE_WIDTH_PX;
  const middle = Math.max(MIN_PANE_WIDTH_PX, Math.min(input.middle, middleMax));
  return { left, middle };
}

export function serializeWidths(w: PaneWidths): string {
  return JSON.stringify({ left: w.left, middle: w.middle });
}

export function parseStoredWidths(raw: string): PaneWidths | null {
  let parsed: unknown;
  try {
    parsed = JSON.parse(raw);
  } catch {
    return null;
  }
  if (!parsed || typeof parsed !== "object") return null;
  const obj = parsed as Record<string, unknown>;
  const left = obj.left;
  const middle = obj.middle;
  if (typeof left !== "number" || !Number.isFinite(left)) return null;
  if (typeof middle !== "number" || !Number.isFinite(middle)) return null;
  return { left, middle };
}
