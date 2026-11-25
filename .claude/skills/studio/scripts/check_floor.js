#!/usr/bin/env node
// Checks the Studio floor plan: every seat, visit tile and idle spot is a free tile,
// and every one of them can be reached from every other one (so anyone can walk anywhere).
// Usage: node .claude/skills/studio/scripts/check_floor.js   (exit 1 on any problem)
'use strict';
const path = require('path');
const M = require(path.join(__dirname, '..', 'floor', 'map.js'));

const targets = [];
for (const [k, s] of Object.entries(M.SEATS)) targets.push([`seat ${k}`, s.c, s.r]);
for (const [k, [c, r]] of Object.entries(M.VISIT)) targets.push([`visit ${k}`, c, r]);
for (const a of M.ACTIVITIES) a.spots.forEach((s, i) => targets.push([`${a.id}#${i + 1}`, s.c, s.r]));

let bad = 0;
const seen = new Set();
for (const [name, c, r] of targets) {
  const key = `${c},${r}`;
  if (seen.has(key) && !name.startsWith('visit')) { console.log(`DUPLICATE tile ${key}: ${name}`); bad++; }
  seen.add(key);
  if (c < 0 || r < 0 || c >= M.COLS || r >= M.ROWS) { console.log(`OUT OF GRID ${name} (${c},${r})`); bad++; continue; }
  if (M.blocked[r][c]) { console.log(`BLOCKED ${name} (${c},${r})`); bad++; }
}
let pairs = 0;
for (const [a, ac, ar] of targets) for (const [b, bc, br] of targets) {
  if (a === b) continue;
  pairs++;
  const p = M.findPath(ac, ar, bc, br);
  if (p === null) { console.log(`UNREACHABLE ${a} (${ac},${ar}) -> ${b} (${bc},${br})`); bad++; }
  else for (const [c, r] of p) if (M.blocked[r][c]) { console.log(`PATH THROUGH WALL ${a} -> ${b} at (${c},${r})`); bad++; break; }
}
console.log(`${targets.length} tiles, ${pairs} paths checked, ${bad} problem(s)`);
process.exit(bad ? 1 : 0);
