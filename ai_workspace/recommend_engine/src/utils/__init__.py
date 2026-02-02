# src/utils/__init__.py
"""
Utilities 패키지
"""

from .common import (
    get_project_root,
    get_data_path,
    load_config,
    setup_logging,
    ensure_dir,
    save_json,
    load_json
)

# Lazy import for embedder (requires torch)
def get_embedder(*args, **kwargs):
    from .embedder import get_embedder as _get_embedder
    return _get_embedder(*args, **kwargs)

def embed_text(*args, **kwargs):
    from .embedder import embed_text as _embed_text
    return _embed_text(*args, **kwargs)

def embed_texts(*args, **kwargs):
    from .embedder import embed_texts as _embed_texts
    return _embed_texts(*args, **kwargs)

__all__ = [
    'get_embedder',
    'embed_text',
    'embed_texts',
    'get_project_root',
    'get_data_path',
    'load_config',
    'setup_logging',
    'ensure_dir',
    'save_json',
    'load_json',
]
