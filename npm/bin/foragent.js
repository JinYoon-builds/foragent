#!/usr/bin/env node
// `npx foragent`: run the same POSIX installer as the curl one-liner.
"use strict";
const { spawnSync } = require("child_process");
const fs = require("fs");
const path = require("path");

if (process.platform === "win32") {
  console.error("foragent: Windows is not supported yet; use WSL and run `npx foragent` there.");
  process.exit(1);
}
// The published package carries a copy (prepack); a repository checkout uses the root file.
const installer = [path.join(__dirname, "..", "install.sh"), path.join(__dirname, "..", "..", "install.sh")]
  .find((candidate) => fs.existsSync(candidate));
if (!installer) {
  console.error("foragent: install.sh is missing from this package.");
  process.exit(1);
}
const result = spawnSync("sh", [installer, ...process.argv.slice(2)], { stdio: "inherit" });
if (result.error) {
  console.error(`foragent: could not run sh: ${result.error.message}`);
  process.exit(1);
}
process.exit(result.status === null ? 1 : result.status);
