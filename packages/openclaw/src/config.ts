import type { CaptureMode, PluginConfig } from './types';

/** Parse an integer from a string, returning `fallback` if the result is NaN. */
export function safeParseInt(value: string | undefined, fallback: number): number {
  if (value == null) return fallback;
  const parsed = parseInt(value, 10);
  return Number.isNaN(parsed) ? fallback : parsed;
}

/**
 * Parse plugin configuration from an OpenClaw config object,
 * falling back to environment variables, then defaults.
 */
export function parseConfig(raw?: unknown): PluginConfig {
  const cfg = (raw && typeof raw === 'object' ? raw : {}) as Record<string, unknown>;

  const serverUrl = (
    typeof cfg.serverUrl === 'string'
      ? cfg.serverUrl
      : process.env['MEMEX_SERVER_URL'] ?? 'http://localhost:8000'
  ).replace(/\/$/, '');

  const searchLimit =
    typeof cfg.searchLimit === 'number'
      ? cfg.searchLimit
      : safeParseInt(process.env['MEMEX_SEARCH_LIMIT'], 8);

  const tokenBudgetEnv = process.env['MEMEX_TOKEN_BUDGET'];
  const tokenBudget =
    typeof cfg.tokenBudget === 'number'
      ? cfg.tokenBudget
      : tokenBudgetEnv != null
        ? (Number.isNaN(parseInt(tokenBudgetEnv, 10)) ? null : parseInt(tokenBudgetEnv, 10))
        : null;

  const tagsRaw =
    typeof cfg.defaultTags === 'string'
      ? cfg.defaultTags
      : process.env['MEMEX_DEFAULT_TAGS'] ?? 'agent,openclaw';
  const defaultTags = tagsRaw.split(',').map((t: string) => t.trim()).filter(Boolean);

  const vaultId =
    typeof cfg.vaultId === 'string'
      ? cfg.vaultId
      : process.env['MEMEX_VAULT_ID'] ?? null;

  const vaultName =
    typeof cfg.vaultName === 'string'
      ? cfg.vaultName
      : process.env['MEMEX_VAULT_NAME'] ?? 'OpenClaw';

  const timeoutMs =
    typeof cfg.timeoutMs === 'number'
      ? cfg.timeoutMs
      : safeParseInt(process.env['MEMEX_BEFORE_TURN_TIMEOUT_MS'], 5000);

  const minCaptureLength =
    typeof cfg.minCaptureLength === 'number'
      ? cfg.minCaptureLength
      : safeParseInt(process.env['MEMEX_MIN_CAPTURE_LENGTH'], 50);

  const autoRecall = cfg.autoRecall !== false;
  const autoCapture = cfg.autoCapture !== false;

  const profileFrequency =
    typeof cfg.profileFrequency === 'number'
      ? cfg.profileFrequency
      : safeParseInt(process.env['MEMEX_PROFILE_FREQUENCY'], 20);

  const captureModeRaw =
    typeof cfg.captureMode === 'string'
      ? cfg.captureMode
      : process.env['MEMEX_CAPTURE_MODE'] ?? 'filtered';
  const captureMode: CaptureMode = captureModeRaw === 'full' ? 'full' : 'filtered';

  const sessionGroupingEnv = process.env['MEMEX_SESSION_GROUPING'];
  const sessionGrouping =
    typeof cfg.sessionGrouping === 'boolean'
      ? cfg.sessionGrouping
      : sessionGroupingEnv != null
        ? sessionGroupingEnv === 'true'
        : true;

  return {
    serverUrl,
    searchLimit,
    tokenBudget,
    defaultTags,
    vaultId,
    vaultName,
    timeoutMs,
    minCaptureLength,
    autoRecall,
    autoCapture,
    beforeTurnTimeoutMs: timeoutMs,
    profileFrequency,
    captureMode,
    sessionGrouping,
  };
}

/** Resolve config from environment variables only (no plugin config object). */
export function resolveConfig(): PluginConfig {
  return parseConfig();
}
