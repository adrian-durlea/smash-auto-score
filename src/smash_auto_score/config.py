from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="SAS_", env_file=".env", extra="ignore")
    demo: bool = True
    obs_host: str = "localhost"
    obs_port: int = 4455
    obs_password: str = ""
    obs_source: str = ""
    recorded_video: str = ""
    calibration_path: str = "config/calibration.json"
    tsh_url: str = "http://127.0.0.1:5000"
    tsh_scoreboard: int = 1
    startgg_token: str = ""
    startgg_tournament_slug: str = ""
    startgg_stream_name: str = ""
    min_mapping_confidence: float = 0.95
    min_winner_confidence: float = 0.97
    min_set_confidence: float = 0.97
    min_set_margin: float = 0.20
    post_set_delay_seconds: float = 15.0
    database_path: str = "autoscore.db"
    frame_width: int = 960
    frame_height: int = 540
    frame_fps: float = 5.0
    ocr_interval: float = 1.0
    color_interval: float = 0.5
