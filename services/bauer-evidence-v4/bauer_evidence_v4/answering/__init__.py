"""Coverage-first grounded answering for Bauer RAG V4."""

from .analysis import TaskAnalyzer
from .service import AnswerService, SourceRegistry

__all__ = ["AnswerService", "SourceRegistry", "TaskAnalyzer"]
