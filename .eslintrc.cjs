/**
 * This is intended to be a basic starting point for linting in your app.
 * It relies on recommended configs out of the box for simplicity, but you can
 * and should modify this configuration to best suit your team's needs.
 */

/** @type {import('eslint').Linter.Config} */
module.exports = {
  root: true,
  parserOptions: {
    ecmaVersion: "latest",
    sourceType: "module",
    ecmaFeatures: {
      jsx: true,
    },
  },
  env: {
    browser: true,
    commonjs: true,
    es6: true,
  },
  ignorePatterns: [
    "!**/.server",
    "!**/.client",
    // Orphaned Hydrogen storefront scaffold (merged into main via a
    // separate, unrelated PR) that collides with this app's framework
    // entrypoints and can't be wired in alongside it. Kept on disk rather
    // than deleted; excluded here so lint only covers the live
    // shop-chat-agent app.
    "app/components/",
    "app/graphql/",
    "app/lib/",
    "app/entry.client.jsx",
    "customer-accountapi.generated.d.ts",
    "env.d.ts",
    "eslint.config.js",
    "react-router.config.js",
    "server.js",
    "storefrontapi.generated.d.ts",
    "app/routes/$.jsx",
    "app/routes/\\[robots.txt\\].jsx",
    "app/routes/\\[sitemap.xml\\].jsx",
    "app/routes/_index.jsx",
    "app/routes/account.$.jsx",
    "app/routes/account._index.jsx",
    "app/routes/account.addresses.jsx",
    "app/routes/account.jsx",
    "app/routes/account.orders.$id.jsx",
    "app/routes/account.orders._index.jsx",
    "app/routes/account.profile.jsx",
    "app/routes/account_.authorize.jsx",
    "app/routes/account_.login.jsx",
    "app/routes/account_.logout.jsx",
    "app/routes/blogs.$blogHandle.$articleHandle.jsx",
    "app/routes/blogs.$blogHandle._index.jsx",
    "app/routes/blogs._index.jsx",
    "app/routes/cart.$lines.jsx",
    "app/routes/cart.jsx",
    "app/routes/collections.$handle.jsx",
    "app/routes/collections._index.jsx",
    "app/routes/collections.all.jsx",
    "app/routes/discount.$code.jsx",
    "app/routes/pages.$handle.jsx",
    "app/routes/policies.$handle.jsx",
    "app/routes/policies._index.jsx",
    "app/routes/products.$handle.jsx",
    "app/routes/search.jsx",
    "app/routes/sitemap.$type.$page\\[.xml\\].jsx",
  ],

  // Base config
  extends: ["eslint:recommended"],

  overrides: [
    // React
    {
      files: ["**/*.{js,jsx,ts,tsx}"],
      plugins: ["react", "jsx-a11y"],
      extends: [
        "plugin:react/recommended",
        "plugin:react/jsx-runtime",
        "plugin:react-hooks/recommended",
        "plugin:jsx-a11y/recommended",
      ],
      settings: {
        react: {
          version: "detect",
        },
        formComponents: ["Form"],
        linkComponents: [
          { name: "Link", linkAttribute: "to" },
          { name: "NavLink", linkAttribute: "to" },
        ],
        "import/resolver": {
          typescript: {},
        },
      },
      rules: {
        "react/no-unknown-property": ["error", { ignore: ["variant"] }],
      },
    },

    // Typescript
    {
      files: ["**/*.{ts,tsx}"],
      plugins: ["@typescript-eslint", "import"],
      parser: "@typescript-eslint/parser",
      settings: {
        "import/internal-regex": "^~/",
        "import/resolver": {
          node: {
            extensions: [".ts", ".tsx"],
          },
          typescript: {
            alwaysTryTypes: true,
          },
        },
      },
      extends: [
        "plugin:@typescript-eslint/recommended",
        "plugin:import/recommended",
        "plugin:import/typescript",
      ],
    },

    // Node
    {
      files: [
        ".eslintrc.cjs",
        "vite.config.{js,ts}",
        ".graphqlrc.{js,ts}",
        "shopify.server.{js,ts}",
        "**/*.server.{js,ts}",
      ],
      env: {
        node: true,
      },
    },
  ],
  globals: {
    shopify: "readonly"
  },
};
