/**
 * Single source of truth for the GUI's auth lifecycle. A Svelte 5
 * rune-backed singleton — `$state` gives fine-grained reactivity in
 * components that read `auth.snapshot.*`.
 *
 * State transitions:
 *
 *   initial ─ refreshState ─► logged_out / logged_in
 *      │
 *      ├─ probe ─► needs_trust ─ confirmTrust ─► logged_out
 *      │
 *      └─ (any) ─ login (success) ─► logged_in
 *               ─ login (failure) ─► logged_out + errorMessage
 *               ─ logout ─► logged_out
 */
import {
  confirmTrust as rustConfirmTrust,
  getCapabilities,
  login as rustLogin,
  logout as rustLogout,
  probeServer,
  refresh as rustRefresh,
  whoami,
  type Capabilities,
  type ProbeResult,
} from "../tauri";
import { formatError } from "../format_error";

export type AuthState =
  | { phase: "connecting" }
  | {
      phase: "needs_trust";
      url: string;
      apiMajor: number;
      apiMinor: number;
      serverVersion: string;
      certSha256: string;
    }
  | { phase: "logged_out"; errorMessage?: string }
  | {
      phase: "logged_in";
      username: string;
      isAdmin: boolean;
      capabilities: Capabilities;
      expiresAt?: string;
    };

class AuthStore {
  #state: AuthState = $state({ phase: "connecting" });
  // Endpoint metadata is keyring-resident on the Rust side. We cache the
  // values we learn during probe/confirmTrust so the Settings → Server tab
  // can display them without a round-trip. These are not used for any
  // authenticated request — Rust always reads from the keyring directly.
  #serverUrl: string | null = $state(null);
  #certPin: string | null = $state(null);

  get snapshot(): AuthState {
    return this.#state;
  }

  get serverUrl(): string | null {
    return this.#serverUrl;
  }

  get certPin(): string | null {
    return this.#certPin;
  }

  reset(): void {
    this.#state = { phase: "connecting" };
    this.#serverUrl = null;
    this.#certPin = null;
  }

  async refreshState(): Promise<void> {
    try {
      const me = await whoami();
      const caps = await getCapabilities();
      // === true normalises the undefined an older server sends.
      this.#state = {
        phase: "logged_in",
        username: me.username,
        isAdmin: me.is_admin === true,
        capabilities: caps,
      };
    } catch (err: unknown) {
      const kind = (err as { kind?: string } | undefined)?.kind;
      if (kind === "NotConnected") {
        this.#state = { phase: "connecting" };
      } else {
        this.#state = { phase: "logged_out" };
      }
    }
  }

  async probe(url: string): Promise<void> {
    try {
      const res: ProbeResult = await probeServer(url);
      this.#serverUrl = url;
      this.#certPin = res.cert_sha256;
      this.#state = {
        phase: "needs_trust",
        url,
        apiMajor: res.api_major,
        apiMinor: res.api_minor,
        serverVersion: res.server_version,
        certSha256: res.cert_sha256,
      };
    } catch (err: unknown) {
      this.#state = { phase: "connecting" };
      throw new Error(formatError(err));
    }
  }

  async confirmTrust(): Promise<void> {
    if (this.#state.phase !== "needs_trust") {
      throw new Error("confirmTrust called when not in needs_trust state");
    }
    await rustConfirmTrust(this.#state.url, this.#state.certSha256);
    this.#serverUrl = this.#state.url;
    this.#certPin = this.#state.certSha256;
    this.#state = { phase: "logged_out" };
  }

  async login(username: string, password: string): Promise<void> {
    try {
      await rustLogin(username, password);
      await this.refreshState();
    } catch (err: unknown) {
      this.#state = { phase: "logged_out", errorMessage: formatError(err) };
    }
  }

  async logout(): Promise<void> {
    try {
      await rustLogout();
    } finally {
      this.#state = { phase: "logged_out" };
    }
  }

  async refreshToken(): Promise<void> {
    try {
      await rustRefresh();
      await this.refreshState();
    } catch (err: unknown) {
      this.#state = { phase: "logged_out", errorMessage: formatError(err) };
    }
  }
}

export const auth = new AuthStore();
