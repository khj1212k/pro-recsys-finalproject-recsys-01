# src/utils/__init__.py
from .common import (
    get_project_root,
    load_config,
    get_data_path,
    get_logger,       # [수정] setup_logging -> get_logger
    save_json,
    load_json,
    ensure_dir
)

from .embedder import (
    BGEEmbedder,
    get_embedder,
    embed_text,
    embed_texts
)