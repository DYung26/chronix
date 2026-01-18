"""Abstract interface for task source integrations."""

from abc import ABC, abstractmethod
from typing import List, Any


class TaskSourceIntegration(ABC):
    """Abstract base class for integrating external task sources."""
    
    @abstractmethod
    def fetch_tasks(self) -> List[Any]:
        """Fetch tasks from the external source."""
        pass
    
    @abstractmethod
    def authenticate(self) -> bool:
        """Authenticate with the external source."""
        pass
    
    @abstractmethod
    def validate_connection(self) -> bool:
        """Validate connection to the external source."""
        pass
