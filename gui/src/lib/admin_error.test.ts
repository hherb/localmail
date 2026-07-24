import { describe, expect, it } from "vitest";

import { httpStatusOf, isConflict, isForbidden } from "./admin_error";

describe("httpStatusOf", () => {
  it("reads a top-level HttpStatus", () => {
    expect(httpStatusOf({ kind: "HttpStatus", detail: { status: 404, body: "" } })).toBe(404);
  });

  it("reads an HttpStatus nested under AuthError::Http", () => {
    const err = {
      kind: "Http",
      detail: { kind: "HttpStatus", detail: { status: 409, body: "in use" } },
    };
    expect(httpStatusOf(err)).toBe(409);
  });

  it("returns null for a non-HTTP error", () => {
    expect(httpStatusOf({ kind: "NotLoggedIn" })).toBeNull();
  });

  it("returns null for junk input", () => {
    expect(httpStatusOf(null)).toBeNull();
    expect(httpStatusOf("boom")).toBeNull();
    expect(httpStatusOf(undefined)).toBeNull();
  });

  it("does not loop forever on a self-referential error", () => {
    const err: Record<string, unknown> = { kind: "Http" };
    err.detail = err;
    expect(httpStatusOf(err)).toBeNull();
  });
});

describe("isConflict / isForbidden", () => {
  it("detects 409", () => {
    expect(isConflict({ kind: "HttpStatus", detail: { status: 409 } })).toBe(true);
    expect(isConflict({ kind: "HttpStatus", detail: { status: 400 } })).toBe(false);
  });

  it("detects 403", () => {
    expect(isForbidden({ kind: "HttpStatus", detail: { status: 403 } })).toBe(true);
    expect(isForbidden({ kind: "NotLoggedIn" })).toBe(false);
  });
});
