/**
 * The product's user-facing name.
 *
 * Kept in one module so the stylised form cannot drift between the wordmark,
 * the transcript, the status bar, and the documentation reader. The Python side
 * has the matching constant in `daino/branding.py`, where the brackets also
 * have to survive console markup; in the browser they are just characters.
 */
export const BRAND = "D[Ai]NO";
