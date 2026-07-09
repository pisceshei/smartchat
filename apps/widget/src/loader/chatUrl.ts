/** Chat iframe URL contract.
 *
 * The api mounts the chat app at /widget-app (apps/api/app/main.py) and prod
 * nginx proxies /widget-app → api. "/chat/…" exists only as a LEGACY ALIAS so
 * loaders cached before this fix keep working — never emit it from here.
 * (Pointing the iframe at a path the edge doesn't route falls through to the
 * admin-SPA catch-all and renders the LOGIN page inside the widget panel —
 * that was a production incident.)
 */
export const CHAT_APP_PATH = "/widget-app/index.html";

export function buildChatIframeUrl(
  assetBase: string, // already stripped of any trailing slash by the loader
  key: string,
  parentOrigin: string,
  lang: string,
): URL {
  const u = new URL(assetBase + CHAT_APP_PATH);
  u.searchParams.set("k", key);
  u.searchParams.set("po", parentOrigin);
  u.searchParams.set("lang", lang);
  return u;
}
