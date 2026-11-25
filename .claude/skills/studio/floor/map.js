// The Studio floor plan: grid, walls, furniture footprints, seats, idle spots and pathfinding.
// Loaded by index.html and by scripts/check_floor.js (node). Must match references/floor-plan.md.
(function (root) {
  'use strict';
  const T = 8, COLS = 52, ROWS = 30;

  const blocked = Array.from({ length: ROWS }, () => Array(COLS).fill(false));
  const blk = (c0, r0, c1, r1) => { for (let r = r0; r <= r1; r++) for (let c = c0; c <= c1; c++) blocked[r][c] = true; };
  const open = (c, r) => { blocked[r][c] = false; };

  // shell: top wall (timeline screen lives here), side walls, bottom wall
  blk(0, 0, COLS - 1, 4); blk(0, 0, 0, ROWS - 1); blk(COLS - 1, 0, COLS - 1, ROWS - 1); blk(0, ROWS - 1, COLS - 1, ROWS - 1);
  // Rio's glass office (cols 1-12, rows 5-12), glass at col 13 and row 13, door at (9-10, 13)
  blk(13, 5, 13, 13); blk(1, 13, 13, 13); open(9, 13); open(10, 13);
  blk(3, 7, 9, 8);            // Rio's desk with dual monitors
  blk(10, 5, 12, 5);          // awards shelf
  // round table: every tile whose centre is within 3.3 tiles of (27,16)
  const TABLE = { c: 27, r: 16, radiusTiles: 3.3 };
  for (let r = 0; r < ROWS; r++) for (let c = 0; c < COLS; c++)
    if (Math.hypot(c - TABLE.c, r - TABLE.r) <= TABLE.radiusTiles) blocked[r][c] = true;
  blk(46, 5, 50, 8);          // render rack
  // sound booth (cols 1-9, rows 21-28): wall on row 20 and col 10, door at (10,25)
  blk(1, 20, 10, 20); blk(10, 20, 10, 28); open(10, 25);
  blk(5, 25, 5, 25);          // mic stand
  // screening corner: projector screen on a stand, couch, projector
  blk(33, 22, 43, 22); blk(34, 26, 42, 27); blk(38, 28, 38, 28);
  blk(49, 16, 50, 22);        // espresso bar along the right wall
  for (const [c, r] of [[1, 5], [14, 5], [44, 5], [1, 14], [11, 28], [50, 26]]) blk(c, r, c, r);  // plants

  // Seats. face: 'front' = we see their face (north of the table, looking south at it).
  const SEATS = {
    lead: { c: 6, r: 6, face: 'front' },
    S1: { c: 27, r: 12, face: 'front' },
    S2: { c: 30, r: 13, face: 'front' },
    S3: { c: 31, r: 17, face: 'back' },
    S4: { c: 29, r: 20, face: 'back' },
    S5: { c: 25, r: 20, face: 'back' },
    S6: { c: 23, r: 17, face: 'back' },
    S7: { c: 24, r: 13, face: 'front' },
  };
  // Where Rio stands when he checks on someone at the table.
  const VISIT = { S1: [28, 12], S2: [31, 13], S3: [32, 16], S4: [30, 20], S5: [24, 20], S6: [22, 16], S7: [23, 13] };

  // Idle activities. w = weight when picking. back = drawn from behind.
  const ACTIVITIES = [
    { id: 'dailies', label: 'watching dailies on the couch', w: 3, spots: [
      { c: 35, r: 25, pose: 'sitback', back: true }, { c: 38, r: 25, pose: 'sitback', back: true }, { c: 41, r: 25, pose: 'sitback', back: true } ] },
    { id: 'espresso', label: 'pulling an espresso', w: 3, spots: [
      { c: 48, r: 17, pose: 'drink' }, { c: 48, r: 20, pose: 'drink' } ] },
    { id: 'booth', label: 'testing the mic in the booth', w: 1, spots: [{ c: 5, r: 24, pose: 'mic' }] },
    { id: 'skylight', label: 'stretching under the skylight', w: 2, spots: [
      { c: 17, r: 24, pose: 'stretch' }, { c: 20, r: 25, pose: 'stretch' } ] },
    { id: 'timeline', label: 'studying the timeline wall', w: 2, spots: [
      { c: 20, r: 5, pose: 'back', back: true }, { c: 30, r: 5, pose: 'back', back: true }, { c: 38, r: 5, pose: 'back', back: true } ] },
    { id: 'phone', label: 'scrolling their phone', w: 2, spots: [
      { c: 12, r: 17, pose: 'phone' }, { c: 17, r: 16, pose: 'phone' }, { c: 38, r: 12, pose: 'phone' },
      { c: 44, r: 14, pose: 'phone' }, { c: 26, r: 25, pose: 'phone' } ] },
  ];

  const DIRS = [[1, 0], [-1, 0], [0, 1], [0, -1]];
  function findPath(sc, sr, gc, gr) {
    if (sc === gc && sr === gr) return [];
    const prev = new Int32Array(COLS * ROWS).fill(-1);
    const start = sr * COLS + sc, goal = gr * COLS + gc;
    const q = [start]; prev[start] = start;
    for (let i = 0; i < q.length; i++) {
      const cur = q[i]; if (cur === goal) break;
      const c = cur % COLS, r = (cur / COLS) | 0;
      for (const [dc, dr] of DIRS) {
        const nc = c + dc, nr = r + dr;
        if (nc < 0 || nr < 0 || nc >= COLS || nr >= ROWS) continue;
        const n = nr * COLS + nc;
        if (prev[n] !== -1 || (blocked[nr][nc] && n !== goal)) continue;
        prev[n] = cur; q.push(n);
      }
    }
    if (prev[goal] === -1) return null;   // unreachable
    const path = [];
    for (let n = goal; n !== start; n = prev[n]) path.push([n % COLS, (n / COLS) | 0]);
    return path.reverse();
  }

  const MAP = { T, COLS, ROWS, blocked, TABLE, SEATS, VISIT, ACTIVITIES, findPath };
  root.STUDIO_MAP = MAP;
  if (typeof module !== 'undefined' && module.exports) module.exports = MAP;
})(typeof window !== 'undefined' ? window : globalThis);
