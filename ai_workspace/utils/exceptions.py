"""
Custom exception classes for AI Workspace

Provides a hierarchy of exceptions for better error handling and debugging.
All custom exceptions inherit from AIWorkspaceError for easy catching.
"""


class AIWorkspaceError(Exception):
    """Base exception for all AI Workspace errors"""
    pass


class DatabaseError(AIWorkspaceError):
    """Database operation failed"""
    pass


class EmbeddingError(AIWorkspaceError):
    """Embedding generation or processing failed"""
    pass


class ClusteringError(AIWorkspaceError):
    """Clustering operation failed"""
    pass


class LLMError(AIWorkspaceError):
    """LLM API call failed"""
    pass


class RateLimitError(LLMError):
    """LLM API rate limit exceeded (429)"""
    pass


class ValidationError(AIWorkspaceError):
    """Data validation failed"""
    pass


class CrawlerError(AIWorkspaceError):
    """Web crawling or content extraction failed"""
    pass


class WorkflowError(AIWorkspaceError):
    """LangGraph workflow execution failed"""
    pass
