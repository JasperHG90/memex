import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import { safeParseInt, parseConfig, resolveConfig } from '../src/config';

describe('safeParseInt', () => {
  it('returns fallback when value is undefined', () => {
    expect(safeParseInt(undefined, 42)).toBe(42);
  });

  it('parses a valid integer string', () => {
    expect(safeParseInt('10', 0)).toBe(10);
  });

  it('returns fallback for non-numeric string', () => {
    expect(safeParseInt('abc', 7)).toBe(7);
  });

  it('returns fallback for empty string', () => {
    expect(safeParseInt('', 99)).toBe(99);
  });

  it('truncates floating-point strings to integer', () => {
    expect(safeParseInt('3.14', 0)).toBe(3);
  });
});

describe('parseConfig', () => {
  const originalEnv = process.env;

  beforeEach(() => {
    process.env = { ...originalEnv };
  });

  afterEach(() => {
    process.env = originalEnv;
  });

  it('returns defaults when called with no arguments', () => {
    const cfg = parseConfig();
    expect(cfg.serverUrl).toBe('http://localhost:8000');
    expect(cfg.searchLimit).toBe(8);
    expect(cfg.tokenBudget).toBeNull();
    expect(cfg.defaultTags).toEqual(['agent', 'openclaw']);
    expect(cfg.vaultId).toBeNull();
    expect(cfg.vaultName).toBe('OpenClaw');
    expect(cfg.timeoutMs).toBe(5000);
    expect(cfg.beforeTurnTimeoutMs).toBe(5000);
    expect(cfg.minCaptureLength).toBe(50);
    expect(cfg.autoRecall).toBe(true);
    expect(cfg.autoCapture).toBe(true);
    expect(cfg.profileFrequency).toBe(20);
    expect(cfg.captureMode).toBe('filtered');
    expect(cfg.sessionGrouping).toBe(true);
  });

  it('uses explicit config values over defaults', () => {
    const cfg = parseConfig({
      serverUrl: 'http://custom:9000',
      searchLimit: 20,
      defaultTags: 'foo,bar',
      vaultId: 'vault-1',
      vaultName: 'CustomVault',
      timeoutMs: 10000,
      minCaptureLength: 100,
      autoRecall: false,
      autoCapture: false,
    });
    expect(cfg.serverUrl).toBe('http://custom:9000');
    expect(cfg.searchLimit).toBe(20);
    expect(cfg.defaultTags).toEqual(['foo', 'bar']);
    expect(cfg.vaultId).toBe('vault-1');
    expect(cfg.vaultName).toBe('CustomVault');
    expect(cfg.timeoutMs).toBe(10000);
    expect(cfg.beforeTurnTimeoutMs).toBe(10000);
    expect(cfg.minCaptureLength).toBe(100);
    expect(cfg.autoRecall).toBe(false);
    expect(cfg.autoCapture).toBe(false);
  });

  it('strips trailing slash from serverUrl', () => {
    const cfg = parseConfig({ serverUrl: 'http://host:8000/' });
    expect(cfg.serverUrl).toBe('http://host:8000');
  });

  it('reads from environment variables when config values are absent', () => {
    process.env['MEMEX_SERVER_URL'] = 'http://env-host:3000/';
    process.env['MEMEX_SEARCH_LIMIT'] = '15';
    process.env['MEMEX_DEFAULT_TAGS'] = 'env-tag1, env-tag2';
    process.env['MEMEX_VAULT_ID'] = 'env-vault';
    process.env['MEMEX_VAULT_NAME'] = 'EnvVault';
    process.env['MEMEX_BEFORE_TURN_TIMEOUT_MS'] = '7000';
    process.env['MEMEX_MIN_CAPTURE_LENGTH'] = '200';

    const cfg = parseConfig();
    expect(cfg.serverUrl).toBe('http://env-host:3000');
    expect(cfg.searchLimit).toBe(15);
    expect(cfg.defaultTags).toEqual(['env-tag1', 'env-tag2']);
    expect(cfg.vaultId).toBe('env-vault');
    expect(cfg.vaultName).toBe('EnvVault');
    expect(cfg.timeoutMs).toBe(7000);
    expect(cfg.minCaptureLength).toBe(200);
  });

  it('explicit config takes precedence over env vars', () => {
    process.env['MEMEX_SERVER_URL'] = 'http://env-host:3000';
    const cfg = parseConfig({ serverUrl: 'http://explicit:4000' });
    expect(cfg.serverUrl).toBe('http://explicit:4000');
  });

  it('handles null and non-object raw values gracefully', () => {
    expect(parseConfig(null).serverUrl).toBe('http://localhost:8000');
    expect(parseConfig(42).serverUrl).toBe('http://localhost:8000');
    expect(parseConfig('string').serverUrl).toBe('http://localhost:8000');
  });

  it('filters empty tags from comma-separated string', () => {
    const cfg = parseConfig({ defaultTags: ',a,,b,' });
    expect(cfg.defaultTags).toEqual(['a', 'b']);
  });

  it('sets beforeTurnTimeoutMs equal to timeoutMs', () => {
    const cfg = parseConfig({ timeoutMs: 12345 });
    expect(cfg.beforeTurnTimeoutMs).toBe(cfg.timeoutMs);
    expect(cfg.beforeTurnTimeoutMs).toBe(12345);
  });

  it('uses explicit tokenBudget config value', () => {
    const cfg = parseConfig({ tokenBudget: 3000 });
    expect(cfg.tokenBudget).toBe(3000);
  });

  it('reads tokenBudget from MEMEX_TOKEN_BUDGET env var', () => {
    process.env['MEMEX_TOKEN_BUDGET'] = '4000';
    const cfg = parseConfig();
    expect(cfg.tokenBudget).toBe(4000);
  });

  it('explicit tokenBudget config takes precedence over env var', () => {
    process.env['MEMEX_TOKEN_BUDGET'] = '4000';
    const cfg = parseConfig({ tokenBudget: 5000 });
    expect(cfg.tokenBudget).toBe(5000);
  });

  it('returns null tokenBudget for non-numeric env var', () => {
    process.env['MEMEX_TOKEN_BUDGET'] = 'abc';
    const cfg = parseConfig();
    expect(cfg.tokenBudget).toBeNull();
  });
});

describe('parseConfig — profileFrequency', () => {
  const originalEnv = process.env;

  beforeEach(() => {
    process.env = { ...originalEnv };
  });

  afterEach(() => {
    process.env = originalEnv;
  });

  it('defaults to 20', () => {
    const cfg = parseConfig();
    expect(cfg.profileFrequency).toBe(20);
  });

  it('uses explicit config value', () => {
    const cfg = parseConfig({ profileFrequency: 100 });
    expect(cfg.profileFrequency).toBe(100);
  });

  it('reads from MEMEX_PROFILE_FREQUENCY env var', () => {
    process.env['MEMEX_PROFILE_FREQUENCY'] = '50';
    const cfg = parseConfig();
    expect(cfg.profileFrequency).toBe(50);
  });

  it('falls back to default for invalid env var', () => {
    process.env['MEMEX_PROFILE_FREQUENCY'] = 'abc';
    const cfg = parseConfig();
    expect(cfg.profileFrequency).toBe(20);
  });

  it('explicit config takes precedence over env var', () => {
    process.env['MEMEX_PROFILE_FREQUENCY'] = '50';
    const cfg = parseConfig({ profileFrequency: 75 });
    expect(cfg.profileFrequency).toBe(75);
  });
});

describe('parseConfig — captureMode', () => {
  const originalEnv = process.env;

  beforeEach(() => {
    process.env = { ...originalEnv };
  });

  afterEach(() => {
    process.env = originalEnv;
  });

  it('defaults to filtered', () => {
    const cfg = parseConfig();
    expect(cfg.captureMode).toBe('filtered');
  });

  it('uses explicit config value', () => {
    const cfg = parseConfig({ captureMode: 'full' });
    expect(cfg.captureMode).toBe('full');
  });

  it('reads from MEMEX_CAPTURE_MODE env var', () => {
    process.env['MEMEX_CAPTURE_MODE'] = 'full';
    const cfg = parseConfig();
    expect(cfg.captureMode).toBe('full');
  });

  it('falls back to filtered for invalid value', () => {
    process.env['MEMEX_CAPTURE_MODE'] = 'bogus';
    const cfg = parseConfig();
    expect(cfg.captureMode).toBe('filtered');
  });

  it('explicit config takes precedence over env var', () => {
    process.env['MEMEX_CAPTURE_MODE'] = 'filtered';
    const cfg = parseConfig({ captureMode: 'full' });
    expect(cfg.captureMode).toBe('full');
  });
});

describe('parseConfig — sessionGrouping', () => {
  const originalEnv = process.env;

  beforeEach(() => {
    process.env = { ...originalEnv };
  });

  afterEach(() => {
    process.env = originalEnv;
  });

  it('defaults to true', () => {
    const cfg = parseConfig();
    expect(cfg.sessionGrouping).toBe(true);
  });

  it('uses explicit config value', () => {
    const cfg = parseConfig({ sessionGrouping: false });
    expect(cfg.sessionGrouping).toBe(false);
  });

  it('reads from MEMEX_SESSION_GROUPING env var', () => {
    process.env['MEMEX_SESSION_GROUPING'] = 'true';
    const cfg = parseConfig();
    expect(cfg.sessionGrouping).toBe(true);
  });

  it('treats non-true env var as false', () => {
    process.env['MEMEX_SESSION_GROUPING'] = 'false';
    const cfg = parseConfig();
    expect(cfg.sessionGrouping).toBe(false);
  });

  it('explicit config takes precedence over env var', () => {
    process.env['MEMEX_SESSION_GROUPING'] = 'false';
    const cfg = parseConfig({ sessionGrouping: true });
    expect(cfg.sessionGrouping).toBe(true);
  });
});

describe('resolveConfig', () => {
  const originalEnv = process.env;

  beforeEach(() => {
    process.env = { ...originalEnv };
  });

  afterEach(() => {
    process.env = originalEnv;
  });

  it('resolves config from environment only', () => {
    process.env['MEMEX_SERVER_URL'] = 'http://resolve:5000';
    const cfg = resolveConfig();
    expect(cfg.serverUrl).toBe('http://resolve:5000');
  });

  it('returns defaults when no env vars are set', () => {
    delete process.env['MEMEX_SERVER_URL'];
    delete process.env['MEMEX_SEARCH_LIMIT'];
    delete process.env['MEMEX_DEFAULT_TAGS'];
    delete process.env['MEMEX_VAULT_ID'];
    delete process.env['MEMEX_VAULT_NAME'];
    delete process.env['MEMEX_BEFORE_TURN_TIMEOUT_MS'];
    delete process.env['MEMEX_MIN_CAPTURE_LENGTH'];

    const cfg = resolveConfig();
    expect(cfg.serverUrl).toBe('http://localhost:8000');
    expect(cfg.searchLimit).toBe(8);
  });
});
