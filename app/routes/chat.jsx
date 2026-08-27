/**
 * Chat API Route
 * Handles chat interactions with Claude API and tools
 */
import MCPClient from "../mcp-client";
import {
  saveMessage,
  getConversationHistory,
  storeCustomerAccountUrls,
  getCustomerAccountUrls as getCustomerAccountUrlsFromDb,
} from "../db.server";
import { signConversationId, verifyConversationToken } from "../auth.server";
import { isRateLimited } from "../services/rate-limit.server";
import AppConfig from "../services/config.server";
import { createSseStream } from "../services/streaming.server";
import { createClaudeService } from "../services/claude.server";
import { createToolService } from "../services/tool.server";

/**
 * React Router loader function for handling GET requests
 */
export async function loader({ request }) {
  // Handle OPTIONS requests (CORS preflight)
  if (request.method === "OPTIONS") {
    return new Response(null, {
      status: 204,
      headers: getCorsHeaders(request),
    });
  }

  const url = new URL(request.url);

  // Handle history fetch requests - matches /chat?history=true&conversation_id=XYZ
  if (
    url.searchParams.has("history") &&
    url.searchParams.has("conversation_id")
  ) {
    return handleHistoryRequest(
      request,
      url.searchParams.get("conversation_id"),
    );
  }

  // Chat itself is POST-only (it reads a JSON body); GET only ever serves
  // history or CORS preflight above.
  return new Response(
    JSON.stringify({ error: AppConfig.errorMessages.apiUnsupported }),
    { status: 400, headers: getCorsHeaders(request) },
  );
}

/**
 * React Router action function for handling POST requests
 */
export async function action({ request }) {
  return handleChatRequest(request);
}

/**
 * Handle history fetch requests
 * @param {Request} request - The request object
 * @param {string} conversationToken - The signed conversation ID from the client
 * @returns {Response} JSON response with chat history
 */
async function handleHistoryRequest(request, conversationToken) {
  // Reject unsigned/tampered/guessed conversation IDs before reading
  // anything, so a leaked or enumerated ID alone can't expose another
  // shopper's chat history.
  const conversationId = await verifyConversationToken(conversationToken);
  if (!conversationId) {
    return new Response(JSON.stringify({ messages: [] }), {
      headers: getCorsHeaders(request),
    });
  }

  const messages = await getConversationHistory(conversationId);

  return new Response(JSON.stringify({ messages }), {
    headers: getCorsHeaders(request),
  });
}

/**
 * Handle chat requests (both GET and POST)
 * @param {Request} request - The request object
 * @returns {Response} Server-sent events stream
 */
async function handleChatRequest(request) {
  // Every call here triggers an Anthropic request and MCP calls, with no
  // other gate on this public endpoint; throttle per-client before doing
  // any work.
  if (isRateLimited(request)) {
    return new Response(
      JSON.stringify({ error: AppConfig.errorMessages.rateLimitExceeded }),
      { status: 429, headers: getSseHeaders(request) },
    );
  }

  try {
    // Get message data from request body
    const body = await request.json();
    const userMessage = body.message;

    // Validate required message: truthiness alone accepts whitespace,
    // objects and arrays, none of which Prisma or the Claude API can use.
    if (typeof userMessage !== "string" || userMessage.trim().length === 0) {
      return new Response(
        JSON.stringify({ error: AppConfig.errorMessages.missingMessage }),
        { status: 400, headers: getSseHeaders(request) },
      );
    }

    // Use the client's conversation ID only if it carries a valid
    // signature; otherwise mint and sign a fresh, unguessable one. This
    // also means a client can never graft its messages onto someone
    // else's conversation by guessing or reusing a stray ID.
    const verifiedConversationId = await verifyConversationToken(
      body.conversation_id,
    );
    const conversationId = verifiedConversationId || crypto.randomUUID();
    const promptType = body.prompt_type || AppConfig.api.defaultPromptType;

    // Create a stream for the response
    const responseStream = createSseStream(async (stream) => {
      await handleChatSession({
        request,
        userMessage,
        conversationId,
        promptType,
        stream,
      });
    });

    return new Response(responseStream, {
      headers: getSseHeaders(request),
    });
  } catch (error) {
    console.error("Error in chat request handler:", error);
    return new Response(JSON.stringify({ error: error.message }), {
      status: 500,
      headers: getCorsHeaders(request),
    });
  }
}

/**
 * Handle a complete chat session
 * @param {Object} params - Session parameters
 * @param {Request} params.request - The request object
 * @param {string} params.userMessage - The user's message
 * @param {string} params.conversationId - The conversation ID
 * @param {string} params.promptType - The prompt type
 * @param {Object} params.stream - Stream manager for sending responses
 */
async function handleChatSession({
  request,
  userMessage,
  conversationId,
  promptType,
  stream,
}) {
  // Initialize services
  const claudeService = createClaudeService();
  const toolService = createToolService();

  // Initialize MCP client. shopDomain comes from the client-supplied
  // Origin header, so only trust it as a real Shopify storefront domain -
  // it's about to be used to build a server-side fetch URL.
  const shopId = request.headers.get("X-Shopify-Shop-Id");
  const rawOrigin = request.headers.get("Origin");
  const shopDomain = isTrustedShopOrigin(rawOrigin) ? rawOrigin : null;
  const { mcpApiUrl } =
    (shopDomain
      ? await getCustomerAccountUrls(shopDomain, conversationId)
      : null) || {};

  const mcpClient = new MCPClient(
    shopDomain,
    conversationId,
    shopId,
    mcpApiUrl,
  );

  // Send the signed conversation ID to the client; this is the token it
  // must round-trip on future history/chat/token-status requests.
  stream.sendMessage({
    type: "id",
    conversation_id: await signConversationId(conversationId),
  });

  // Connect to MCP servers and get available tools
  let storefrontMcpTools = [],
    customerMcpTools = [];

  try {
    storefrontMcpTools = await mcpClient.connectToStorefrontServer();
    customerMcpTools = await mcpClient.connectToCustomerServer();

    console.log(`Connected to MCP with ${storefrontMcpTools.length} tools`);
    console.log(
      `Connected to customer MCP with ${customerMcpTools.length} tools`,
    );
  } catch (error) {
    console.warn(
      "Failed to connect to MCP servers, continuing without tools:",
      error.message,
    );
  }

  // Prepare conversation state
  let conversationHistory = [];
  let productsToDisplay = [];

  // Save user message to the database
  await saveMessage(conversationId, "user", userMessage);

  // Fetch messages from the database for this conversation. Cap how many
  // are replayed to Claude so a long-lived conversation doesn't grow
  // latency/cost without bound or eventually exceed the context window;
  // the full history is still persisted in the database above.
  const dbMessages = await getConversationHistory(conversationId);
  const recentDbMessages = dbMessages.slice(-AppConfig.api.maxHistoryMessages);

  // Format messages for Claude API
  conversationHistory = recentDbMessages.map((dbMessage) => {
    let content;
    try {
      content = JSON.parse(dbMessage.content);
    } catch (e) {
      content = dbMessage.content;
    }
    return {
      role: dbMessage.role,
      content,
    };
  });

  // Execute the conversation stream. Only a tool_use turn should loop
  // back for another round; any other terminal stop reason (end_turn,
  // max_tokens, stop_sequence, ...) means Claude is done responding, and
  // resubmitting the same history in a loop would just accrue cost.
  let finalMessage = {
    role: "user",
    content: userMessage,
    stop_reason: "tool_use",
  };

  while (finalMessage.stop_reason === "tool_use") {
    finalMessage = await claudeService.streamConversation(
      {
        messages: conversationHistory,
        promptType,
        tools: mcpClient.tools,
      },
      {
        // Handle text chunks
        onText: (textDelta) => {
          stream.sendMessage({
            type: "chunk",
            chunk: textDelta,
          });
        },

        // Handle complete messages
        onMessage: (message) => {
          conversationHistory.push({
            role: message.role,
            content: message.content,
          });

          saveMessage(
            conversationId,
            message.role,
            JSON.stringify(message.content),
          ).catch((error) => {
            console.error("Error saving message to database:", error);
          });

          // Send a completion message
          stream.sendMessage({ type: "message_complete" });
        },

        // Handle tool use requests
        onToolUse: async (content) => {
          const toolName = content.name;
          const toolArgs = content.input;
          const toolUseId = content.id;

          const toolUseMessage = `Calling tool: ${toolName} with arguments: ${JSON.stringify(toolArgs)}`;

          stream.sendMessage({
            type: "tool_use",
            tool_use_message: toolUseMessage,
          });

          // Call the tool
          const toolUseResponse = await mcpClient.callTool(toolName, toolArgs);

          // Handle tool response based on success/error
          if (toolUseResponse.error) {
            await toolService.handleToolError(
              toolUseResponse,
              toolName,
              toolUseId,
              conversationHistory,
              stream.sendMessage,
              conversationId,
            );
          } else {
            await toolService.handleToolSuccess(
              toolUseResponse,
              toolName,
              toolUseId,
              conversationHistory,
              productsToDisplay,
              conversationId,
            );
          }

          // Signal new message to client
          stream.sendMessage({ type: "new_message" });
        },

        // Handle content block completion
        onContentBlock: (contentBlock) => {
          if (contentBlock.type === "text") {
            stream.sendMessage({
              type: "content_block_complete",
              content_block: contentBlock,
            });
          }
        },
      },
    );
  }

  // Signal end of turn
  stream.sendMessage({ type: "end_turn" });

  // Send product results if available
  if (productsToDisplay.length > 0) {
    stream.sendMessage({
      type: "product_results",
      products: productsToDisplay,
    });
  }
}

/**
 * Get the customer MCP API URL for a shop
 * @param {string} shopDomain - The shop domain
 * @param {string} conversationId - The conversation ID
 * @returns {string} The customer MCP API URL
 */
async function getCustomerAccountUrls(shopDomain, conversationId) {
  if (!isTrustedShopOrigin(shopDomain)) {
    return null;
  }

  try {
    // Check if the customer account URL exists in the DB
    const existingUrls = await getCustomerAccountUrlsFromDb(conversationId);

    // If URL exists, return early with the MCP API URL
    if (existingUrls) return existingUrls;

    // If not, query for it from the Shopify API
    const { hostname } = new URL(shopDomain);

    const urls = await Promise.all([
      fetch(`https://${hostname}/.well-known/customer-account-api`).then(
        (res) => res.json(),
      ),
      fetch(`https://${hostname}/.well-known/openid-configuration`).then(
        (res) => res.json(),
      ),
    ]).then(async ([mcpResponse, openidResponse]) => {
      const response = {
        mcpApiUrl: mcpResponse.mcp_api,
        authorizationUrl: openidResponse.authorization_endpoint,
        tokenUrl: openidResponse.token_endpoint,
      };

      await storeCustomerAccountUrls({
        conversationId,
        mcpApiUrl: mcpResponse.mcp_api,
        authorizationUrl: openidResponse.authorization_endpoint,
        tokenUrl: openidResponse.token_endpoint,
      });

      return response;
    });

    return urls;
  } catch (error) {
    console.error("Error getting customer MCP API URL:", error);
    return null;
  }
}

/**
 * Whether an Origin/shop-domain value looks like a real Shopify storefront:
 * either the default *.myshopify.com domain, or the one custom domain this
 * app instance is explicitly configured for (SHOP_CUSTOM_DOMAIN). Origin is
 * client-supplied, so this gate stands between it and both server-side
 * fetches keyed on it and reflected CORS headers.
 * @param {string|null} origin
 * @returns {boolean}
 */
function isTrustedShopOrigin(origin) {
  if (!origin) return false;
  let hostname;
  try {
    hostname = new URL(origin).hostname.toLowerCase();
  } catch (error) {
    return false;
  }

  if (/^[a-z0-9][a-z0-9-]*\.myshopify\.com$/.test(hostname)) {
    return true;
  }

  const customDomain = process.env.SHOP_CUSTOM_DOMAIN?.toLowerCase();
  return Boolean(customDomain && hostname === customDomain);
}

/**
 * Gets CORS headers for the response. Only reflects Origin (with
 * credentials enabled) for a recognized shop domain - reflecting an
 * arbitrary Origin while allowing credentials would let any website read
 * responses from this API.
 * @param {Request} request - The request object
 * @returns {Object} CORS headers object
 */
function getCorsHeaders(request) {
  const origin = request.headers.get("Origin");
  const requestHeaders =
    request.headers.get("Access-Control-Request-Headers") ||
    "Content-Type, Accept";
  const headers = {
    "Access-Control-Allow-Methods": "GET, POST, OPTIONS",
    "Access-Control-Allow-Headers": requestHeaders,
    "Access-Control-Max-Age": "86400", // 24 hours
  };

  if (isTrustedShopOrigin(origin)) {
    headers["Access-Control-Allow-Origin"] = origin;
    headers["Access-Control-Allow-Credentials"] = "true";
  }

  return headers;
}

/**
 * Get SSE headers for the response. Same origin allowlisting as
 * getCorsHeaders - see there for why arbitrary Origin reflection isn't safe.
 * @param {Request} request - The request object
 * @returns {Object} SSE headers object
 */
function getSseHeaders(request) {
  const origin = request.headers.get("Origin");
  const headers = {
    "Content-Type": "text/event-stream",
    "Cache-Control": "no-cache",
    Connection: "keep-alive",
    "Access-Control-Allow-Methods": "GET,OPTIONS,POST",
    "Access-Control-Allow-Headers":
      "X-CSRF-Token, X-Requested-With, Accept, Accept-Version, Content-Length, Content-MD5, Content-Type, Date, X-Api-Version",
  };

  if (isTrustedShopOrigin(origin)) {
    headers["Access-Control-Allow-Origin"] = origin;
    headers["Access-Control-Allow-Credentials"] = "true";
  }

  return headers;
}
