import { flatRoutes } from "@react-router/fs-routes";

// The Hydrogen storefront files below were merged in from an unrelated PR
// (see the shop-chat-agent-scaffold merge commit) and collide with this
// app's own routes if left live under flatRoutes' file-based discovery.
// Kept on disk rather than deleted; excluded from routing here.
const ignoredRouteFiles = [
  "routes/$.jsx",
  "routes/\\[robots.txt\\].jsx",
  "routes/\\[sitemap.xml\\].jsx",
  "routes/_index.jsx",
  "routes/account.$.jsx",
  "routes/account._index.jsx",
  "routes/account.addresses.jsx",
  "routes/account.jsx",
  "routes/account.orders.$id.jsx",
  "routes/account.orders._index.jsx",
  "routes/account.profile.jsx",
  "routes/account_.authorize.jsx",
  "routes/account_.login.jsx",
  "routes/account_.logout.jsx",
  "routes/blogs.$blogHandle.$articleHandle.jsx",
  "routes/blogs.$blogHandle._index.jsx",
  "routes/blogs._index.jsx",
  "routes/cart.$lines.jsx",
  "routes/cart.jsx",
  "routes/collections.$handle.jsx",
  "routes/collections._index.jsx",
  "routes/collections.all.jsx",
  "routes/discount.$code.jsx",
  "routes/pages.$handle.jsx",
  "routes/policies.$handle.jsx",
  "routes/policies._index.jsx",
  "routes/products.$handle.jsx",
  "routes/search.jsx",
  "routes/sitemap.$type.$page\\[.xml\\].jsx",
];

export default flatRoutes({ ignoredRouteFiles });
