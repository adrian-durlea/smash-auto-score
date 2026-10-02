"""Read-only integration check for the current tournament computer."""

import asyncio
from pathlib import Path

from .config import Settings
from .setup_wizard import discover_obs, discover_tsh, probe_startgg, readiness, test_obs_frame


async def check() -> dict:
    settings = Settings()
    tsh, obs = await asyncio.gather(discover_tsh(settings), discover_obs(settings))
    frame = None
    if settings.obs_source and any(item["status"] == "PASS" for item in obs):
        frame, _ = await test_obs_frame(settings.obs_host, settings.obs_port,
                                        settings.obs_password, settings.obs_source)
    startgg = await probe_startgg(settings.startgg_token, settings.startgg_tournament_slug,
                                   settings.startgg_stream_name)
    tsh_status = ("PASS" if any(item.get("capabilities", {}).get("current_set") == "PASS"
                                and item.get("capabilities", {}).get("players") == "PASS"
                                for item in tsh) else "CONNECTED_NO_SET" if tsh else "FAIL")
    obs_status = ("PASS" if frame and frame["status"] == "PASS" else
                  "SOURCE_NOT_CONFIGURED" if obs else "FAIL")
    return {"TSH": tsh_status, "OBS": obs_status, "Start.gg": startgg["status"],
            "Calibration": "PASS" if Path(settings.calibration_path).exists() else "REVIEW",
            "readiness": readiness(tsh, obs, frame, startgg, Path(settings.calibration_path), settings)}


def main() -> None:
    result = asyncio.run(check())
    for name in ("TSH", "OBS", "Start.gg", "Calibration"):
        print(f"{name:12} {result[name]}")
    ready = result["readiness"]
    print("\n" + ready["status"].replace("_", " "))
    for issue in ready["blocking"]:
        print(f"Blocking: {issue}")
    for issue in ready["warnings"]:
        print(f"Warning: {issue}")


if __name__ == "__main__":
    main()
