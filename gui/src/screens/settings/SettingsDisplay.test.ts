import { render, fireEvent } from "@testing-library/svelte";
import { describe, it, expect, beforeEach } from "vitest";
import SettingsDisplay from "./SettingsDisplay.svelte";
import { settings } from "../../lib/stores/settings.svelte";

beforeEach(() => {
  settings.resetForTest();
});

describe("SettingsDisplay", () => {
  it("flips density to compact on click", async () => {
    const { getByLabelText } = render(SettingsDisplay);
    await fireEvent.click(getByLabelText(/Compact/));
    expect(settings.snapshot.density).toBe("compact");
  });

  it("flips image policy to allow on click", async () => {
    const { getByLabelText } = render(SettingsDisplay);
    await fireEvent.click(getByLabelText(/Always allow/));
    expect(settings.snapshot.imagePolicy).toBe("allow");
  });

  it("flips date format to absolute on click", async () => {
    const { getByLabelText } = render(SettingsDisplay);
    await fireEvent.click(getByLabelText(/Absolute/));
    expect(settings.snapshot.dateFormat).toBe("absolute");
  });
});
