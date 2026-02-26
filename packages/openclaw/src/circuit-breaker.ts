/**
 * Simple circuit breaker to prevent hammering a dead Memex server.
 *
 * State machine:
 *   closed (healthy)
 *     → open  (after failureThreshold consecutive failures)
 *     → half-open (after resetTimeoutMs, allows one probe)
 *     → closed (if probe succeeds) or open (if probe fails)
 */

export type CircuitState = 'closed' | 'open' | 'half-open';

export interface CircuitBreakerOptions {
  /** Number of consecutive failures before opening the circuit. Default: 3. */
  failureThreshold?: number;
  /** Milliseconds to stay open before allowing a probe. Default: 60_000. */
  resetTimeoutMs?: number;
}

export class CircuitBreaker {
  private state: CircuitState = 'closed';
  private failureCount = 0;
  private lastFailureTime = 0;

  private readonly failureThreshold: number;
  private readonly resetTimeoutMs: number;

  constructor(options: CircuitBreakerOptions = {}) {
    this.failureThreshold = options.failureThreshold ?? 3;
    this.resetTimeoutMs = options.resetTimeoutMs ?? 60_000;
  }

  /**
   * Returns true when the circuit is open and requests should be skipped.
   * Automatically transitions from open → half-open after resetTimeoutMs.
   */
  isOpen(): boolean {
    if (this.state === 'open') {
      if (Date.now() - this.lastFailureTime >= this.resetTimeoutMs) {
        this.state = 'half-open';
        return false;
      }
      return true;
    }
    return false;
  }

  /** Record a successful request; resets the breaker to closed. */
  recordSuccess(): void {
    this.failureCount = 0;
    this.state = 'closed';
  }

  /**
   * Record a failed request. Opens the circuit after failureThreshold
   * consecutive failures.
   */
  recordFailure(): void {
    this.failureCount++;
    this.lastFailureTime = Date.now();
    if (this.failureCount >= this.failureThreshold) {
      this.state = 'open';
    }
  }

  get currentState(): CircuitState {
    return this.state;
  }
}
