import { describe, expect, it } from "vitest";

import { restartSyncAccountIds } from "./daemon_view";

describe("restartSyncAccountIds", () => {
  it("returns an empty list for no heartbeats", () => {
    expect(restartSyncAccountIds([])).toEqual([]);
  });

  it("ignores heartbeats without an account id", () => {
    expect(
      restartSyncAccountIds([
        { account_id: null },
        { account_id: null },
      ]),
    ).toEqual([]);
  });

  it("dedupes the idle+poll pair that shares one account", () => {
    // The daemon runs two workers (idle, poll) per account, each with its own
    // heartbeat row — the restart-sync button must appear once per account.
    expect(
      restartSyncAccountIds([
        { account_id: "3" },
        { account_id: "3" },
      ]),
    ).toEqual(["3"]);
  });

  it("preserves first-seen order across accounts", () => {
    expect(
      restartSyncAccountIds([
        { account_id: "5" },
        { account_id: "2" },
        { account_id: "5" },
        { account_id: "2" },
      ]),
    ).toEqual(["5", "2"]);
  });

  it("keeps non-null ids and drops interleaved nulls", () => {
    expect(
      restartSyncAccountIds([
        { account_id: null },
        { account_id: "9" },
        { account_id: null },
        { account_id: "9" },
      ]),
    ).toEqual(["9"]);
  });
});
