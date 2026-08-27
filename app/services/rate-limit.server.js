/**
 * Rate Limit Service
 * Minimal in-memory, fixed-window request limiter for a single process.
 * Not a substitute for edge/CDN-level rate limiting across multiple
 * instances, but bounds the cost of unauthenticated abuse of a single
 * running server.
 */
import AppConfig from "./config.server";

const requestWindows = new Map();

/**
 * Derive a best-effort client identity from proxy headers, falling back
 * to a constant key (which degrades to a single shared limit) if none
 * of the usual proxy headers are present.
 * @param {Request} request
 * @returns {string}
 */
function getClientKey(request) {
  const forwardedFor = request.headers.get("X-Forwarded-For");
  return (
    request.headers.get("CF-Connecting-IP") ||
    forwardedFor?.split(",")[0]?.trim() ||
    request.headers.get("X-Real-IP") ||
    "unknown-client"
  );
}

/**
 * Check whether the given request should be throttled, recording this
 * call towards its client's current window as a side effect.
 * @param {Request} request
 * @returns {boolean} true if the request should be rejected
 */
export function isRateLimited(request) {
  const key = getClientKey(request);
  const now = Date.now();
  const { windowMs, maxRequestsPerWindow } = AppConfig.rateLimit;

  const entry = requestWindows.get(key);
  if (!entry || now - entry.windowStart > windowMs) {
    requestWindows.set(key, { count: 1, windowStart: now });
    pruneExpiredWindows(now, windowMs);
    return false;
  }

  entry.count += 1;
  return entry.count > maxRequestsPerWindow;
}

/**
 * Periodically sweep expired windows so clients that stop sending
 * requests don't accumulate in memory indefinitely.
 */
function pruneExpiredWindows(now, windowMs) {
  if (requestWindows.size < 1000) return;
  for (const [key, entry] of requestWindows) {
    if (now - entry.windowStart > windowMs) {
      requestWindows.delete(key);
    }
  }
}
