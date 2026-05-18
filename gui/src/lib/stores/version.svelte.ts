/**
 * Singleton store that owns the server's advertised API version. The
 * `check()` method is called once at startup (and may be re-invoked after
 * a poll-response error). `compatible === false` triggers VersionGate's
 * hard modal; `null` means "not yet checked" or "check failed".
 */
import { getVersion, type ServerVersionInfo } from "../api/version";
import { isMajorCompatible } from "../version_check";

interface VersionState {
  info: ServerVersionInfo | null;
  compatible: boolean | null;
  errorMessage: string | null;
  checking: boolean;
}

class VersionStore {
  #state: VersionState = $state({
    info: null,
    compatible: null,
    errorMessage: null,
    checking: false,
  });

  get snapshot(): VersionState {
    return this.#state;
  }

  async check(): Promise<void> {
    this.#state.checking = true;
    this.#state.errorMessage = null;
    try {
      const info = await getVersion();
      this.#state.info = info;
      this.#state.compatible = isMajorCompatible(info);
    } catch (err: unknown) {
      this.#state.errorMessage = String(err);
      this.#state.compatible = null;
    } finally {
      this.#state.checking = false;
    }
  }

  reset(): void {
    this.#state.info = null;
    this.#state.compatible = null;
    this.#state.errorMessage = null;
    this.#state.checking = false;
  }
}

export const version = new VersionStore();
