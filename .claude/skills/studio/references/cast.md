# The Studio: cast looks

Source of truth: `floor/cast.js` (names, roles, colours, seat). Rename people there; keep the ids (`lead`, `producer`, ...) because the pipeline calls them. Sprites are 12 × 22 px, drawn by `drawChar` in `floor/index.html` from these fields only, so every session looks the same.

| id | Name | Look |
|---|---|---|
| `lead` | Rio, Lead Editor | grey hair under a charcoal **beret**, grey beard, tortoise **glasses**, black **turtleneck**, grey trousers. Works in his glass office. |
| `producer` | Ola, Producer | deep brown skin, long black **braids** with amber bead tips, **mustard blazer** over a cream top, gold earrings, navy trousers. |
| `transcriber` | Lulu, Transcriber | fair skin, **pink bob**, big **teal over-ear headphones**, lavender **hoodie**, white trainers. |
| `watcher` | Tariq, Screen Watcher | tan skin, short black hair and beard, **teal-rimmed glasses**, blue **polo**, khaki trousers. |
| `story` | Rosa, Story Editor | long wavy **auburn hair**, teal **cardigan** over cream, gold earrings. |
| `render` | Kit, Render Engineer | backwards **amber cap**, charcoal tee with **denim overalls**, white trainers. |
| `qc` | Ines, QC Inspector | dark hair in a **bun**, white **lab coat** over a blue scrub top, teal ID badge, pen in pocket. |
| `archivist` | Theo, Archivist | silver side-parted hair, dark **glasses**, brown **waistcoat** over cream shirt, red **bow tie**. |

Each person also has an `accent` colour: their name in the activity feed and their laptop glow at the table.

Hair styles available: `short side wavy bob bun braids beret cap`. Outfits: `blazer cardigan hoodie turtleneck polo labcoat overalls vest tee`. Extras: `glasses earrings cans badge bowtie`.
