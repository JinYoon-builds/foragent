---
name: broken-npx-install-404
description: README's first install line `npx foragent` fails with npm E404 because the npm package is not published
metadata:
  type: project
---
Goal: a new user with Node installs Foragent from the README.
Start: an empty folder. Node and npm are installed.
Repro: `npx --yes foragent </dev/null` → exit 1, `npm error 404 Not Found - GET https://registry.npmjs.org/foragent`. `npm view foragent` gives the same 404. The curl line works.
Failure: the first install command fails, and the error does not point to the curl line. Still 404 on 2026-11-09 (fa cdd787cb). Publishing the npm package was out of scope in #1475 (it needs Jin's account). Confirmed by a second session on 2026-11-06.
**How to apply:** rerun the repro. Remove this entry when `npm view foragent` returns a version or the README stops showing npx first.
