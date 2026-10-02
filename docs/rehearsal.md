# Live rehearsal and integration record

Use a disposable TSH scoreboard and a test OBS scene. Keep auto scoring disarmed until every read and write path has been checked. The current build has **not** been verified against a running TSH or OBS installation on this computer; neither service was running during development. The TSH routes below follow source for joaorb64 TournamentStreamHelper 5.x and are undocumented implementation details.

## TSH route check

Configure `SAS_DEMO=false`, `SAS_TSH_URL`, and `SAS_TSH_SCOREBOARD` for the test scoreboard. Record the installed TSH version and date in your rehearsal notes.

| Action | Route used | Expected observation |
| --- | --- | --- |
| Read scoreboard | `/scoreboardN-get` | Two players, scores, colors, best-of |
| Read current set | `/scoreboardN-get-set` | Stable set ID |
| Read candidate sets | `/get-sets` | List of available set IDs and players |
| Increment score | `/scoreboardN-teamK-scoreup` | Exactly one point, then successful readback |
| Undo/correct score | `/scoreboardN-teamK-scoredown` | Expected prior score, then readback |
| Swap player sides | `/scoreboardN-swap-teams` | Names exchanged on readback |
| Load next set | `/scoreboardN-load-set?set=ID` | Requested ID on readback |

Run each write only against disposable state. Close TSH, try a dashboard refresh and confirm the connection error appears and automation disarms. Restart TSH and confirm reads recover. Test a request timeout and a rejected score update. An uncertain write must leave automation disarmed; reconcile TSH's actual score manually before any new attempt. The application never blindly retries an ambiguous score increment.

## OBS check

Enable OBS WebSocket 5.x and configure `SAS_OBS_HOST`, `SAS_OBS_PORT`, `SAS_OBS_PASSWORD`, and `SAS_OBS_SOURCE`. Verify the selected source screenshot appears in the calibration card. Rename or remove the source, disconnect OBS, and restart OBS. Confirm the dashboard reports the error and automation disarms, then capture resumes when the configured source is available again. Record OBS round-trip latency, capture FPS, analysis FPS, CPU and RAM from the performance panel.

## Set rehearsal

1. Load two players in TSH and verify names, scores, best-of, and player colors in the dashboard.
2. Enter verified Supermajor IDs only if desired; confirm the page name and usage data or an explicit unavailable status.
3. Show gameplay in OBS. Calibrate `p1_character`, `p2_character`, `placement`, `winner_badge`, and `loser_badge` for the exact output layout. Capture local character templates, then check that both HUD slots become stable.
4. Establish P1/P2 mapping with tags, HUD colors, or an operator override. Verify the mapped competitor shown beside the winner panel.
5. Run a game to its result. Confirm a game-end event appears once, then review the winner slot and evidence. If unknown, score manually. For an initial test, keep auto scoring disarmed and compare its proposed decision with the result.
6. Arm only after repeated correct rehearsals. Check the TSH score increments exactly once while a result screen remains visible. Confirm a fresh game needs new HUD observations and the post-game lockout.
7. Switch a character between games, then test a ditto. The result badge method should remain slot based. If the result is brief or ambiguous, it may abstain.
8. Complete the set, inspect the next-set suggestion, load it, and check that old winner and character state reset.
9. Test manual correction and undo. With diagnostics enabled, inspect the saved JSON metadata and crops for manually labeled winner frames.

## Failure cases to record

Random tags; both tags random; swapped TSH sides; Supermajor offline; brief or obscured result; wrong or low-confidence winner; character switch; ditto; OBS source missing or renamed; OBS restart; TSH restart; TSH timeout; uncertain score write; manual correction; undo; persistent result screen; and ambiguous next-set candidates.

## Known limits

The tested result geometry and red/blue slot badges come from one 16:9 broadcast. Recalibrate and replay every new layout. The current method has three selected labeled game windows (P1, P1, P2), and no timeout or SD-specific validation. At 5 FPS it detected all three; at 2.5 FPS it missed one brief result. Keep manual scoring available. The optional diagnostics directory is ignored by Git because it contains captured game frames.
