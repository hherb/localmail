/**
 * Pure helpers for branching on the HTTP status buried inside a Tauri
 * error value.
 *
 * The Rust side serialises tagged enums as `{ kind, detail }` and nests
 * them (`AuthError::Http(HttpError::HttpStatus { status, body })` arrives
 * as `{kind:"Http",detail:{kind:"HttpStatus",detail:{status,body}}}`).
 * `formatError` already renders these for display; this module exists for
 * the cases where the UI must *act* on the status — a 409 offering a
 * force-delete, a 403 explaining that admin rights were revoked.
 */

const CONFLICT = 409;
const FORBIDDEN = 403;

// Bounds the walk so a malformed (or self-referential) error object cannot
// spin. The real nesting is at most three levels deep.
const MAX_DEPTH = 8;

export function httpStatusOf(err: unknown): number | null {
  let node: unknown = err;
  for (let depth = 0; depth < MAX_DEPTH; depth += 1) {
    if (!node || typeof node !== "object") return null;
    const { kind, detail } = node as { kind?: unknown; detail?: unknown };
    if (kind === "HttpStatus" && detail && typeof detail === "object") {
      const status = (detail as { status?: unknown }).status;
      return typeof status === "number" ? status : null;
    }
    if (detail === node) return null;
    node = detail;
  }
  return null;
}

export function isConflict(err: unknown): boolean {
  return httpStatusOf(err) === CONFLICT;
}

export function isForbidden(err: unknown): boolean {
  return httpStatusOf(err) === FORBIDDEN;
}
