import { clsx, type ClassValue } from "clsx"
import { twMerge } from "tailwind-merge"

export function cn(...inputs: ClassValue[]) {
  return twMerge(clsx(inputs))
}

/**
 * Encode a string to base64, supporting non-ASCII (Unicode) characters.
 * Uses TextEncoder to handle multi-byte characters that btoa() cannot.
 */
export function encodeBase64(input: string): string {
  const bytes = new TextEncoder().encode(input)
  return btoa(String.fromCharCode(...bytes))
}
