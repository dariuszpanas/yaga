// Trusted setup for the native composite Action. This process receives no Action token.
"use strict";
const fs = require("node:fs");
const path = require("node:path");
const crypto = require("node:crypto");
const os = require("node:os");
const { spawn, spawnSync } = require("node:child_process");

const TOOLCHAIN = "1.100.0";
const RUSTC_COMMIT = "0ef1bc386059596dada8fb65b50425aab2ea637a";
const CARGO_COMMIT = "3acc79c0c841844675bc53a605922dbfc441e750";
const MAX_ASSET = 100 * 1024 * 1024;
const CARGO_SOURCES = ["Cargo.toml", "Cargo.lock", "crates"];
const TRIPLES = ["aarch64", "x86_64"].flatMap(arch =>
  ["apple-darwin", "pc-windows-msvc", "unknown-linux-musl"].map(platform => arch + "-" + platform));
const basename = triple => "yaga-" + triple + (triple.endsWith("msvc") ? ".exe" : "");

function inside(child, parent) {
  const rel = path.relative(parent, child);
  return !rel || (rel !== ".." && !rel.startsWith(".." + path.sep) && !path.isAbsolute(rel));
}
function plain(file, limit) {
  const stat = fs.lstatSync(file);
  if (!stat.isFile() || stat.size > limit) throw Error("invalid native distribution file");
  return fs.readFileSync(file);
}
function directory(dir) {
  if (!fs.lstatSync(dir).isDirectory()) throw Error("invalid native distribution directory");
}
function present(file) {
  try { fs.lstatSync(file); return true; }
  catch (error) { if (error.code === "ENOENT") return false; throw error; }
}
function digest(buffer) {
  return crypto.createHash("sha256").update(buffer).digest("hex");
}
function verifiedManifest(bundle, expected) {
  directory(bundle);
  const bytes = plain(path.join(bundle, "yaga-native-sha256.txt"), 2048);
  if (!/^[a-f0-9]{64}$/.test(expected) || digest(bytes) !== expected) throw Error("native manifest digest mismatch");
  const text = bytes.toString("utf8");
  const lines = text.split("\n");
  if (lines.pop() !== "" || !/^yaga-native-v1 (?:0|[1-9]\d*)\.(?:0|[1-9]\d*)\.(?:0|[1-9]\d*)$/.test(lines.shift()) ||
      lines.length !== TRIPLES.length) throw Error("invalid native distribution manifest");
  const names = TRIPLES.map(basename).sort();
  const hashes = new Map();
  for (let i = 0; i < names.length; i++) {
    const m = /^([0-9a-f]{64})  ([A-Za-z0-9_.-]+)$/.exec(lines[i]);
    if (!m || m[2] !== names[i]) throw Error("invalid native distribution asset list");
    hashes.set(m[2], m[1]);
  }
  return { hashes, bytes };
}
function verifiedAsset(bundle, manifest, name) {
  const image = plain(path.join(bundle, name), MAX_ASSET);
  if (!image.length || digest(image) !== manifest.hashes.get(name)) throw Error("native executable digest mismatch");
  return image;
}
function copyBundle(bundle, dest, pinned) {
  const pin = plain(path.join(bundle, "manifest.sha256"), 65).toString("utf8");
  if (!/^[0-9a-f]{64}\n$/.test(pin) || (pinned && pin !== pinned)) throw Error("invalid native manifest pin");
  const manifest = verifiedManifest(bundle, pin.trimEnd());
  const arch = { arm64: "aarch64", x64: "x86_64" }[process.arch];
  const system = { linux: "unknown-linux-musl", darwin: "apple-darwin", win32: "pc-windows-msvc" }[process.platform];
  if (!arch || !system) throw Error("unsupported native Action runner");
  const image = verifiedAsset(bundle, manifest, basename(arch + "-" + system));
  fs.writeFileSync(dest, image, { flag: "wx", mode: 0o700 });
}
function stageBundle([source, dest, pin, ...extra], distribution = false) {
  if (!source || !dest || !pin || extra.length) throw Error("stage requires a release directory, new bundle directory and manifest SHA-256");
  const manifest = verifiedManifest(source, pin);
  if (present(dest)) throw Error("native Action staging destination already exists");
  const staging = fs.mkdtempSync(path.join(path.dirname(path.resolve(dest)), ".yaga-action-dist-"));
  try {
    writeBundle(source, staging, manifest, pin, distribution);
    if (present(dest)) throw Error("native Action staging destination already exists");
    fs.renameSync(staging, dest);
  } finally {
    if (present(staging)) fs.rmSync(staging, { recursive: true, force: true });
  }
}
function writeBundle(source, dest, manifest, pin, distribution) {
  if (distribution) {
    const action = path.resolve(__dirname, "..");
    for (const name of ["action.yml", "LICENSE"]) fs.copyFileSync(path.join(action, name), path.join(dest, name), fs.constants.COPYFILE_EXCL);
    fs.mkdirSync(path.join(dest, "scripts"), { mode: 0o700 });
    fs.copyFileSync(__filename, path.join(dest, "scripts", "action-runtime.cjs"), fs.constants.COPYFILE_EXCL);
    dest = path.join(dest, "action-native");
    fs.mkdirSync(dest, { mode: 0o700 });
  }
  // A Git checkout must reproduce the bytes used to compute the manifest and its pin.
  fs.writeFileSync(path.join(dest, ".gitattributes"), "* -text\n", { flag: "wx" });
  for (const name of manifest.hashes.keys()) {
    fs.writeFileSync(path.join(dest, name), verifiedAsset(source, manifest, name), { flag: "wx", mode: 0o700 });
  }
  // An interrupted or corrupt staging operation cannot create a runnable distribution.
  fs.writeFileSync(path.join(dest, "yaga-native-sha256.txt"), manifest.bytes, { flag: "wx" });
  fs.writeFileSync(path.join(dest, "manifest.sha256"), pin + "\n", { flag: "wx" });
}
function copySources(source, target) {
  const s = fs.lstatSync(source);
  if (s.isFile()) {
    fs.copyFileSync(source, target, fs.constants.COPYFILE_EXCL);
  } else if (s.isDirectory()) {
    fs.mkdirSync(target, { mode: 0o700 });
    for (const name of fs.readdirSync(source)) {
      if (name === ".cargo" || name === "target" || name === ".git") continue;
      copySources(path.join(source, name), path.join(target, name));
    }
  } else {
    throw Error("native Action source contains a link or special file");
  }
}
function toolPath(workspace) {
  const dirs = (process.env.PATH || "").split(path.delimiter).flatMap(entry => {
    if (!path.isAbsolute(entry)) return [];
    try {
      const real = fs.realpathSync(entry);
      return inside(real, workspace) || !fs.statSync(real).isDirectory() ? [] : [real];
    } catch { return []; }
  });
  return [...new Set(dirs)];
}
function tool(name, dirs, workspace) {
  const file = name + (process.platform === "win32" ? ".exe" : "");
  for (const dir of dirs) {
    const candidate = path.join(dir, file);
    if (fs.existsSync(candidate) && fs.statSync(candidate).isFile() && !inside(fs.realpathSync(candidate), workspace)) return candidate;
  }
  throw Error("required runner tool is missing: " + name);
}
function checkedTool(program, argv, options, label) {
  const r = spawnSync(program, argv, { ...options, encoding: "utf8", maxBuffer: 4 * 1024 * 1024,
    windowsHide: true, stdio: ["ignore", "pipe", "pipe"] });
  if (r.error || r.status !== 0) throw Error(label);
  return r.stdout;
}
async function buildTool(program, argv, options, label) {
  // Toolchain installation and compilation spawn children. Terminate the entire
  // invocation on timeout/cancellation before an Action can use its output.
  await new Promise((resolve, reject) => {
    const child = spawn(program, argv, {
      cwd: options.cwd, env: options.env, stdio: "ignore", detached: process.platform !== "win32", windowsHide: true,
    });
    let stopped = false;
    function stop() {
      if (!child.pid || stopped) return;
      stopped = true;
      if (process.platform === "win32") {
        spawnSync(path.join(process.env.SYSTEMROOT, "System32", "taskkill.exe"), ["/PID", String(child.pid), "/T", "/F"],
          { stdio: "ignore", windowsHide: true, timeout: 5000 });
        child.kill();
      } else {
        try { process.kill(-child.pid, "SIGKILL"); } catch (error) { if (error.code !== "ESRCH") child.kill("SIGKILL"); }
      }
    }
    const timer = setTimeout(stop, options.timeout);
    for (const signal of ["SIGINT", "SIGTERM"]) process.once(signal, stop);
    child.once("error", () => finish(false));
    child.once("close", status => finish(status === 0));
    function finish(ok) {
      clearTimeout(timer);
      for (const signal of ["SIGINT", "SIGTERM"]) process.removeListener(signal, stop);
      if (ok && !stopped) resolve();
      else reject(Error(label));
    }
  });
}
function trustedLocal(action, workspace, staging) {
  const sha = process.env.GITHUB_SHA || "";
  if (action !== workspace || !["pull_request_target", "workflow_run"].includes(process.env.GITHUB_EVENT_NAME) ||
      !/^[a-f0-9]{40}$/.test(sha)) throw Error("privileged local Action requires a trusted default-branch checkout");
  let event;
  try { event = JSON.parse(plain(process.env.GITHUB_EVENT_PATH, 4 * 1024 * 1024).toString("utf8")); }
  catch { throw Error("invalid runner event for the privileged local Action"); }
  const branch = event?.repository?.default_branch;
  if (typeof branch !== "string" || !branch || branch.length > 255 ||
      event.repository.full_name !== process.env.GITHUB_REPOSITORY ||
      process.env.GITHUB_REF !== "refs/heads/" + branch) throw Error("privileged local Action requires the runner default branch");
  const dirs = toolPath(workspace);
  const git = tool("git", dirs, workspace);
  const emptyConfig = path.join(staging, "git-config");
  fs.writeFileSync(emptyConfig, "", { flag: "wx", mode: 0o600 });
  const env = { PATH: dirs.join(path.delimiter), HOME: staging, USERPROFILE: staging,
    GIT_CONFIG_NOSYSTEM: "1", GIT_CONFIG_GLOBAL: emptyConfig, GIT_NO_REPLACE_OBJECTS: "1",
    GIT_TERMINAL_PROMPT: "0", GIT_NO_LAZY_FETCH: "1" };
  for (const name of ["SYSTEMROOT", "WINDIR"]) if (process.env[name]) env[name] = process.env[name];
  const options = { cwd: staging, env, timeout: 30_000 };
  const args = ["--no-lazy-fetch", "-C", action];
  if (checkedTool(git, [...args, "rev-parse", "--verify", "HEAD"], options,
    "trusted Action commit unavailable").trimEnd() !== sha) throw Error("local Action checkout must match the runner commit");
  const tree = checkedTool(git, [...args, "ls-tree", "-z", sha, "--", "action-native"], options, "could not check trusted native distribution");
  let pin;
  if (tree) {
    if (!/^040000 tree [0-9a-f]{40}\taction-native\0$/.test(tree)) throw Error("invalid committed native distribution");
    pin = checkedTool(git, [...args, "cat-file", "blob", sha + ":action-native/manifest.sha256"], options,
      "missing committed native manifest pin");
    if (!/^[0-9a-f]{64}\n$/.test(pin)) throw Error("invalid committed native manifest pin");
  }
  return { git, options, sha, pin };
}
async function build(action, workspace, staging, dest, trusted) {
  const src = path.join(staging, "source");
  const dirs = toolPath(workspace);
  const env = { PATH: dirs.join(path.delimiter), CARGO_HOME: path.join(staging, "cargo"),
    CARGO_TARGET_DIR: path.join(staging, "target"), HOME: path.join(staging, "home"),
    RUSTUP_HOME: process.env.RUSTUP_HOME || path.join(os.homedir(), ".rustup"),
    RUSTUP_TOOLCHAIN: TOOLCHAIN, TERM: "dumb" };
  for (const name of ["SYSTEMROOT", "WINDIR", "PROGRAMFILES", "PROGRAMFILES(X86)", "PROGRAMW6432"]) {
    if (process.env[name]) env[name] = process.env[name];
  }
  if (inside(fs.realpathSync(env.RUSTUP_HOME), workspace)) throw Error("toolchain must be outside the consumer workspace");
  fs.mkdirSync(env.HOME, { mode: 0o700 });
  for (const name of ["USERPROFILE", "LOCALAPPDATA", "APPDATA"]) env[name] = env.HOME;
  for (const name of ["TMP", "TEMP", "TMPDIR"]) env[name] = staging;
  fs.mkdirSync(src, { mode: 0o700 });
  for (let dir = src; ; dir = path.dirname(dir)) {
    if (["config", "config.toml"].some(name => present(path.join(dir, ".cargo", name)))) {
      throw Error("runner temporary directory inherits a Cargo configuration");
    }
    if (dir === path.dirname(dir)) break;
  }
  if (trusted) {
    // Runner-owned base SHA, never a PR head or mutable worktree files.
    const { git, options, sha } = trusted;
    // Read each committed blob directly. Avoid checkout filters, archive substitution and user tar drivers.
    const listing = checkedTool(git, ["--no-lazy-fetch", "-C", action, "ls-tree", "-rz", sha, "--",
      ...CARGO_SOURCES], options, "could not enumerate trusted Action sources");
    const files = listing.split("\0").filter(Boolean).map(row => {
      const m = /^(100644|100755) blob ([0-9a-f]{40})\t((?:[A-Za-z0-9_.-]+\/)*[A-Za-z0-9_.-]+)$/.exec(row);
      if (!m || m[3].split("/").some(x => [".", "..", ".cargo", ".git"].includes(x))) throw Error("invalid trusted source entry");
      return { oid: m[2], name: m[3] };
    });
    if (files.length > 10000) throw Error("too many native Action sources");
    const batch = spawnSync(git, ["--no-lazy-fetch", "-C", action, "cat-file", "--batch"],
      { ...options, input: files.map(f => f.oid + "\n").join(""), maxBuffer: 64 * 1024 * 1024, windowsHide: true });
    if (batch.error || batch.status !== 0) throw Error("could not read trusted source objects");
    let offset = 0;
    for (const file of files) {
      const eol = batch.stdout.indexOf(10, offset);
      if (eol < 0 || eol - offset > 100) throw Error("invalid trusted source record");
      const header = /^([0-9a-f]{40}) blob ([0-9]+)$/.exec(batch.stdout.toString("ascii", offset, eol));
      if (!header || header[1] !== file.oid) throw Error("invalid trusted source identity");
      const size = Number(header[2]);
      const end = eol + 1 + size;
      if (!Number.isSafeInteger(size) || size > 8 * 1024 * 1024 || batch.stdout[end] !== 10)
        throw Error("invalid trusted source length");
      const blob = batch.stdout.subarray(eol + 1, end);
      if (crypto.createHash("sha1").update("blob " + size + "\0").update(blob).digest("hex") !== file.oid)
        throw Error("invalid trusted source content");
      offset = end + 1;
      const target = path.join(src, file.name);
      fs.mkdirSync(path.dirname(target), { recursive: true, mode: 0o700 });
      fs.writeFileSync(target, blob, { flag: "wx", mode: 0o600 });
    }
    if (offset !== batch.stdout.length) throw Error("invalid trusted source trailer");
  } else {
    for (const name of CARGO_SOURCES) copySources(path.join(action, name), path.join(src, name));
  }
  const options = { cwd: src, env, timeout: 300_000 };
  const rustup = tool("rustup", dirs, workspace);
  const installed = checkedTool(rustup, ["toolchain", "list"], options, "could not enumerate runner toolchains")
    .split(/\r?\n/).map(line => line.split(/\s/)[0]);
  let selected = TOOLCHAIN;
  if (!installed.some(name => name.startsWith(TOOLCHAIN + "-"))) {
    const stable = installed.find(name => name.startsWith("stable-"));
    if (stable && checkedTool(rustup, ["run", stable, "rustc", "-vV"], options,
      "could not inspect runner compiler").split(/\r?\n/).includes("commit-hash: " + RUSTC_COMMIT)) selected = stable;
  }
  if (selected === TOOLCHAIN && !installed.some(name => name.startsWith(TOOLCHAIN + "-"))) {
    await buildTool(rustup, ["toolchain", "install", TOOLCHAIN, "--profile", "minimal", "--no-self-update"],
      { ...options, timeout: 120_000 }, "could not select the pinned Rust Action toolchain");
  }
  env.RUSTUP_TOOLCHAIN = selected;
  for (const [name, revision] of [["rustc", RUSTC_COMMIT], ["cargo", CARGO_COMMIT]]) {
    const identity = checkedTool(rustup, ["run", selected, name, "-Vv"], options, "could not inspect pinned Action toolchain");
    if (!identity.split(/\r?\n/).includes("commit-hash: " + revision)) throw Error("native Action toolchain identity mismatch");
  }
  // Commit quality is an installed CLI command. The gate uses the CLI's explicit GitHub transport.
  await buildTool(rustup, ["run", selected, "cargo", "build", "--locked", "--no-default-features",
    "--profile", "dev", "-p", "yaga-cli", "--bin", "yaga"], options,
    "locked native Action build failed or exceeded its setup deadline");
  const image = path.join(env.CARGO_TARGET_DIR, "debug", path.basename(dest));
  fs.copyFileSync(image, dest, fs.constants.COPYFILE_EXCL);
  fs.chmodSync(dest, 0o700);
}
async function main() {
  const started = Date.now();
  const action = fs.realpathSync(process.env.GITHUB_ACTION_PATH);
  const workspace = fs.realpathSync(process.env.GITHUB_WORKSPACE);
  const temp = fs.realpathSync(process.env.RUNNER_TEMP);
  if (inside(temp, workspace)) throw Error("runner temporary directory must be outside the consumer workspace");
  // Trust applies to the action.yml and bootstrap that the runner already selected.
  // A privileged consumer must never run a local Action from checked-out PR code.
  const privileged = process.env.YAGA_SETUP_PRIVILEGED === "1";
  if (privileged && !inside(action, workspace) && !/^[0-9a-f]{40}$/.test(process.env.YAGA_SETUP_ACTION_REF || "")) {
    throw Error("privileged Actions require an immutable full Action commit");
  }
  const staging = fs.mkdtempSync(path.join(temp, "yaga-action-"));
  const exe = path.join(staging, process.platform === "win32" ? "yaga.exe" : "yaga");
  const bundle = path.join(action, "action-native");
  const trusted = privileged && inside(action, workspace) ? trustedLocal(action, workspace, staging) : null;
  if (present(bundle)) {
    if (trusted && !trusted.pin) throw Error("uncommitted native distribution");
    copyBundle(bundle, exe, trusted?.pin);
  } else {
    if (trusted?.pin) throw Error("committed native distribution is missing");
    await build(action, workspace, staging, exe, trusted);
  }
  if (/[\r\n]/.test(exe)) throw Error("invalid runner tool location");
  fs.appendFileSync(process.env.GITHUB_OUTPUT, "binary=" + exe + "\nstart-ms=" + started + "\n");
}
if (require.main === module) {
  Promise.resolve().then(() => {
    if (process.argv[2] === "stage") stageBundle(process.argv.slice(3));
    else if (process.argv[2] === "distribution") stageBundle(process.argv.slice(3), true);
    else if (process.argv.length === 2) return main();
    else throw Error("invalid native Action setup argument");
  }).catch(error => {
    // Internal OS paths and raw tool/HTTP output are not runner annotations.
    const detail = error.code ? "native Action setup failed" : error.message;
    console.error("YAGA setup failed: " + detail.replace(/[\r\n%]/g, "?").slice(0, 200));
    if (process.env.GITHUB_OUTPUT) fs.appendFileSync(process.env.GITHUB_OUTPUT, "exit-code=2\n");
    process.exitCode = 2;
  });
}
