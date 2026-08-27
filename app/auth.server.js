/**
 * Authentication service for handling OAuth and PKCE flows
 */

/**
 * Generate authorization URL for the customer
 * @param {string} conversationId - The conversation ID to track the auth flow
 * @returns {Promise<Object>} - Object containing the auth URL and conversation ID
 */
export async function generateAuthUrl(conversationId, shopId) {
  const { storeCodeVerifier } = await import('./db.server');

  // Generate authorization URL for the customer
  const clientId = process.env.SHOPIFY_API_KEY;
  const scope = "customer-account-mcp-api:full";
  const responseType = "code";

  // Use the actual app URL for redirect
  const redirectUri = process.env.REDIRECT_URL;

  // Encode the conversation ID and shop ID into an opaque state parameter.
  // Both values are client-influenced and may contain "-" (e.g. a UUID
  // conversation ID), so a plain "-"-joined string would be ambiguous to
  // split back apart; base64url-encoded JSON has no such delimiter clash.
  const state = encodeState({ conversationId, shopId });

  // Generate code verifier and challenge
  const verifier = generateCodeVerifier();
  const challenge = await generateCodeChallenge(verifier);

  // Store the code verifier in the database. If this fails, the callback
  // has no way to complete the PKCE exchange, so fail the initiation
  // rather than handing back an authorization URL that can't work.
  await storeCodeVerifier(state, verifier);

  // Set code_challenge and code_challenge_method parameters
  const codeChallengeMethod = "S256";
  const baseAuthUrl = await getBaseAuthUrl(conversationId);

  if (!baseAuthUrl) {
    throw new Error('Base auth URL not found');
  }

  const authUrlParams = new URLSearchParams({
    client_id: clientId,
    scope,
    redirect_uri: redirectUri,
    response_type: responseType,
    state,
    code_challenge: challenge,
    code_challenge_method: codeChallengeMethod
  });
  const authUrl = `${baseAuthUrl}?${authUrlParams.toString()}`;

  return {
    url: authUrl,
    conversation_id: conversationId
  };
}

/**
 * Encode conversation/shop identifiers into an opaque OAuth state value.
 * @param {{conversationId: string, shopId: string}} payload
 * @returns {string} base64url-encoded JSON state
 */
export function encodeState(payload) {
  return base64UrlEncode(utf8ToBinaryString(JSON.stringify(payload)));
}

/**
 * Decode a state value produced by encodeState.
 * @param {string} state - The OAuth state parameter
 * @returns {{conversationId: string, shopId: string}|null} the decoded payload, or null if malformed
 */
export function decodeState(state) {
  if (!state || typeof state !== "string") return null;
  try {
    const json = binaryStringToUtf8(base64UrlDecode(state));
    const payload = JSON.parse(json);
    if (!payload || typeof payload.conversationId !== "string") return null;
    return payload;
  } catch (error) {
    console.error('Failed to decode OAuth state:', error);
    return null;
  }
}

/**
 * Sign a conversation ID so it can be safely handed to an untrusted client
 * and later verified, without a database lookup, before using it to key
 * reads of that conversation's history/tokens. Keyed by the app's own
 * client secret so no extra configuration is required.
 * @param {string} conversationId
 * @returns {Promise<string>} `${conversationId}.${signatureBase64Url}`
 */
export async function signConversationId(conversationId) {
  const signature = await hmacSign(conversationId);
  return `${conversationId}.${signature}`;
}

/**
 * Verify a token produced by signConversationId.
 * @param {string} token
 * @returns {Promise<string|null>} the conversation ID if the signature is valid, else null
 */
export async function verifyConversationToken(token) {
  if (!token || typeof token !== "string") return null;
  const separatorIndex = token.lastIndexOf(".");
  if (separatorIndex <= 0 || separatorIndex === token.length - 1) return null;

  const conversationId = token.slice(0, separatorIndex);
  const signature = token.slice(separatorIndex + 1);
  const expectedSignature = await hmacSign(conversationId);

  return signature === expectedSignature ? conversationId : null;
}

/**
 * HMAC-SHA256 sign a value with the app's client secret.
 * @param {string} value
 * @returns {Promise<string>} base64url-encoded signature
 */
async function hmacSign(value) {
  const encoder = new TextEncoder();
  const key = await crypto.subtle.importKey(
    "raw",
    encoder.encode(process.env.SHOPIFY_API_SECRET || ""),
    { name: "HMAC", hash: "SHA-256" },
    false,
    ["sign"]
  );
  const signature = await crypto.subtle.sign("HMAC", key, encoder.encode(value));
  return base64UrlEncode(convertBufferToString(signature));
}

/**
 * Get the base auth URL from the customer MCP API URL
 * @param {string} conversationId - The conversation ID to track the auth flow
 * @returns {Promise<string|null>} - The base auth URL or null if not found
 */
async function getBaseAuthUrl(conversationId) {
  const { getCustomerAccountUrls } = await import('./db.server');
  const { authorizationUrl } = await getCustomerAccountUrls(conversationId);

  return authorizationUrl;
}

/**
 * Generate a code verifier for PKCE
 * @returns {string} - The generated code verifier
 */
export function generateCodeVerifier() {
  const array = new Uint8Array(32);
  crypto.getRandomValues(array);
  const randomString = convertBufferToString(array);
  return base64UrlEncode(randomString);
}

/**
 * Generate a code challenge from a verifier
 * @param {string} verifier - The code verifier
 * @returns {Promise<string>} - The generated code challenge
 */
export async function generateCodeChallenge(verifier) {
  const encoder = new TextEncoder();
  const data = encoder.encode(verifier);
  const digestOp = await crypto.subtle.digest('SHA-256', data);
  const hash = convertBufferToString(digestOp);
  return base64UrlEncode(hash);
}

/**
 * Convert a buffer to a string
 * @param {ArrayBuffer} buffer - The buffer to convert
 * @returns {string} - The converted string
 */
function convertBufferToString(buffer) {
  const uintArray = new Uint8Array(buffer);
  const numberArray = Array.from(uintArray);
  return String.fromCharCode.apply(null, numberArray);
}

/**
 * Encode a string in base64url format
 * @param {string} str - The string to encode
 * @returns {string} - The encoded string
 */
function base64UrlEncode(str) {
  // Convert string to base64
  let base64 = btoa(str);

  // Make base64 URL-safe by replacing characters
  base64 = base64.replace(/\+/g, "-")
                 .replace(/\//g, "_")
                 .replace(/=+$/, ""); // Remove any trailing '=' padding

  return base64;
}

/**
 * Decode a base64url string back to a binary string.
 * @param {string} str - The base64url string to decode
 * @returns {string} - The decoded binary string
 */
function base64UrlDecode(str) {
  let base64 = str.replace(/-/g, "+").replace(/_/g, "/");
  while (base64.length % 4) {
    base64 += "=";
  }
  return atob(base64);
}

/**
 * Encode a UTF-8 string as a binary string, for use with base64UrlEncode.
 * @param {string} str - The UTF-8 string to encode
 * @returns {string} - The binary string
 */
function utf8ToBinaryString(str) {
  return convertBufferToString(new TextEncoder().encode(str));
}

/**
 * Decode a binary string (as produced by base64UrlDecode) back to UTF-8.
 * @param {string} binaryString - The binary string to decode
 * @returns {string} - The UTF-8 string
 */
function binaryStringToUtf8(binaryString) {
  const bytes = Uint8Array.from(binaryString, (ch) => ch.charCodeAt(0));
  return new TextDecoder().decode(bytes);
}
