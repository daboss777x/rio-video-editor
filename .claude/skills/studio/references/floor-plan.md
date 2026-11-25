# The Studio: floor plan

Fixed every session. Implemented in `floor/map.js` (walls, footprints, seats, spots, pathfinding) and drawn in `floor/index.html`. If you change one, change the other, then run `node scripts/check_floor.js` (every seat, visit tile and spot must be free and reachable from every other).

**Grid:** 52 columns × 30 rows of 8-px tiles (416 × 240 logical px). Coordinates are `(column, row)`. Characters walk tile to tile with a breadth-first shortest path around furniture.

```
    0123456789012345678901234567890123456789012345678901
 0  ####################################################   top wall (rows 0-4):
 ..                                                         monitor wall + framed clapperboard (above Rio's office),
 4  ####################################################   TIMELINE WALL SCREEN (x 106-376), RENDER + ALERT signs (right)
 5  ##........#####.....t.........t.......t.....#.######   awards shelf (10-12,5); plants (1,5) (14,5) (44,5); render rack (46-50, rows 5-8)
 6  #.....R......#................................######   R = Rio's seat (6,6)
 7  #..#######...#................................######   Rio's desk + dual monitors (3-9, rows 7-8)
 8  #..#######...#................................######
 9  #............#.....................................#   glass wall of Rio's office at col 13 (rows 5-13)
12  #............#.............1..........p............#
13  #########..###..........7.###.2....................#   glass wall row 13, office door at (9-10, 13)
14  ##.......................#####..............p......#   round walnut table: tiles within 3.3 of (27,16)
15  #.......................#######....................#
16  #................p......#######..................###   espresso bar (49-50, rows 16-22)
17  #...........p..........6#######3................e###
18  #........................#####...................###
19  #.........................###....................###
20  ###########..............5...4..................e###   sound booth wall row 20 (cols 1-10)
21  #.........#......................................###
22  #.........#......................###########.....###   projector screen on a stand (33-43, row 22)
23  #.........#........................................#
24  #....m....#......k.................................#   skylight light patch (x 114-182, y 174-222)
25  #....#..............k.....p........d..d..d.........#   booth door (10,25); mic stand (5,25)
26  #.........#.......................#########.......##   screening couch (34-42, rows 26-27); plant (50,26)
27  #.........#.......................#########........#
28  #.........##..........................#............#   projector (38,28); plant (11,28)
29  ####################################################   bottom wall, entrance at x 184-200 (decorative)
```

## Seats (fixed; each person always sits in the same place)

| Seat | Tile | Faces | Person | Rio's visit tile |
|---|---|---|---|---|
| lead | (6, 6) | front (towards us) | Rio | - |
| S1 | (27, 12) | front | Ola, producer | (28, 12) |
| S2 | (30, 13) | front | Lulu, transcriber | (31, 13) |
| S3 | (31, 17) | back | Tariq, watcher | (32, 16) |
| S4 | (29, 20) | back | Rosa, story | (30, 20) |
| S5 | (25, 20) | back | Kit, render | (24, 20) |
| S6 | (23, 17) | back | Ines, QC | (22, 16) |
| S7 | (24, 13) | front | Theo, archivist | (23, 13) |

Seats run clockwise from the top in pipeline order. Each seat has a laptop on the table edge facing it (lit in the person's accent colour while they work).

## Idle activity spots

| Activity | Spots | Pose |
|---|---|---|
| Watching dailies on the couch (`d`) | (35,25) (38,25) (41,25) | `sitback`, from behind; the projector beam and the screen's "dailies" play while anyone sits |
| Espresso bar (`e`) | (48,17) (48,20) | `drink` |
| Sound booth mic (`m`) | (5,24) | `mic` (headphones on); the booth's lamp turns red |
| Stretching under the skylight (`k`) | (17,24) (20,25) | `stretch` |
| Studying the timeline wall (`t`) | (20,5) (30,5) (38,5) | `back` |
| Phone (`p`) | (12,17) (17,16) (38,12) (44,14) (26,25) | `phone` |

## Behaviour
- Everyone is in the studio from `reset`. **Busy** → walk to their seat and type ("…" bubble now and then). **Idle** → a random free activity, changing every 22-50 s, avoiding a repeat.
- **Rio** types at his desk. While anyone is seated he walks to their visit tile every ~16-32 s with a clipboard ("…" then a tick), then back.
- **Done**: Rio returns to his chair and leans back; a clapperboard snaps shut over the table with "THAT'S A WRAP", a clack + chime plays (after one click on the page), and the tab title flashes.
- **Fail**: the ALERT sign flashes red, the floor pulses red, Rio stands at his desk with a "!", a softer two-note tone plays, and "RUN STOPPED" shows.
- **Timeline wall**: one cell per stage. Pending = dim, running = amber with a moving scan and a timer (or `n/N` from the note), done = teal tick, failed = red "FAIL", skipped = "SKIP".
- **Render rack**: slow green heartbeat; fast amber/green flicker and a lit RENDER sign while `render` is busy or the render stage runs.
- **Skylight** follows local time: warm by day, amber at dusk, faint blue at night.
