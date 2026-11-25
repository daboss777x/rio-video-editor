# Lessons

## Approved
<!-- Only you edit this section. The editor reads it on every run. Move a lesson here from Proposed to switch it on. -->
- [2026-10-29 setterintro3min #1] If the recording ends partway through a sentence, end the video on the last complete sentence. Never end on a cut-off word or a false start.
- [2026-10-29 setterintro3min #2] When the speaker starts a sentence, abandons it and restarts it (e.g. "The first one is if you're ever stuck..." then "The first one is I'm gonna be showing you..."), cut the abandoned start on word edges and keep only the restart, unless the abandoned part shows an on-screen step.
- [2026-10-29 setterintro3min #3+#4] The final audio's true peak must be at or below -1.0 dBTP. This is a render setting, not an editing decision: never change the EDL to fix it. If verify still flags audio_peak, list it in REPORT.md.
- [2026-10-29 user, after v2] Leave small natural stutters in (e.g. "assemb-assembling"); they keep the video feeling natural. Only cut a stutter that garbles the meaning.

- [2026-11-03 user, claudesetup] Always clean and normalise the voice (user: "the sound is amazingly perfect"). render.voice_filter in the profile runs on every render before loudnorm: a high-pass at 100 Hz, a room-boom cut, a 3.5 kHz clarity lift, light denoise and gentle compression. This is a render setting, not an editing decision: never change the EDL for it, and never turn it off without the user.

## Proposed
<!-- Added automatically after each run, from verifier problems and your feedback. Nothing here is applied. Move the ones you agree with up to Approved; delete the rest. -->
