import { describe, expect, it } from "vitest";

import { hasImapEndpoint, usesStoredPassword } from "./admin_auth_method";

describe("hasImapEndpoint", () => {
  it("is true for the live auth methods", () => {
    expect(hasImapEndpoint("password")).toBe(true);
    expect(hasImapEndpoint("oauth2")).toBe(true);
  });

  it("is false for archive accounts, which have no IMAP endpoint", () => {
    expect(hasImapEndpoint("archive")).toBe(false);
  });
});

describe("usesStoredPassword", () => {
  it("is true only for password auth", () => {
    expect(usesStoredPassword("password")).toBe(true);
  });

  it("is false for oauth2, whose refresh token comes from the consent flow", () => {
    expect(usesStoredPassword("oauth2")).toBe(false);
  });

  it("is false for archive", () => {
    expect(usesStoredPassword("archive")).toBe(false);
  });
});
