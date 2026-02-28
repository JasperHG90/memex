import type { z } from 'zod';

/**
 * Validate and parse API response data with a Zod schema.
 * In development mode, validation errors are logged as warnings
 * but the raw data is still returned to avoid breaking the UI.
 * In production, raw data is returned without validation overhead.
 */
export function validateResponse<T>(schema: z.ZodType<T>, data: unknown): T {
  if (import.meta.env.PROD) {
    return data as T;
  }

  const result = schema.safeParse(data);
  if (!result.success) {
    console.warn(
      '[API Validation] Response does not match schema:',
      result.error.issues,
    );
    return data as T;
  }
  return result.data;
}

/**
 * Create a validated fetcher that wraps an API call with Zod validation.
 * Use in TanStack Query queryFn or mutationFn.
 */
export function validated<T>(schema: z.ZodType<T>, fetcher: () => Promise<unknown>): () => Promise<T> {
  return async () => {
    const data = await fetcher();
    return validateResponse(schema, data);
  };
}

/**
 * Validate an array response against an item schema.
 */
export function validateArrayResponse<T>(schema: z.ZodType<T>, data: unknown): T[] {
  if (!Array.isArray(data)) {
    console.warn('[API Validation] Expected array response, got:', typeof data);
    return data as T[];
  }

  if (import.meta.env.PROD) {
    return data as T[];
  }

  return data.map((item, index) => {
    const result = schema.safeParse(item);
    if (!result.success) {
      console.warn(
        `[API Validation] Array item [${index}] does not match schema:`,
        result.error.issues,
      );
      return item as T;
    }
    return result.data;
  });
}
