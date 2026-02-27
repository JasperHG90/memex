/**
 * Type declarations for the OpenClaw Plugin SDK.
 *
 * These types describe the plugin API surface that OpenClaw provides at
 * runtime. The actual implementation lives in the `openclaw` package (peer
 * dependency). This declaration file allows TypeScript to compile the plugin
 * without requiring the peer dependency to be installed.
 */
declare module 'openclaw/plugin-sdk' {
  /* eslint-disable @typescript-eslint/no-explicit-any */

  /** Commander-like CLI builder. */
  interface CliCommand {
    command(name: string): CliCommand;
    description(desc: string): CliCommand;
    argument(name: string, desc: string): CliCommand;
    option(flags: string, desc: string, defaultValue?: string): CliCommand;
    action(fn: (...args: any[]) => Promise<void> | void): CliCommand;
  }

  export interface OpenClawPluginApi {
    pluginConfig: unknown;

    logger: {
      debug?: (msg: string) => void;
      info: (msg: string) => void;
      warn?: (msg: string) => void;
      error?: (msg: string) => void;
    };

    registerTool(
      definition: {
        name: string;
        label: string;
        description: string;
        parameters: unknown;
        execute(toolCallId: string, params: unknown): Promise<{
          content: Array<{ type: string; text: string }>;
          details?: Record<string, unknown>;
        }>;
      },
      meta: { name: string },
    ): void;

    registerCommand(definition: {
      name: string;
      description: string;
      acceptsArgs: boolean;
      requireAuth: boolean;
      handler(ctx: { args?: string }): Promise<{ text: string }>;
    }): void;

    on(
      event: string,
      handler: (event: any) => Promise<unknown>,
    ): void;

    registerCli(
      fn: (ctx: { program: CliCommand }) => void,
      meta: { commands: string[] },
    ): void;

    registerService(definition: {
      id: string;
      start: () => void;
      stop: () => void;
    }): void;
  }

  /* eslint-enable @typescript-eslint/no-explicit-any */
}
