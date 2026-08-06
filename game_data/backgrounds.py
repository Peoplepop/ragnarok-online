"""Admin-uploadable presentation assets: 14 background image slots (game/
battle per-element + neutral, tournament, shop) and one global background
music track. Same admin-override convention as game_data/avatars.py -- pure
filesystem existence checks, no DB column tracks whether an override exists.
See web_helpers.py's background_url()/bgm_url() (read path) and
blueprints/admin.py's /admin/backgrounds routes (write path)."""
import os

_STATIC_DIR = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "static",
)
BACKGROUND_CUSTOM_DIR = os.path.join(_STATIC_DIR, "backgrounds", "custom")
BGM_DIR = os.path.join(_STATIC_DIR, "audio")

# countries.element ("金"/"木"/"水"/"火"/"土") -> ASCII slug used in both the
# background image_key naming (e.g. "game_bg_jin") and filenames on disk, to
# keep filesystem/URL handling simple (matches this codebase's existing
# ASCII monster image_key convention). A tile with no owning country (or an
# unrecognized element) falls back to "neutral" wherever this is looked up.
ELEMENT_TO_BG_SLUG = {"金": "jin", "木": "mu", "水": "shui", "火": "huo", "土": "tu"}

# The 14 background image slots, key -> admin-facing Chinese label. Validated
# as a fixed allow-list before any key ever touches a filesystem path (same
# rule as _known_monster_image_keys()/BUILT_IN_AVATAR_KEYS).
BACKGROUND_KEYS = {
    "game_bg_jin": "遊戲畫面 - 金國",
    "game_bg_mu": "遊戲畫面 - 木國",
    "game_bg_shui": "遊戲畫面 - 水國",
    "game_bg_huo": "遊戲畫面 - 火國",
    "game_bg_tu": "遊戲畫面 - 土國",
    "game_bg_neutral": "遊戲畫面 - 未佔領區域",
    "battle_bg_jin": "戰鬥畫面 - 金國",
    "battle_bg_mu": "戰鬥畫面 - 木國",
    "battle_bg_shui": "戰鬥畫面 - 水國",
    "battle_bg_huo": "戰鬥畫面 - 火國",
    "battle_bg_tu": "戰鬥畫面 - 土國",
    "battle_bg_neutral": "戰鬥畫面 - 未佔領區域",
    "tournament_bg": "天下武道大會",
    "shop_bg": "商店",
    "login_bg": "登入頁",
    "register_bg": "註冊頁",
}

# Background uploads are never cropped to a square (unlike avatars/monster
# art) -- downscaled to fit within these bounds, aspect ratio preserved,
# CSS background-size:cover handles the rest on render. Re-encoded to JPEG
# regardless of input format (no alpha needed for a full-bleed photo).
BACKGROUND_MAX_BYTES = 5 * 1024 * 1024  # 5MB
BACKGROUND_MAX_WIDTH = 1920
BACKGROUND_MAX_HEIGHT = 1080
BACKGROUND_ALLOWED_FORMATS = {"JPEG", "PNG", "GIF", "WEBP"}
BACKGROUND_FORMAT_HINT = (
    f"圖片格式限 JPG / PNG / GIF / WEBP，檔案大小上限 "
    f"{BACKGROUND_MAX_BYTES // (1024 * 1024)}MB，系統會自動縮放至 "
    f"{BACKGROUND_MAX_WIDTH}x{BACKGROUND_MAX_HEIGHT} 以內（保持原比例，不裁切）"
)

# Background music: no audio-processing library in requirements.txt, so an
# accepted upload is stored as-is after validation (see
# web_helpers._sniff_audio_format) rather than re-encoded. Generous cap for a
# compressed loop track; only one file is ever kept on disk at a time (see
# admin_upload_bgm's delete-other-extensions step).
BGM_MAX_BYTES = 10 * 1024 * 1024  # 10MB
BGM_EXTENSIONS = ("mp3", "ogg", "wav")
BGM_CONTENT_TYPES = {"mp3": "audio/mpeg", "ogg": "audio/ogg", "wav": "audio/wav"}
BGM_FORMAT_HINT = f"音樂格式限 MP3 / OGG / WAV，檔案大小上限 {BGM_MAX_BYTES // (1024 * 1024)}MB"
