import { cp, mkdir, readFile, writeFile } from "node:fs/promises";
import { fileURLToPath } from "node:url";
import path from "node:path";

const apiBaseUrl = process.env.AUREON_API_BASE_URL?.trim().replace(/\/+$/, "");
if (!apiBaseUrl) {
  throw new Error("Set AUREON_API_BASE_URL to the deployed HTTPS Aureon API before building the native app.");
}

let apiUrl;
try {
  apiUrl = new URL(apiBaseUrl);
} catch {
  throw new Error("AUREON_API_BASE_URL must be an absolute HTTPS URL.");
}
if (apiUrl.protocol !== "https:" || !apiUrl.host || apiUrl.username || apiUrl.password) {
  throw new Error("AUREON_API_BASE_URL must be a public HTTPS URL without embedded credentials.");
}

const scriptDir = path.dirname(fileURLToPath(import.meta.url));
const projectDir = path.resolve(scriptDir, "..");
const uiDir = path.join(projectDir, "static", "ui");
const nativeWebDir = path.join(projectDir, "native-web");
await mkdir(path.join(nativeWebDir, "ui"), { recursive: true });
await cp(uiDir, path.join(nativeWebDir, "ui"), { recursive: true });
await cp(path.join(uiDir, "index.html"), path.join(nativeWebDir, "index.html"));
await cp(path.join(uiDir, "manifest.webmanifest"), path.join(nativeWebDir, "manifest.webmanifest"));
await cp(path.join(uiDir, "sw.js"), path.join(nativeWebDir, "sw.js"));
await cp(
  path.join(projectDir, "node_modules", "@capacitor", "core", "dist", "capacitor.js"),
  path.join(nativeWebDir, "capacitor.js"),
);
const nativeIndexPath = path.join(nativeWebDir, "index.html");
const nativeIndex = await readFile(nativeIndexPath, "utf8");
await writeFile(
  nativeIndexPath,
  nativeIndex.replace(
    '<script src="/ui/runtime-config.js"></script>',
    '<script src="/capacitor.js"></script>\n  <script src="/ui/runtime-config.js"></script>',
  ),
  "utf8",
);

const config = `window.AUREON_CONFIG = Object.freeze(${JSON.stringify({ apiBaseUrl })});\n`;
await writeFile(path.join(nativeWebDir, "ui", "runtime-config.js"), config, "utf8");
console.log(`Configured native app API endpoint: ${apiUrl.origin}`);
