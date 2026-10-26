// Starts the standalone server produced by `next build` (output: "standalone").
// That build does not include static assets, so copy them next to the server first. The container
// image does the same. Configure with PORT and HOSTNAME (and PHAROS_DEFAULT_LOCALE).

import { cpSync, existsSync } from "node:fs";
import path from "node:path";
import { fileURLToPath, pathToFileURL } from "node:url";

const root = path.join(path.dirname(fileURLToPath(import.meta.url)), "..");
const standalone = path.join(root, ".next", "standalone");

if (!existsSync(path.join(standalone, "server.js"))) {
  console.error("No standalone build found. Run `pnpm build` first.");
  process.exit(1);
}

cpSync(path.join(root, ".next", "static"), path.join(standalone, ".next", "static"), {
  recursive: true,
});
if (existsSync(path.join(root, "public"))) {
  cpSync(path.join(root, "public"), path.join(standalone, "public"), { recursive: true });
}

await import(pathToFileURL(path.join(standalone, "server.js")).href);
