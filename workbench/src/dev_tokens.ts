/** Dev token fixtures — only public fixture values for local dev/test.
 *
 * These are the ONLY source of truth for local dev token values.
 * Workbench reads them; they exist in page memory only, never persisted.
 * Backend validates them at startup via constant-time compare.
 * Production build rejects both values silently.
 */

export interface DevTokens {
  viewerToken: string;
  adminToken: string;
}

export const DEV_TOKENS: DevTokens = {
  viewerToken: "local-test-token-123456789012345678901234567890",
  adminToken: "local-admin-token-123456789012345678901234567890",
};