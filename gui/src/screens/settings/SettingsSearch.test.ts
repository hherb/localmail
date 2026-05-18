import { render, fireEvent } from "@testing-library/svelte";
import { describe, it, expect, beforeEach } from "vitest";
import SettingsSearch from "./SettingsSearch.svelte";
import { settings } from "../../lib/stores/settings.svelte";

beforeEach(() => {
  settings.resetForTest();
});

describe("SettingsSearch", () => {
  it("toggles debug on checkbox click", async () => {
    const { getByRole } = render(SettingsSearch);
    const cb = getByRole("checkbox") as HTMLInputElement;
    await fireEvent.click(cb);
    expect(settings.snapshot.debug).toBe(true);
  });

  it("clamps page size to 200 at blur", async () => {
    const { getByRole } = render(SettingsSearch);
    const numInput = getByRole("spinbutton") as HTMLInputElement;
    await fireEvent.input(numInput, { target: { value: "1000" } });
    await fireEvent.blur(numInput);
    expect(settings.snapshot.pageSize).toBe(200);
  });

  it("normalises default language to lowercase at blur", async () => {
    const { getByPlaceholderText } = render(SettingsSearch);
    const langInput = getByPlaceholderText(/e\.g\. en/) as HTMLInputElement;
    await fireEvent.input(langInput, { target: { value: "EN" } });
    await fireEvent.blur(langInput);
    expect(settings.snapshot.defaultLanguage).toBe("en");
  });
});
