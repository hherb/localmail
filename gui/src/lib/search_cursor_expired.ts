/**
 * Returns true when `err` carries the /problems/search-cursor-expired
 * problem-type from the server. Used by the search store to drive the
 * transparent re-submit + drop-and-append recovery path.
 *
 * The Rust side surfaces the response body as a string in AuthError /
 * formatError output; substring match against the canonical type URI is
 * sufficient (we control both sides of the wire).
 */
export function isSearchCursorExpired(err: unknown): boolean {
  const text = typeof err === "string" ? err :
               (err && typeof err === "object" && "message" in err
                  ? String((err as { message: unknown }).message)
                  : String(err));
  return text.includes("/problems/search-cursor-expired");
}
