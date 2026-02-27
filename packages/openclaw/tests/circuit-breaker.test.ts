import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';

import { CircuitBreaker } from '../src/circuit-breaker';

describe('CircuitBreaker', () => {
  beforeEach(() => {
    vi.useFakeTimers();
  });

  afterEach(() => {
    vi.useRealTimers();
  });

  it('starts in closed state', () => {
    const cb = new CircuitBreaker();
    expect(cb.currentState).toBe('closed');
    expect(cb.isOpen()).toBe(false);
  });

  it('stays closed below failure threshold', () => {
    const cb = new CircuitBreaker({ failureThreshold: 3 });
    cb.recordFailure();
    cb.recordFailure();
    expect(cb.currentState).toBe('closed');
    expect(cb.isOpen()).toBe(false);
  });

  it('opens after reaching failure threshold', () => {
    const cb = new CircuitBreaker({ failureThreshold: 3 });
    cb.recordFailure();
    cb.recordFailure();
    cb.recordFailure();
    expect(cb.currentState).toBe('open');
    expect(cb.isOpen()).toBe(true);
  });

  it('recordSuccess resets failure count and closes the circuit', () => {
    const cb = new CircuitBreaker({ failureThreshold: 3 });
    cb.recordFailure();
    cb.recordFailure();
    cb.recordSuccess();
    // After reset, another 2 failures should not open the circuit
    cb.recordFailure();
    cb.recordFailure();
    expect(cb.currentState).toBe('closed');
    expect(cb.isOpen()).toBe(false);
  });

  it('isOpen returns true while reset timeout has not elapsed', () => {
    const cb = new CircuitBreaker({ failureThreshold: 1, resetTimeoutMs: 10_000 });
    cb.recordFailure();
    expect(cb.isOpen()).toBe(true);

    vi.advanceTimersByTime(5_000);
    expect(cb.isOpen()).toBe(true);
  });

  it('transitions from open to half-open after reset timeout', () => {
    const cb = new CircuitBreaker({ failureThreshold: 1, resetTimeoutMs: 10_000 });
    cb.recordFailure();
    expect(cb.currentState).toBe('open');

    vi.advanceTimersByTime(10_000);
    // isOpen() triggers the transition
    expect(cb.isOpen()).toBe(false);
    expect(cb.currentState).toBe('half-open');
  });

  it('closes on success in half-open state', () => {
    const cb = new CircuitBreaker({ failureThreshold: 1, resetTimeoutMs: 10_000 });
    cb.recordFailure();

    vi.advanceTimersByTime(10_000);
    cb.isOpen(); // trigger half-open transition
    expect(cb.currentState).toBe('half-open');

    cb.recordSuccess();
    expect(cb.currentState).toBe('closed');
    expect(cb.isOpen()).toBe(false);
  });

  it('re-opens on failure in half-open state', () => {
    const cb = new CircuitBreaker({ failureThreshold: 1, resetTimeoutMs: 10_000 });
    cb.recordFailure();

    vi.advanceTimersByTime(10_000);
    cb.isOpen(); // trigger half-open transition
    expect(cb.currentState).toBe('half-open');

    cb.recordFailure();
    expect(cb.currentState).toBe('open');
    expect(cb.isOpen()).toBe(true);
  });

  it('uses default options when none provided', () => {
    const cb = new CircuitBreaker();
    // Default threshold is 3
    cb.recordFailure();
    cb.recordFailure();
    expect(cb.isOpen()).toBe(false);
    cb.recordFailure();
    expect(cb.isOpen()).toBe(true);

    // Default timeout is 60_000ms
    vi.advanceTimersByTime(59_999);
    expect(cb.isOpen()).toBe(true);
    vi.advanceTimersByTime(1);
    expect(cb.isOpen()).toBe(false);
  });

  it('opens with threshold of 1', () => {
    const cb = new CircuitBreaker({ failureThreshold: 1 });
    cb.recordFailure();
    expect(cb.currentState).toBe('open');
  });
});
