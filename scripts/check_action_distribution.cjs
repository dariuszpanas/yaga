// Qualify the transported, source-free Action against the reviewed release manifest.
"use strict";
const assert = require("node:assert/strict");
const crypto = require("node:crypto");
const fs = require("node:fs");
const os = require("node:os");
const path = require("node:path");
const { spawnSync } = require("node:child_process");

const [distribution, target, source, manifestHash, sourceFilesHash] = process.argv.slice(2);
assert.equal(process.argv.length, 7, "expected distribution, target, source commit, manifest SHA-256 and source-files SHA-256");
assert.match(source, /^[0-9a-f]{40}$/);
assert.match(manifestHash, /^[0-9a-f]{64}$/);
assert.match(sourceFilesHash, /^[0-9a-f]{64}$/);
const cpu = { arm64: "aarch64", x64: "x86_64" }[process.arch];
const system = { linux: "unknown-linux-musl", darwin: "apple-darwin", win32: "pc-windows-msvc" }[process.platform];
assert.ok(cpu && system, "unsupported qualification host");
assert.equal(target, cpu + "-" + system, "the release target requires a matching native Node runner");
const action = fs.realpathSync(distribution);
const bootstrap = path.join(action, "scripts", "action-runtime.cjs");
const bundle = path.join(action, "action-native");
const manifest = fs.readFileSync(path.join(bundle, "yaga-native-sha256.txt"));
const digest = bytes => crypto.createHash("sha256").update(bytes).digest("hex");
assert.equal(digest(manifest), manifestHash, "unexpected release manifest");
// The staging job hashes the same sha256sum listing from the reviewed source checkout.
const sourceFiles = ["action.yml", "LICENSE", "scripts/action-runtime.cjs"];
const sourceListing = sourceFiles.map(name => digest(fs.readFileSync(path.join(action, name))) + "  " + name + "\n");
assert.equal(digest(sourceListing.join("")), sourceFilesHash, "unexpected staged Action source files");
assert.equal(fs.readFileSync(path.join(bundle, ".gitattributes"), "utf8"), "* -text\n", "missing byte-preserving Git attributes");
const filename = "yaga-" + target + (process.platform === "win32" ? ".exe" : "");
const lines = manifest.toString("ascii").split("\n").filter(line => line.endsWith("  " + filename));
assert.equal(lines.length, 1, "expected one manifest entry for this target");
const expected = lines[0].split("  ")[0];
assert.match(expected, /^[0-9a-f]{64}$/);

const scratch = fs.mkdtempSync(path.join(os.tmpdir(), "yaga-qualified-action-"));
try {
  const workspace = path.join(scratch, "workspace");
  const runner = path.join(scratch, "runner");
  const tools = path.join(scratch, "empty-path");
  for (const dir of [workspace, runner, tools]) fs.mkdirSync(dir);
  const output = path.join(scratch, "outputs");
  const env = {
    PATH: tools, TMPDIR: scratch, TEMP: scratch, TMP: scratch, HOME: scratch, USERPROFILE: scratch,
    GITHUB_ACTION_PATH: action, GITHUB_WORKSPACE: workspace, GITHUB_OUTPUT: output, RUNNER_TEMP: runner,
  };
  if (process.platform === "win32") env.SystemRoot = process.env.SystemRoot;
  function run(command, args, extra, code) {
    const result = spawnSync(command, args, {
      cwd: workspace, env: { ...env, ...extra }, encoding: "utf8", timeout: 30_000, maxBuffer: 131072,
    });
    if (result.error) throw result.error;
    assert.equal(result.status, code, "unexpected child exit");
    assert.equal(result.stderr, "", "unexpected child stderr");
    return result.stdout;
  }
  run(process.execPath, [bootstrap], {}, 0);
  const fields = Object.fromEntries(fs.readFileSync(output, "utf8").trimEnd().split(/\r?\n/).map(line => {
    const at = line.indexOf("=");
    return [line.slice(0, at), line.slice(at + 1)];
  }));
  const binary = fs.realpathSync(fields.binary);
  const relative = path.relative(fs.realpathSync(runner), binary);
  assert.ok(!path.isAbsolute(relative) && !relative.split(path.sep).includes(".."), "binary outside runner temp");
  assert.equal(digest(fs.readFileSync(binary)), expected, "wrong selected release executable");
  fs.writeFileSync(path.join(workspace, "branch.toml"), 'branch-policy-version = 1\nallowed-patterns = ["feat/*"]\n');
  const policy = { YAGA_INPUT_CHECK: "branch", YAGA_INPUT_POLICY: "branch.toml", YAGA_INPUT_BRANCH: "feat/qualified" };
  const pass = run(binary, ["__action", "cli"], policy, 0);
  assert.match(pass, /branch 'feat\/qualified' matched 'feat\/\*'/);
  const finding = run(binary, ["__action", "cli"], { ...policy, YAGA_INPUT_BRANCH: "unmatched" }, 1);
  assert.match(finding, /::error title=YAGA branch\.allowed::/);
  console.log(JSON.stringify({ source, target, manifest_sha256: manifestHash, executable_sha256: expected, branch_exits: [0, 1] }));
} finally {
  fs.rmSync(scratch, { recursive: true, force: true });
}
