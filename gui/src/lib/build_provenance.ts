/**
 * Human phrasing for the server's build provenance (#278, #300).
 *
 * Pure, and separate from the About tab, per project convention — logic out of
 * components. Both functions accept an unknown source and degrade: the client
 * can be older or newer than the server it is talking to, and a version screen
 * that renders "undefined" is worse than one that says nothing.
 *
 * The two functions degrade in *opposite* directions, deliberately. A build
 * hash is often legitimately absent (`not_a_repo` is the normal state of an
 * installed artifact), so an unrecognised build source says nothing. A version
 * source is a fault unless it is exactly `installed` — every other member of
 * the server's enum is required to carry a remedy — so an unrecognised one is
 * reported. Reading that map with `?? null` instead would let a *newer* server
 * report a *new* fault and have this screen render it as healthy: the
 * `dict.get()` hole CLAUDE.md closes server-side, reopened on the client.
 *
 * Both lookups are `Object.hasOwn`, never `in` or a bare index: these keys come
 * off the wire, and a plain object literal inherits `Object.prototype`, so
 * `"toString"` and `"constructor"` are otherwise "present" and resolve to
 * functions that render into the row.
 *
 * The wire strings are restated here by hand from the Python enums, with no
 * differential test possible across the language boundary. That is priced, not
 * missed: a drifted key degrades to `UNKNOWN` or a generic warning — it never
 * asserts something false.
 */

type UnidentifiedBuildSource = "not_a_repo" | "git_unavailable" | "git_failed";

const BUILD_REASONS: Record<UnidentifiedBuildSource, string> = {
  not_a_repo: "— not a repository",
  git_unavailable: "— git unavailable",
  git_failed: "— could not read the repository",
};

type VersionFault = "not_installed" | "metadata_incomplete" | "metadata_unreadable";

const VERSION_FAULTS: Record<VersionFault, string> = {
  not_installed: "not installed",
  metadata_incomplete: "install damaged",
  metadata_unreadable: "metadata unreadable",
};

/** The one `version_source` that means nothing is wrong. */
const HEALTHY_VERSION_SOURCE = "installed";

/** The placeholder for "this server told us nothing we understand". */
const UNKNOWN = "?";

/** Shown when the server names a fault this client is too old to phrase. */
const UNRECOGNISED_FAULT = "version unresolved";

/** What the "Server build" row shows. */
export function buildLabel(
  hash: string | null | undefined,
  source: string | null | undefined,
): string {
  if (hash) return hash;
  if (source && Object.hasOwn(BUILD_REASONS, source)) {
    return BUILD_REASONS[source as UnidentifiedBuildSource];
  }
  return UNKNOWN;
}

/**
 * A short fault phrase to mark the "Server" row with, or null when the
 * server's version is trustworthy. Only `installed` — and an absent field,
 * which is an older server claiming nothing — are silent.
 */
export function versionWarning(source: string | null | undefined): string | null {
  if (!source) return null;
  if (source === HEALTHY_VERSION_SOURCE) return null;
  if (Object.hasOwn(VERSION_FAULTS, source)) {
    return VERSION_FAULTS[source as VersionFault];
  }
  return UNRECOGNISED_FAULT;
}
