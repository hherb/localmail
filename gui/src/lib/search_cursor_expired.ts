/**
 * Returns true when `err` carries the /problems/search-cursor-expired
 * problem-type from the server. Used by the search store to drive the
 * transparent re-submit + drop-and-append recovery path.
 *
 * Tauri's invoke() rejects with the serialised Rust error — a plain JS
 * object shaped as nested `{kind, detail}` chains (both AuthError and
 * HttpError use `#[serde(tag = "kind", content = "detail")]`).  A 409
 * looks like:
 *
 *   { kind: "Http",
 *     detail: { kind: "HttpStatus",
 *                detail: { status: 409,
 *                           body: '{"type":"/problems/search-cursor-expired",...}' } } }
 *
 * There is no `.message` field on these objects, so the old predicate
 * always fell back to String(err) = "[object Object]" and returned false.
 *
 * The fix: JSON.stringify the error and substring-match the canonical
 * type URI. This is safe because we control both sides of the wire and
 * the type URI is a stable string we never use for anything else.
 * Plain Error objects and raw strings still match via the same path
 * (JSON.stringify of an Error gives "{}", but we also check .message).
 */
export function isSearchCursorExpired(err: unknown): boolean {
  const needle = "/problems/search-cursor-expired";
  if (typeof err === "string") return err.includes(needle);
  if (err && typeof err === "object" && "message" in err) {
    if (String((err as { message: unknown }).message).includes(needle)) return true;
  }
  try {
    return JSON.stringify(err).includes(needle);
  } catch {
    return false;
  }
}
