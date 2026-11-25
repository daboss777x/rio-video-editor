# Editing brief (pre-approved)

You are cutting a screen-recorded tutorial (one speaker). Cuts and joins only. This strategy is approved. Never ask questions; pick the most obvious reading. If in doubt, keep.

## Goal
- Compact and valuable. **No silences.** Never pad. Never force a length.
- Pacing: the intro is punchy (about 100 ms at joins); the body leaves about 140 ms (100 ms after a word has really faded, 40 ms before the next), brisk but never clipped. The merge applies these gaps for you, measured on the audio, so no word ending is ever clipped.
- Every kept sentence is complete, makes sense, and is said once. You are the editor who reads it, not a silence remover.

## Must keep
- Every spoken step and explanation. The intro. The outro.

## Must cut
- **Every silence**, including silent clicking, scrolling, typing or screen prep. The speaker uses those moments to check the script or set up the screen, not to teach.
- Filler words (um, uh, "so basically" restarts), false starts, tangents the speaker explicitly abandons.
- Rephrased repeats: when the speaker says something, then says it again in different words because they felt they misspoke, keep only the LAST version, as long as it makes clear sense on its own.

## Rules
1. Never drop a spoken step. A slightly long video beats a broken tutorial.
2. **Cut every silence, whatever the screen is doing.** The only exception: the speaker explicitly announces a silent demo or walkthrough ("watch this", "I'll let this run", "let me just show you without talking"). Then keep that silent stretch and set `"keep_silence": true` on that range.
3. You choose WHICH words to keep; the merge removes the pauses. Pauses longer than 0.175 s (0.15 s in the intro) inside your ranges are cut down automatically, on the audio, to about 140 ms (100 ms in the intro). So keep whole sentences in one range; do not try to trim pauses or tighten edges yourself.
4. Label only the **hook** `beat: "INTRO"` (usually the first ~1-2 minutes: the opening promise before the walkthrough starts; use the plan to see where it ends). Only those ranges get the punchy joins; the rest of an intro clip is body pacing.
5. Retakes and rephrasings: keep the LAST complete version, unless it is clearly worse (cut off, wrong, or missing something the earlier one said).
6. Cut only on word boundaries. Fades are added for you; do not plan them.
7. Do not cut across a chunk boundary. Your ranges stay inside the chunk's start/end.
8. Output a JSON array only: `{source, start, end, beat, quote, reason}` per range, in time order, no overlaps. `source` is the video stem. `beat` is a short label (INTRO, STEP, RESULT, OUTRO...).
9. No grade, subtitles, overlays, music, zooms or animations.

## Reading the chunk file
- Phrase lines: `[SSSS.ss-EEEE.ee] S0 text`. Times are source seconds.
- ACTIVITY lines describe each silent gap (>= 1.0 s) and what the screen did. They are background only: the silence is cut regardless of its class. Use them only to judge an announced silent demo.
- WORDS lines (`word@start-end`) appear only for phrases flagged with fillers or repeats. Use them to cut exactly on word edges, e.g. to drop an "um" or a repeated word.
- Use `timeline_view.py` only for genuinely ambiguous points. Max 10 per chunk.

## Chunk boundaries
- A chunk starts and ends at a silent gap. Do not extend a range past the chunk's end or before its start.
- If the last sentence runs to the chunk end, or the first one continues from the previous chunk, keep it whole.
- Keep the intro in chunk 1 and the outro in the last chunk.

## Decision table
| Situation | Do this |
|---|---|
| Silent gap, any class (static, typing, active) | Cut it. The merge leaves the join air. |
| Speaker announces a silent demo ("watch this", "I'll let it run") | Keep that silent stretch in one range with `"keep_silence": true`. |
| Speaker says a sentence, then says it again in other words | Keep only the last version, if it makes clear sense alone. |
| Retake: both complete, same content | Keep the last. |
| Instant retake, no pause: a sentence breaks off and restarts ("It will- The agent will...", "So when it had output a plan, so when...") | It is a retake. Cut the broken start on word edges, keep the restart. |
| Several stumbles in a row before one clean version | Keep only the final version that makes sense. |
| A word repeats but is a name ("trigger trigger.dev", see `glossary.md`) | Not a stutter. Keep both. |
| A stray sound that is not a word ("oop", a breath-laugh, "what?" to themselves) | Cut it. |
| Words with squashed timings (marked `~` in the read-through) | Keep the whole sentence in one range; never cut inside it. |
| Retake: last take is cut off or wrong | Keep the earlier complete take. |
| Speaker says "wait, scratch that" or abandons a tangent | Cut from the tangent start to where the main thread resumes. |
| Filler inside a sentence | Cut the filler word only, on word edges. |
| Sentence refers to something ("as you saw") | Keep what it refers to. |
| Unsure whether spoken content is a step | Keep it. |

## Values (from `profiles/tutorial.json`; the merge applies them, you do not)
- silence_policy: cut_all
- Join gap on the audio: body 100 ms tail + 40 ms lead, intro 60 + 40 ms. Pauses cut above: intro 0.15 s, body 0.175 s.
- Fade 10 ms at every join.
