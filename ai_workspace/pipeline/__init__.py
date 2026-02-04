"""
Pipeline 모듈러
"""
from pipeline.stages import (
    PipelineStage,
    Stage0_UserEmbedding,
    Stage1_RSSCollection,
    Stage2_ContentExtraction,
    Stage3_NewsEmbedding,
    # Stage4_Clustering,
    Stage5_NewsletterGeneration,
    # Stage6_NewsletterEmbedding,
)
from pipeline.runner import PipelineRunner

__all__ = [
    "PipelineStage",
    "Stage0_UserEmbedding",
    "Stage1_RSSCollection",
    "Stage2_ContentExtraction",
    "Stage3_NewsEmbedding",
    "Stage4_Clustering",
    "Stage5_NewsletterGeneration",
    "Stage6_NewsletterEmbedding",
    "PipelineRunner",
]
