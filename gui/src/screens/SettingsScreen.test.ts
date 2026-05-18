import { fireEvent, render } from "@testing-library/svelte";
import { describe, expect, it, vi } from "vitest";

// SettingsAbout (filled in by a sibling batch-3b agent) imports
// `@tauri-apps/api/core` for its "Open log directory" button. Mock it so
// the import resolves under jsdom. Mock the version store import as well
// in case it touches platform APIs.
vi.mock("@tauri-apps/api/core", () => ({
  invoke: vi.fn(async () => undefined),
}));

import SettingsScreen from "./SettingsScreen.svelte";

/**
 * Each tab body has a unique root element we can query for. The four
 * sub-components may evolve independently (batch 3b), but their root
 * marker classes are part of the scaffold contract:
 *   SettingsServer  -> [data-testid=settings-server-placeholder] (still a stub here)
 *   SettingsDisplay -> section.display
 *   SettingsSearch  -> section.search
 *   SettingsAbout   -> section.about
 *
 * We assert "this tab is rendered" by finding any of these unique markers
 * so the test stays green whether the sub-component is a stub or its
 * full implementation.
 */
function serverShown(container: HTMLElement): boolean {
  return (
    container.querySelector("[data-testid=settings-server-placeholder]") !== null ||
    container.querySelector("section.server") !== null
  );
}
function displayShown(container: HTMLElement): boolean {
  return (
    container.querySelector("[data-testid=settings-display-placeholder]") !== null ||
    container.querySelector("section.display") !== null
  );
}
function searchShown(container: HTMLElement): boolean {
  return (
    container.querySelector("[data-testid=settings-search-placeholder]") !== null ||
    container.querySelector("section.search") !== null
  );
}
function aboutShown(container: HTMLElement): boolean {
  return (
    container.querySelector("[data-testid=settings-about-placeholder]") !== null ||
    container.querySelector("section.about") !== null
  );
}

describe("SettingsScreen", () => {
  it("renders nothing when open=false", () => {
    const { container } = render(SettingsScreen, {
      props: { open: false, onClose: vi.fn() },
    });
    expect(container.querySelector("[role=dialog]")).toBeFalsy();
  });

  it("renders the dialog when open=true", () => {
    const { container } = render(SettingsScreen, {
      props: { open: true, onClose: vi.fn() },
    });
    expect(container.querySelector("[role=dialog]")).toBeTruthy();
  });

  it("defaults to the Server tab", () => {
    const { container } = render(SettingsScreen, {
      props: { open: true, onClose: vi.fn() },
    });
    expect(serverShown(container)).toBe(true);
    expect(displayShown(container)).toBe(false);
    expect(searchShown(container)).toBe(false);
    expect(aboutShown(container)).toBe(false);
  });

  it("switches to the Display tab when its tab button is clicked", async () => {
    const { container, getByTestId } = render(SettingsScreen, {
      props: { open: true, onClose: vi.fn() },
    });
    await fireEvent.click(getByTestId("settings-tab-display"));
    expect(displayShown(container)).toBe(true);
    expect(serverShown(container)).toBe(false);
  });

  it("switches between Search and About tabs", async () => {
    const { container, getByTestId } = render(SettingsScreen, {
      props: { open: true, onClose: vi.fn() },
    });
    await fireEvent.click(getByTestId("settings-tab-search"));
    expect(searchShown(container)).toBe(true);
    expect(aboutShown(container)).toBe(false);

    await fireEvent.click(getByTestId("settings-tab-about"));
    expect(aboutShown(container)).toBe(true);
    expect(searchShown(container)).toBe(false);
  });

  it("marks the active tab via aria-selected", async () => {
    const { getByTestId } = render(SettingsScreen, {
      props: { open: true, onClose: vi.fn() },
    });
    expect(getByTestId("settings-tab-server").getAttribute("aria-selected")).toBe("true");
    expect(getByTestId("settings-tab-display").getAttribute("aria-selected")).toBe("false");

    await fireEvent.click(getByTestId("settings-tab-display"));
    expect(getByTestId("settings-tab-server").getAttribute("aria-selected")).toBe("false");
    expect(getByTestId("settings-tab-display").getAttribute("aria-selected")).toBe("true");
  });

  it("calls onClose when the close button is clicked", async () => {
    const onClose = vi.fn();
    const { getByLabelText } = render(SettingsScreen, {
      props: { open: true, onClose },
    });
    await fireEvent.click(getByLabelText("Close"));
    expect(onClose).toHaveBeenCalledOnce();
  });
});
