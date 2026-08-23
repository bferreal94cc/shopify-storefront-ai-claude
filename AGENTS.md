# Shopify Hydrogen development

This storefront is scaffolded from Shopify's Hydrogen skeleton template. See the README for framework-specific details.

Use the [Shopify AI Toolkit](https://shopify.dev/docs/apps/build/ai-toolkit) for Shopify API and platform work where applicable.

## Shopify MCP instructions for AI agents

### Store

The configured Shopify store is:
`aarayflex.myshopify.com`

The supplied Storefront MCP endpoint is:
`https://aarayflex.myshopify.com/api/mcp`

Future AI agents working in this repository may use Shopify MCP interfaces for storefront catalog discovery and cart operations for this store.

### Current MCP architecture

Shopify's current documentation separates catalog MCP and cart MCP capabilities from the legacy Storefront MCP cart tools.

- Storefront MCP base endpoint: `https://aarayflex.myshopify.com/api/mcp`
- Current UCP catalog/cart endpoint: `https://aarayflex.myshopify.com/api/ucp/mcp`

Shopify announced that the legacy `get_cart` and `update_cart` tools on `/api/mcp` are deprecated in favor of UCP Cart MCP and will be maintained only through August 31, 2026. New implementations MUST therefore prefer `/api/ucp/mcp` for catalog and cart capabilities. Use `/api/mcp` when a supported non-UCP Storefront MCP capability specifically requires it.

### Product/catalog operations

Use Storefront Catalog MCP for shopper-facing catalog discovery and product information:

- `search_catalog`
- `lookup_catalog`
- `get_product`

Catalog MCP is a storefront/catalog interface. It is NOT the Shopify Admin API and must not be treated as permission to create, delete, publish, unpublish, reprice, or otherwise administratively modify products.

If actual merchant product management is required, use an authenticated Shopify Admin API integration with the minimum required scopes. Never put Admin API credentials or private tokens in client code, prompts, source control, or public storefront configuration.

### Cart operations

Use Cart MCP at:
`https://aarayflex.myshopify.com/api/ucp/mcp`

Current cart tools:

- `create_cart`
- `get_cart`
- `update_cart`
- `cancel_cart`

Requests use JSON-RPC 2.0 and must include the required `meta.ucp-agent.profile` information. `cancel_cart` additionally requires a unique `meta["idempotency-key"]` UUID.

UCP `update_cart` uses replacement semantics. Agents must send the complete intended `line_items` state rather than assuming patch semantics.

When the buyer is ready to purchase, use Shopify's Checkout MCP flow rather than treating a cart as a completed order.

### MCP request pattern

Use this JSON-RPC shape and the exact current Shopify tool schema:

```json
{
  "jsonrpc": "2.0",
  "method": "tools/call",
  "id": 1,
  "params": {
    "name": "tool_name",
    "arguments": {
      "meta": {
        "ucp-agent": {
          "profile": "<agent-profile-uri>"
        }
      }
    }
  }
}
```

Do not invent tool parameters or assume that a deprecated Storefront MCP schema remains valid.

### Security, authorization, and Shopify terms

Shopify states that use of its Storefront MCP servers is subject to the Shopify API License and Terms of Use. Agents must follow those terms and Shopify's current MCP/UCP documentation.

MCP access does not grant arbitrary Admin privileges. Keep storefront shopping operations separate from privileged merchant administration.

Agents must not bypass Shopify authentication, authorization, rate limits, checkout controls, merchant policies, or platform safeguards. Do not scrape or reverse-engineer MCP behavior when an official API/MCP capability exists.

Never silently place an order or complete checkout without explicit buyer authorization in the storefront flow.

### AI-agent operating procedure

1. Review Shopify's current MCP/UCP documentation before implementing or changing MCP behavior.
2. Prefer `/api/ucp/mcp` for current catalog and cart capabilities.
3. Use `/api/mcp` only for supported capabilities that remain on the Storefront MCP endpoint.
4. Validate tool names and schemas against Shopify's current documentation.
5. Keep secrets server-side and out of Git.
6. Never interpret a product-management request as automatic Admin API write authorization.
7. Preserve the complete intended cart state when using UCP `update_cart`.
8. Require explicit buyer authorization before checkout/order completion.

### Authoritative Shopify references

- Storefront MCP: https://shopify.dev/docs/apps/build/storefront-mcp/servers/storefront
- Storefront Catalog MCP: https://shopify.dev/docs/agents/catalog/storefront-catalog
- Cart MCP: https://shopify.dev/docs/agents/carts-and-checkout/cart-mcp
- Cart-tool deprecation: https://shopify.dev/changelog/storefront-mcp-cart-tools-are-being-deprecated-in-favour-of-ucp-cart-mcp
