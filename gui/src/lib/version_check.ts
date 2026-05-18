/**
 * Pure helper: the client is compatible with the server iff the server's
 * advertised api_major equals the major this client was built against.
 * Minor differences are tolerated (additive changes only). VersionGate
 * surfaces a hard modal when this returns false.
 */
export const EXPECTED_API_MAJOR = 1;

export interface VersionInfo {
  api_major: number;
  api_minor: number;
}

export function isMajorCompatible(v: VersionInfo): boolean {
  return v.api_major === EXPECTED_API_MAJOR;
}
