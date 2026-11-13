from pathlib import Path
from functools import lru_cache
import yaml
from pydantic import BaseModel, Field

CONFIG_PATH = Path(__file__).resolve().parents[1] / "config.yaml"

class ServerConfig(BaseModel):
    host: str = "127.0.0.1"
    port: int = 8010
    api_key: str = ""

class OmlxConfig(BaseModel):
    base_url: str = "http://127.0.0.1:8000"
    api_key: str = ""
    timeout_seconds: int = 300

class ModelConfig(BaseModel):
    reasoning: str
    speech: str

class AudioConfig(BaseModel):
    client_sample_rate: int = 24000
    max_input_seconds: int = 90
    output_mime_type: str = "audio/pcm"

class VisionConfig(BaseModel):
    # The configured reasoning model is Qwen3.6 VLM. Advertise image input to
    # Ask AI so its local screen-share control is available in remote mode.
    enabled: bool = True
    max_images_per_turn: int = 4
    max_image_bytes: int = 8 * 1024 * 1024

class ConversationConfig(BaseModel):
    max_history_turns: int = 20
    system_prompt: str = "You are a helpful voice assistant."

class Config(BaseModel):
    server: ServerConfig = Field(default_factory=ServerConfig)
    omlx: OmlxConfig = Field(default_factory=OmlxConfig)
    models: ModelConfig
    audio: AudioConfig = Field(default_factory=AudioConfig)
    conversation: ConversationConfig = Field(default_factory=ConversationConfig)
    vision: VisionConfig = Field(default_factory=VisionConfig)

@lru_cache
def get_config() -> Config:
    with CONFIG_PATH.open("r", encoding="utf-8") as f:
        return Config.model_validate(yaml.safe_load(f))
