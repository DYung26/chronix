"""Project-level task aggregation and normalization."""

from typing import Optional
from dataclasses import dataclass

from chronix.core.models import Task


@dataclass
class ProjectContext:
    """Project identity and metadata."""
    
    project_id: str
    project_name: str
    source: str = "google_docs"
    document_id: Optional[str] = None
    
    def __hash__(self):
        return hash((self.project_id, self.source))
    
    def __eq__(self, other):
        if not isinstance(other, ProjectContext):
            return False
        return self.project_id == other.project_id and self.source == other.source


@dataclass
class AggregatedTask:
    """A task with explicit project context."""
    
    task: Task
    project_context: ProjectContext
    
    def __hash__(self):
        return hash((self.task.id, self.project_context))
    
    def __eq__(self, other):
        if not isinstance(other, AggregatedTask):
            return False
        return self.task.id == other.task.id and self.project_context == other.project_context


class ProjectTodoList:
    """Represents a single project's TODO list with identity."""
    
    def __init__(
        self,
        project_name: str,
        tasks: list[Task],
        project_id: Optional[str] = None,
        source: str = "google_docs",
        document_id: Optional[str] = None
    ):
        self.project_context = ProjectContext(
            project_id=project_id or self._normalize_project_name(project_name),
            project_name=project_name,
            source=source,
            document_id=document_id
        )
        self.tasks = tasks
    
    @staticmethod
    def _normalize_project_name(name: str) -> str:
        """Normalize project name to create stable identifier."""
        normalized = name.lower().strip()
        normalized = "".join(c if c.isalnum() or c in ('-', '_') else '_' for c in normalized)
        normalized = "_".join(filter(None, normalized.split('_')))
        return normalized or "unnamed_project"
    
    def __len__(self):
        return len(self.tasks)
    
    def __repr__(self):
        return f"ProjectTodoList(project='{self.project_context.project_name}', tasks={len(self.tasks)})"


class TaskAggregator:
    """Aggregates tasks from multiple projects into a unified view."""
    
    def aggregate(
        self,
        project_todos: list[ProjectTodoList]
    ) -> list[AggregatedTask]:
        """Aggregate tasks from multiple projects into a single collection."""
        aggregated = []
        
        for project_todo in project_todos:
            for task in project_todo.tasks:
                enriched_task = self._enrich_task_with_project(task, project_todo.project_context)
                
                aggregated_task = AggregatedTask(
                    task=enriched_task,
                    project_context=project_todo.project_context
                )
                aggregated.append(aggregated_task)
        
        return aggregated
    
    def _enrich_task_with_project(self, task: Task, project_context: ProjectContext) -> Task:
        """Enrich task with project information if not already set."""
        if not task.project:
            task.project = project_context.project_name
        
        return task
    
    def get_task_pool(
        self,
        aggregated_tasks: list[AggregatedTask]
    ) -> list[Task]:
        """Extract raw Task objects from aggregated view."""
        return [agg_task.task for agg_task in aggregated_tasks]
    
    def get_tasks_by_project(
        self,
        aggregated_tasks: list[AggregatedTask]
    ) -> dict[str, list[Task]]:
        """Group tasks by project context (project_id + source)."""
        by_project = {}
        
        for agg_task in aggregated_tasks:
            key = f"{agg_task.project_context.project_id}@{agg_task.project_context.source}"
            if key not in by_project:
                by_project[key] = []
            by_project[key].append(agg_task.task)
        
        return by_project
    
    def get_all_projects(
        self,
        aggregated_tasks: list[AggregatedTask]
    ) -> list[ProjectContext]:
        """Get all unique project contexts."""
        seen = set()
        projects = []
        
        for agg_task in aggregated_tasks:
            if agg_task.project_context not in seen:
                seen.add(agg_task.project_context)
                projects.append(agg_task.project_context)
        
        return projects


def aggregate_project_todos(
    project_todos: list[ProjectTodoList]
) -> list[AggregatedTask]:
    """Aggregate multiple project TODO lists into a unified view."""
    aggregator = TaskAggregator()
    return aggregator.aggregate(project_todos)


def create_project_todo(
    project_name: str,
    tasks: list[Task],
    project_id: Optional[str] = None,
    source: str = "google_docs",
    document_id: Optional[str] = None
) -> ProjectTodoList:
    """Create a ProjectTodoList with explicit project identity."""
    return ProjectTodoList(
        project_name=project_name,
        tasks=tasks,
        project_id=project_id,
        source=source,
        document_id=document_id
    )
