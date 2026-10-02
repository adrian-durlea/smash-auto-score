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
    tsh_url: str = "http://127.0.0.1:5500"
    tsh_scoreboard: int = 1
    startgg_token: str = ""
    startgg_tournament_slug: str = ""
    startgg_stream_name: str = ""
    supermajor_enabled: bool = False
    supermajor_cache_ttl_days: int = 7
    supermajor_timeout_seconds: float = 10.0
    supermajor_min_games: int = 20
    min_mapping_confidence: float = 0.95
    min_winner_confidence: float = 0.97
    game_end_confidence_threshold: float = 0.97
    game_end_confirmation_frames: int = 2
    winner_confirmation_frames: int = 2
    new_game_confirmation_frames: int = 3
    post_game_lockout_seconds: float = 5.0
    performance_telemetry_enabled: bool = True
    winner_diagnostics_enabled: bool = False
    winner_diagnostics_path: str = "diagnostics/winner"
    min_set_confidence: float = 0.97
    min_set_margin: float = 0.20
    post_set_delay_seconds: float = 15.0
    database_path: str = "autoscore.db"
    frame_width: int = 960
    frame_height: int = 540
    frame_fps: float = 5.0
    ocr_interval: float = 1.0
    color_interval: float = 0.5
    character_template_path: str = "config/character_templates"
    character_interval: float = 0.5
