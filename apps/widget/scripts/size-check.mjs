// Budget gate: loader.js < 25KB raw; chat bundle < 120KB gzip total.
import { readFileSync, readdirSync, statSync } from "node:fs";
import { join } from "node:path";
import { fileURLToPath } from "node:url";
import { gzipSync } from "node:zlib";

const dist = fileURLToPath(new URL("../dist", import.meta.url));

const LOADER_RAW_BUDGET = 25 * 1024;
const CHAT_GZIP_BUDGET = 120 * 1024;

let failed = false;

const loader = readFileSync(join(dist, "loader.js"));
const loaderGz = gzipSync(loader).length;
console.log(
  `loader.js: ${loader.length} B raw / ${loaderGz} B gzip (budget ${LOADER_RAW_BUDGET} B raw)`,
);
if (loader.length > LOADER_RAW_BUDGET) {
  console.error("FAIL: loader.js exceeds 25KB raw budget");
  failed = true;
}

function walk(dir) {
  const out = [];
  for (const name of readdirSync(dir)) {
    const p = join(dir, name);
    if (statSync(p).isDirectory()) out.push(...walk(p));
    else out.push(p);
  }
  return out;
}

let chatGz = 0;
for (const f of walk(join(dist, "chat"))) {
  if (/\.(js|css|html)$/.test(f)) chatGz += gzipSync(readFileSync(f)).length;
}
console.log(`chat bundle: ${chatGz} B gzip total (budget ${CHAT_GZIP_BUDGET} B gzip)`);
if (chatGz > CHAT_GZIP_BUDGET) {
  console.error("FAIL: chat bundle exceeds 120KB gzip budget");
  failed = true;
}

process.exit(failed ? 1 : 0);
