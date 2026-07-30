"""20 built-in avatar icons (static/avatars/built_in/*.svg, generated once as
a batch of game-themed art) plus the upload constraints for a custom avatar,
unlocked at job_tier==4 (see character.character_change_avatar)."""
import json
import os

_STATIC_AVATARS_DIR = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "static", "avatars",
)
_MANIFEST_PATH = os.path.join(_STATIC_AVATARS_DIR, "built_in", "manifest.json")
CUSTOM_AVATAR_UPLOAD_DIR = os.path.join(_STATIC_AVATARS_DIR, "uploads")

with open(_MANIFEST_PATH, "r", encoding="utf-8") as _f:
    BUILT_IN_AVATARS = json.load(_f)

BUILT_IN_AVATAR_KEYS = {entry["key"] for entry in BUILT_IN_AVATARS}
DEFAULT_AVATAR_KEY = BUILT_IN_AVATARS[0]["key"]

# Custom avatar upload constraints (job_tier==4 only). Every accepted upload
# is re-encoded through Pillow into a fixed-size square PNG (never trusting
# the uploaded bytes/extension as-is) -- see character.character_change_avatar.
CUSTOM_AVATAR_MAX_BYTES = 2 * 1024 * 1024  # 2MB
CUSTOM_AVATAR_ALLOWED_FORMATS = {"JPEG", "PNG", "GIF"}
CUSTOM_AVATAR_DIMENSION = 200
CUSTOM_AVATAR_FORMAT_HINT = (
    f"圖片格式限 JPG / PNG / GIF，檔案大小上限 2MB；"
    f"建議使用正方形圖片，系統會自動置中裁切並縮放為 "
    f"{CUSTOM_AVATAR_DIMENSION}x{CUSTOM_AVATAR_DIMENSION}"
)
