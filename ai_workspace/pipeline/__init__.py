"""
Pipeline module

Provides modular, class-based implementation of each pipeline stage
for better separation of concerns and testability.
"""
from pipeline.stages import (
    PipelineStage,
    Stage0_UserEmbedding,
    Stage1_RSSCollection,
    Stage2_ContentExtraction,
    Stage3_NewsEmbedding,
    Stage5_NewsletterGeneration,
)
from pipeline.runner import PipelineRunner

__all__ = [
    "PipelineStage",
    "Stage0_UserEmbedding",
    "Stage1_RSSCollection",
    "Stage2_ContentExtraction",
    "Stage3_NewsEmbedding",
    "Stage5_NewsletterGeneration",
    "PipelineRunner",
]
