import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';

import registerPlugin, {
  findLastUserMessage,
  formatMemoryContext,
  resolveConfig,
  safeParseInt,
} from '../src/index';
import type { ConversationMessage } from '../src/types';
import { makeMessages, makeMemoryUnit, makePluginContext, ndjsonResponse, jsonResponse, errorResponse } from './helpers';

// ---------------------------------------------------------------------------
// safeParseInt
// ---------------------------------------------------------------------------

describe('safeParseInt', () => {
  it('parses a valid integer', () => {
    expect(safeParseInt('42', 0)).toBe(42);
  });

  it('returns fallback for NaN input', () => {
    expect(safeParseInt('abc', 99)).toBe(99);
  });

  it('returns fallback for empty string', () => {
    expect(safeParseInt('', 10)).toBe(10);
  });

  it('parses negative numbers', () => {
    expect(safeParseInt('-5', 0)).toBe(-5);
  });

  it('truncates floats to integer', () => {
    expect(safeParseInt('3.14', 0)).toBe(3);
  });
});

// ---------------------------------------------------------------------------
// resolveConfig
// ---------------------------------------------------------------------------

describe('resolveConfig', () => {
  const originalEnv = { ...process.env };

  afterEach(() => {
    process.env = { ...originalEnv };
  });

  it('returns defaults when no env vars are set', () => {
    delete process.env['MEMEX_SERVER_URL'];
    delete process.env['MEMEX_SEARCH_LIMIT'];
    delete process.env['MEMEX_DEFAULT_TAGS'];
    delete process.env['MEMEX_VAULT_ID'];
    delete process.env['MEMEX_BEFORE_TURN_TIMEOUT_MS'];
    delete process.env['MEMEX_MIN_CAPTURE_LENGTH'];

    const config = resolveConfig();

    expect(config.serverUrl).toBe('http://localhost:8000');
    expect(config.searchLimit).toBe(8);
    expect(config.defaultTags).toEqual(['agent', 'openclaw']);
    expect(config.vaultId).toBeNull();
    expect(config.beforeTurnTimeoutMs).toBe(3000);
    expect(config.minCaptureLength).toBe(50);
  });

  it('reads custom env vars', () => {
    process.env['MEMEX_SERVER_URL'] = 'http://custom:9000';
    process.env['MEMEX_SEARCH_LIMIT'] = '20';
    process.env['MEMEX_DEFAULT_TAGS'] = 'alpha, beta';
    process.env['MEMEX_VAULT_ID'] = 'my-vault';
    process.env['MEMEX_BEFORE_TURN_TIMEOUT_MS'] = '5000';
    process.env['MEMEX_MIN_CAPTURE_LENGTH'] = '100';

    const config = resolveConfig();

    expect(config.serverUrl).toBe('http://custom:9000');
    expect(config.searchLimit).toBe(20);
    expect(config.defaultTags).toEqual(['alpha', 'beta']);
    expect(config.vaultId).toBe('my-vault');
    expect(config.beforeTurnTimeoutMs).toBe(5000);
    expect(config.minCaptureLength).toBe(100);
  });

  it('falls back to defaults for invalid integers (NaN fix)', () => {
    process.env['MEMEX_SEARCH_LIMIT'] = 'not-a-number';
    process.env['MEMEX_BEFORE_TURN_TIMEOUT_MS'] = 'xyz';
    process.env['MEMEX_MIN_CAPTURE_LENGTH'] = '';

    const config = resolveConfig();

    expect(config.searchLimit).toBe(8);
    expect(config.beforeTurnTimeoutMs).toBe(3000);
    expect(config.minCaptureLength).toBe(50);
  });

  it('filters empty tags from comma-separated list', () => {
    process.env['MEMEX_DEFAULT_TAGS'] = 'a,,b, ,c';

    const config = resolveConfig();

    expect(config.defaultTags).toEqual(['a', 'b', 'c']);
  });
});

// ---------------------------------------------------------------------------
// findLastUserMessage
// ---------------------------------------------------------------------------

describe('findLastUserMessage', () => {
  it('returns the last user message', () => {
    const msgs = makeMessages(
      { role: 'user', content: 'first' },
      { role: 'assistant', content: 'reply' },
      { role: 'user', content: 'second' },
    );
    expect(findLastUserMessage(msgs)).toBe('second');
  });

  it('returns undefined for empty array', () => {
    expect(findLastUserMessage([])).toBeUndefined();
  });

  it('returns undefined when no user messages', () => {
    const msgs = makeMessages(
      { role: 'assistant', content: 'hello' },
      { role: 'system', content: 'prompt' },
    );
    expect(findLastUserMessage(msgs)).toBeUndefined();
  });

  it('handles single user message', () => {
    const msgs = makeMessages({ role: 'user', content: 'only one' });
    expect(findLastUserMessage(msgs)).toBe('only one');
  });

  it('ignores system and assistant messages', () => {
    const msgs: ConversationMessage[] = [
      { role: 'system', content: 'sys' },
      { role: 'user', content: 'real' },
      { role: 'assistant', content: 'resp' },
    ];
    expect(findLastUserMessage(msgs)).toBe('real');
  });
});

// ---------------------------------------------------------------------------
// formatMemoryContext
// ---------------------------------------------------------------------------

describe('formatMemoryContext', () => {
  it('wraps body in HTML comment markers', () => {
    const result = formatMemoryContext('Some body', 3);
    expect(result).toContain('<!-- MEMEX MEMORY CONTEXT START -->');
    expect(result).toContain('<!-- MEMEX MEMORY CONTEXT END -->');
  });

  it('includes count header', () => {
    const result = formatMemoryContext('body', 5);
    expect(result).toContain('Relevant memories (5 retrieved):');
  });

  it('includes the body text', () => {
    const result = formatMemoryContext('important facts here', 1);
    expect(result).toContain('important facts here');
  });
});

// ---------------------------------------------------------------------------
// registerPlugin & lifecycle hooks
// ---------------------------------------------------------------------------

describe('registerPlugin', () => {
  let fetchSpy: ReturnType<typeof vi.fn>;

  beforeEach(() => {
    fetchSpy = vi.fn();
    vi.stubGlobal('fetch', fetchSpy);
  });

  afterEach(() => {
    vi.unstubAllGlobals();
  });

  it('registers both lifecycle hooks and logs info', async () => {
    const ctx = makePluginContext();

    await registerPlugin(ctx);

    expect(ctx.handlers.beforeTurn).toHaveLength(1);
    expect(ctx.handlers.afterTurn).toHaveLength(1);
    expect(ctx.logger.info).toHaveBeenCalledWith(
      '[memex-openclaw] Memory plugin registered',
    );
  });

  // -----------------------------------------------------------------------
  // beforeTurn
  // -----------------------------------------------------------------------

  describe('beforeTurn', () => {
    it('skips when circuit breaker is open', async () => {
      const ctx = makePluginContext();
      await registerPlugin(ctx);

      // Trip the circuit breaker by failing 3 times
      for (let i = 0; i < 3; i++) {
        fetchSpy.mockRejectedValueOnce(new Error('fail'));
        const msgs = makeMessages({ role: 'user', content: `msg-${i} ${'x'.repeat(60)}` });
        await ctx.handlers.beforeTurn[0]!({
          messages: msgs,
          injectContext: vi.fn(),
        });
      }

      fetchSpy.mockClear();
      const injectContext = vi.fn();
      await ctx.handlers.beforeTurn[0]!({
        messages: makeMessages({ role: 'user', content: 'after breaker open' }),
        injectContext,
      });

      expect(fetchSpy).not.toHaveBeenCalled();
      expect(injectContext).not.toHaveBeenCalled();
      expect(ctx.logger.debug).toHaveBeenCalledWith(
        expect.stringContaining('Circuit breaker open'),
      );
    });

    it('skips when no user message is found', async () => {
      const ctx = makePluginContext();
      await registerPlugin(ctx);
      const injectContext = vi.fn();

      await ctx.handlers.beforeTurn[0]!({
        messages: makeMessages({ role: 'assistant', content: 'no user' }),
        injectContext,
      });

      expect(fetchSpy).not.toHaveBeenCalled();
      expect(injectContext).not.toHaveBeenCalled();
    });

    it('injects indexed memory texts directly from search results', async () => {
      const ctx = makePluginContext();
      await registerPlugin(ctx);

      const m1 = makeMemoryUnit({ text: 'fact 1' });
      const m2 = makeMemoryUnit({ text: 'fact 2' });
      fetchSpy.mockResolvedValueOnce(ndjsonResponse([m1, m2]));

      const injectContext = vi.fn();
      await ctx.handlers.beforeTurn[0]!({
        messages: makeMessages({ role: 'user', content: 'Tell me what you know' }),
        injectContext,
      });

      expect(injectContext).toHaveBeenCalledOnce();
      const injected = injectContext.mock.calls[0]![0] as string;
      expect(injected).toContain('[0] fact 1');
      expect(injected).toContain('[1] fact 2');
      expect(injected).toContain('2 retrieved');
    });

    it('does not inject context when search returns empty', async () => {
      const ctx = makePluginContext();
      await registerPlugin(ctx);
      fetchSpy.mockResolvedValueOnce(ndjsonResponse([]));

      const injectContext = vi.fn();
      await ctx.handlers.beforeTurn[0]!({
        messages: makeMessages({ role: 'user', content: 'hello' }),
        injectContext,
      });

      expect(injectContext).not.toHaveBeenCalled();
    });

    it('logs error and records failure on search exception', async () => {
      const ctx = makePluginContext();
      await registerPlugin(ctx);
      fetchSpy.mockRejectedValueOnce(new Error('connection refused'));

      const injectContext = vi.fn();
      await ctx.handlers.beforeTurn[0]!({
        messages: makeMessages({ role: 'user', content: 'question' }),
        injectContext,
      });

      expect(injectContext).not.toHaveBeenCalled();
      expect(ctx.logger.error).toHaveBeenCalledWith(
        expect.stringContaining('beforeTurn recall failed'),
      );
    });

    it('records success after successful search and inject', async () => {
      const ctx = makePluginContext();
      await registerPlugin(ctx);

      const m = makeMemoryUnit({ text: 'fact' });
      fetchSpy.mockResolvedValueOnce(ndjsonResponse([m]));

      await ctx.handlers.beforeTurn[0]!({
        messages: makeMessages({ role: 'user', content: 'query' }),
        injectContext: vi.fn(),
      });

      // Verify breaker didn't open by doing another call that succeeds
      fetchSpy.mockResolvedValueOnce(ndjsonResponse([]));
      await ctx.handlers.beforeTurn[0]!({
        messages: makeMessages({ role: 'user', content: 'another' }),
        injectContext: vi.fn(),
      });

      // Should have called fetch again (not blocked by breaker)
      expect(fetchSpy).toHaveBeenCalledTimes(2);
    });
  });

  // -----------------------------------------------------------------------
  // afterTurn
  // -----------------------------------------------------------------------

  describe('afterTurn', () => {
    it('captures conversation with ingestNote', async () => {
      const ctx = makePluginContext();
      await registerPlugin(ctx);
      fetchSpy.mockResolvedValueOnce(jsonResponse({}, 202));

      const longMsg = 'A'.repeat(60); // > minCaptureLength default of 50
      await ctx.handlers.afterTurn[0]!({
        messages: makeMessages({ role: 'user', content: longMsg }),
        response: 'AI response here',
      });

      await vi.waitFor(() => {
        expect(fetchSpy).toHaveBeenCalledOnce();
      });

      const [url, init] = fetchSpy.mock.calls[0]!;
      expect(url).toContain('/api/v1/ingestions?background=true');
      const body = JSON.parse(init.body);
      expect(body.name).toMatch(/^Conversation —/);
      expect(body.tags).toEqual(['agent', 'openclaw']);
      expect(body.content).toBeTruthy();
    });

    it('skips capture when message is too short', async () => {
      const ctx = makePluginContext();
      await registerPlugin(ctx);

      await ctx.handlers.afterTurn[0]!({
        messages: makeMessages({ role: 'user', content: 'hi' }), // < 50 chars
        response: 'hello',
      });

      expect(fetchSpy).not.toHaveBeenCalled();
      expect(ctx.logger.debug).toHaveBeenCalledWith(
        expect.stringContaining('Message too short'),
      );
    });

    it('skips when no user message exists', async () => {
      const ctx = makePluginContext();
      await registerPlugin(ctx);

      await ctx.handlers.afterTurn[0]!({
        messages: makeMessages({ role: 'assistant', content: 'solo' }),
        response: 'resp',
      });

      expect(fetchSpy).not.toHaveBeenCalled();
    });

    it('skips when circuit breaker is open', async () => {
      const ctx = makePluginContext();
      await registerPlugin(ctx);

      // Trip the breaker via beforeTurn failures
      for (let i = 0; i < 3; i++) {
        fetchSpy.mockRejectedValueOnce(new Error('fail'));
        await ctx.handlers.beforeTurn[0]!({
          messages: makeMessages({ role: 'user', content: `msg-${i} ${'x'.repeat(60)}` }),
          injectContext: vi.fn(),
        });
      }

      fetchSpy.mockClear();
      await ctx.handlers.afterTurn[0]!({
        messages: makeMessages({ role: 'user', content: 'A'.repeat(60) }),
        response: 'resp',
      });

      expect(fetchSpy).not.toHaveBeenCalled();
      expect(ctx.logger.debug).toHaveBeenCalledWith(
        expect.stringContaining('Circuit breaker open — skipping capture'),
      );
    });

    it('does not propagate errors from capture', async () => {
      const ctx = makePluginContext();
      await registerPlugin(ctx);

      // Make ingestNote's internal fetch throw
      fetchSpy.mockRejectedValueOnce(new Error('network error'));

      const longMsg = 'B'.repeat(60);
      // Should not throw
      await ctx.handlers.afterTurn[0]!({
        messages: makeMessages({ role: 'user', content: longMsg }),
        response: 'resp',
      });

      // No error propagated — the function completes normally
    });
  });
});
