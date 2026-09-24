// DSH's headless CLI consumes a positional task, not stdin. Keep private task
// text out of the OS process arguments by assigning the JavaScript argv only
// after reading a bounded pipe, then importing the installed launcher.
// This bridge does not grant permissions or attest to the selected model.
import { realpath, stat } from "node:fs/promises";
import { isAbsolute } from "node:path";
import { pathToFileURL } from "node:url";
import { TextDecoder } from "node:util";

const MAX_INPUT = 262144;

async function main() {
  const [entry, profile, ...extra] = process.argv.slice(2);
  if (extra.length || !entry || !isAbsolute(entry) ||
      !/^[a-zA-Z0-9_-]{1,64}$/.test(profile ?? "") ||
      !entry.endsWith(".js") && !entry.endsWith(".mjs")) {
    throw new Error("invalid launch specification");
  }
  // Resolve the deployment-selected loader, never a path supplied in the task.
  const loader = await realpath(entry);
  const info = await stat(loader);
  if (!info.isFile() || info.mode & 0o022) throw new Error("unsafe launcher");
  const chunks = [];
  let bytes = 0;
  for await (const chunk of process.stdin) {
    bytes += chunk.length;
    if (bytes > MAX_INPUT) throw new Error("input limit exceeded");
    chunks.push(chunk);
  }
  const task = new TextDecoder("utf-8", { fatal: true }).decode(Buffer.concat(chunks));
  if (!task.trim() || task.includes("\0")) throw new Error("invalid task");
  // Both the launcher and the headless app parse arguments with Commander.
  // Each parser consumes one --; the task cannot become options in either.
  process.argv = [process.execPath, loader, "--profile", profile, "--", "--", task];
  await import(pathToFileURL(loader).href);
}

main().catch(() => {
  // Launcher exceptions may contain task text, URLs or credentials.
  process.stderr.write("DSH task bridge failed; execution result is unconfirmed.\n");
  process.exitCode = 1;
});
