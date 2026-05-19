/**
 * Render Tauri-side errors into a human-readable string for the UI.
 *
 * The Rust side serialises tagged enums like HttpError as
 * `{ kind: "<variant>", detail: <inner> }`. Outer wrappers (AuthError,
 * SearchError, ...) nest more of the same shape, so we walk recursively.
 *
 * Special case for HttpError::HttpStatus { status, body }: when the body
 * is an RFC 7807 problem document (application/problem+json), surface
 * `title` + `detail` instead of dumping the raw JSON string. Falls back
 * to `<status> <body-text>` for non-problem responses.
 */

type ProblemBody = {
  type?: unknown;
  title?: unknown;
  status?: unknown;
  detail?: unknown;
};

function tryParseProblem(body: unknown): ProblemBody | null {
  if (typeof body !== "string") return null;
  try {
    const parsed = JSON.parse(body);
    if (parsed && typeof parsed === "object") return parsed as ProblemBody;
  } catch {
    // not JSON — fall through
  }
  return null;
}

function formatHttpStatus(detail: { status?: unknown; body?: unknown }): string {
  const status = typeof detail.status === "number" ? detail.status : null;
  const body = detail.body;
  const problem = tryParseProblem(body);
  if (problem) {
    const title = typeof problem.title === "string" ? problem.title : null;
    const inner = typeof problem.detail === "string" ? problem.detail : null;
    const parts: string[] = [];
    if (status !== null) parts.push(String(status));
    if (title) parts.push(title);
    const head = parts.join(" ");
    return inner ? `${head}: ${inner}` : head || "HTTP error";
  }
  const bodyStr = typeof body === "string" ? body : safeStringify(body);
  if (status !== null) return bodyStr ? `${status}: ${bodyStr}` : String(status);
  return bodyStr || "HTTP error";
}

function safeStringify(value: unknown): string {
  if (value === null || value === undefined) return "";
  try {
    return JSON.stringify(value);
  } catch {
    return String(value);
  }
}

export function formatError(err: unknown): string {
  if (err && typeof err === "object") {
    const o = err as { kind?: unknown; detail?: unknown };
    const kind = typeof o.kind === "string" ? o.kind : null;

    if (kind === "HttpStatus" && o.detail && typeof o.detail === "object") {
      return formatHttpStatus(o.detail as { status?: unknown; body?: unknown });
    }

    if (kind && o.detail !== undefined && o.detail !== null) {
      const detailStr =
        typeof o.detail === "object"
          ? formatError(o.detail)
          : String(o.detail);
      return `${kind}: ${detailStr}`;
    }
    if (kind) return kind;

    return safeStringify(err) || String(err);
  }
  return String(err);
}
