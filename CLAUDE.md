# shopify_storefront_ai_claude

## Project

AI-powered Shopify storefront chat agent, modeled on Shopify's official
shop-chat-agent reference app (React Router + MCP + Claude).

## Standing instructions

- Activate and use all connected MCP servers/tools relevant to the task
  (Shopify Storefront MCP, Customer Accounts MCP, Shopify AI Toolkit)
  rather than guessing at the Shopify API surface.
- Prefer Shopify's first-party Storefront MCP / Customer Accounts MCP
  over third-party clones.
- Use Sonnet 5 for routine build work, Opus 5 for architecture decisions
  and hard debugging, Haiku 4.5 for narrow/fast tasks, Fable 5 only for
  genuinely hard problems.
- At the end of a session, state which model was configured for it.

## Build & test

```bash
npm install                # installs dependencies
npm run setup               # prisma generate && prisma migrate deploy
npm run dev                  # shopify app dev (requires shopify.app.toml linked to a real app)
npm run build                # react-router build
npm run start                 # react-router-serve ./build/server/index.js (after build)
npm run lint                   # eslint
npm run typecheck               # react-router typegen && tsc --noEmit
```

Requires `.env` (gitignored, copy from `.env.example`): `CLAUDE_API_KEY`,
`SHOPIFY_API_KEY` (app client ID), `REDIRECT_URL`. `shopify.app.toml` still
has placeholder `client_id = "YOUR_CLIENT_ID"` — run `npm run config:link`
against a real app in the Partner/Dev Dashboard before `npm run dev`.
