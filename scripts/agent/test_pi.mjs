// A host-free pi bridge contract test: loads the generated extension and calls
// its registered tools through a real MCP child. No model or API key required.
import assert from "node:assert/strict";
import { pathToFileURL } from "node:url";

const tools = new Map();
const hooks = new Map();
const { default: extension } = await import(pathToFileURL(process.argv[2]).href);
extension({
  registerTool(tool) { tools.set(tool.name, tool); },
  on(name, handler) { hooks.set(name, handler); },
});
assert.deepEqual([...tools.keys()], ["tgrep_search_code", "tgrep_find_files"]);
assert.ok(hooks.has("session_start"));
const prompt = await hooks.get("before_agent_start")({ systemPrompt: "original" });
assert.ok(prompt.systemPrompt.includes("tgrep_search_code"));
const ctx = { cwd: process.argv[3] };
try {
  const [matches, files] = await Promise.all([
    tools.get("tgrep_search_code").execute("1", { pattern: "needle", freshness: "current" }, undefined, undefined, ctx),
    tools.get("tgrep_find_files").execute("2", { pattern: "*.rs", freshness: "current" }, undefined, undefined, ctx),
  ]);
  assert.ok(matches.details.results.length > 0);
  assert.deepEqual(files.details.results, [{ path: "src/main.rs" }]);
  await assert.rejects(() => tools.get("tgrep_search_code").execute("3", { pattern: "[", literal: false, freshness: "current" }, undefined, undefined, ctx));
} finally {
  hooks.get("session_shutdown")();
}
