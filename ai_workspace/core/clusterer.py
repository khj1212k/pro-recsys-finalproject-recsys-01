"""
Legacy support for clusterer module.
Redirects to the new clustering package.
"""
from .clustering.hdbscan_clusterer import NewsClusterer
from .clustering.split_v2 import decide_split_v2

__all__ = ['NewsClusterer', 'decide_split_v2']