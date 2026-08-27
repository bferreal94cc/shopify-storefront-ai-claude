/**
 * Configuration Service
 * Centralizes all configuration values for the chat service
 */

export const AppConfig = {
  // API Configuration
  api: {
    defaultModel: 'claude-sonnet-4-20250514',
    maxTokens: 2000,
    defaultPromptType: 'standardAssistant',
    // Cap how much stored history is replayed to Claude on each turn, so a
    // long-lived conversation can't grow latency/cost without bound or
    // eventually exceed the model's context window.
    maxHistoryMessages: 40,
  },

  // Basic per-client throttling for the public /chat endpoint, to bound
  // the cost of unauthenticated abuse. A single-process, in-memory limiter
  // is not a substitute for edge/CDN-level rate limiting in production.
  rateLimit: {
    windowMs: 60_000,
    maxRequestsPerWindow: 20,
  },

  // Error Message Templates
  errorMessages: {
    missingMessage: "Message is required",
    apiUnsupported: "This endpoint only supports server-sent events (SSE) requests or history requests.",
    authFailed: "Authentication failed with Claude API",
    apiKeyError: "Please check your API key in environment variables",
    rateLimitExceeded: "Rate limit exceeded",
    rateLimitDetails: "Please try again later",
    genericError: "Failed to get response from Claude"
  },

  // Tool Configuration
  tools: {
    productSearchName: "search_shop_catalog",
    maxProductsToDisplay: 3
  }
};

export default AppConfig;
