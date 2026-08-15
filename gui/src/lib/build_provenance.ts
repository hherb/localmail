/**
 * Human phrasing for the server's build provenance (#278, #300).
 *
 * Pure, and separate from the About tab, per project convention — logic out of
 * components. Both functions accept an unknown source and degrade quietly: the
 * client can be older or newer than the server it is talking to, and a version
 * screen that renders "undefined" or cries wolf about a healthy install is
 * worse than one that says nothing.
 */

const BUILD_REASONS: Record<string, string> = {
  not_a_repo: "— not a repository",
  git_unavailable: "— git unavailable",
  git_failed: "— could not read the repository",
};

const VERSION_FAULTS: Record<string, string> = {
  not_installed: "not installed",
  metadata_incomplete: "install damaged",
  metadata_unreadable: "metadata unreadable",
};

/** The placeholder for "this server told us nothing we understand". */
const UNKNOWN = "?";

/** What the "Server build" row shows. */
export function buildLabel(
  hash: string | null | undefined,
  source: string | null | undefined,
): string {
  if (hash) return hash;
  if (source && source in BUILD_REASONS) return BUILD_REASONS[source];
  return UNKNOWN;
}

/**
 * A short fault phrase to mark the "Server" row with, or null when the
 * server's version is trustworthy. `installed` and anything unrecognised are
 * both null — only a *known* fault is worth alarming about.
 */
export function versionWarning(source: string | null | undefined): string | null {
  if (!source) return null;
  return VERSION_FAULTS[source] ?? null;
}
