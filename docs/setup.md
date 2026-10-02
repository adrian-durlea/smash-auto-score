# Setup wizard and integration validation

Open `/setup` after starting TSH, OBS, and AutoScore. The wizard runs local read-only discovery, lets you choose the right endpoints, saves selections in the Git-ignored `.env`, and applies them immediately. `python -m smash_auto_score.integration_check` provides a read-only command-line report.

## Automated checks

| Integration | Automatic work | Operator input only when needed |
| --- | --- | --- |
| TSH | Probe configured address and common loopback ports, read scoreboard/set/players/scores/colors/best-of/candidate sets, display tested capabilities. | Choose among multiple instances; load a disposable two-player set. |
| OBS | Probe configured and standard local WebSocket ports, detect authentication, list scenes and inputs, rank likely gameplay capture inputs, test three screenshots, preview the last image, measure latency, and run calibrated CV on it. | Enable OBS WebSocket if disabled, provide its password, choose a source if unclear, review the frame. |
| Start.gg | Validate a supplied token with a read-only user query, parse a tournament URL, query stream queues and entrant names, and list streams. | Generate and paste a personal API token; supply a tournament URL if TSH does not provide one; choose the intended stream. |
| Calibration | Display existing profiles and suggest the active saved profile for review. | Check result/gameplay regions against the actual OBS image and adjust them in the dashboard. |
| Readiness | Separate blocking conditions from warnings. Start.gg and Supermajor are optional for core scoring. | Run a real gameplay rehearsal before considering auto scoring. |

The TSH score write, swap, and set-load capabilities remain **NOT TESTED** during read-only discovery. A separate disposable score test changes one chosen side by one point, reads it back, restores the original score, and reads it back again. If either step is uncertain, AutoScore stays disarmed and asks for TSH review. The wizard never intentionally restarts TSH or OBS.

## Shadow rehearsal

Press **Start shadow rehearsal** on `/setup`. This disarms auto scoring and blocks AutoScore score writes, player swaps, and set loads. Video detection, mapping, end/winner detection, and next-set ranking continue. A confident decision produces a `would_score` event naming the side and competitor. Save the JSON and Markdown summary with **Save rehearsal report**; the files remain local under ignored `diagnostics/`.

If TSH changes outside AutoScore, the controller disarms and reports the expected and observed scores. **Accept current external score** acknowledges the operator's reviewed correction without reverting it. **Check uncertain write** reads TSH and compares it with the pending transaction; it never retries blindly. A set change or unexpected third score remains for manual review.

## Current machine observation, 2026-10-02

The extracted TSH installation at `Downloads/TournamentStreamHelper-main/TournamentStreamHelper-main` contained `user_data/settings.json`. Launching its EXE with that directory as its working directory avoided the missing-file startup error. The running TSH web server answered read-only scoreboard and set requests on `127.0.0.1:5500`. It reported set ID `0` and no players; candidate-set reading timed out without a loaded tournament. No score mutation was attempted. OBS was not running, and no Start.gg token was available, so live OBS and Start.gg validation remain pending.

Start.gg token handling and stream-queue fields follow the official [authentication](https://developer.start.gg/docs/authentication/), [request format](https://developer.start.gg/docs/sending-requests/), and [stream queue](https://developer.start.gg/docs/examples/queries/stream-queue/) documentation. The installed TSH's web routes remain implementation details; the wizard reports version as unknown when the server does not expose it.
