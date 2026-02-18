"""Task dependency graph validation and topological sorting."""

from typing import Optional
from collections import defaultdict, deque

from chronix.core.models import Task


class DependencyError(Exception):
    """Raised when dependency graph validation fails."""
    pass


class DependencyValidator:
    """Validates task dependency graphs for cycles and correctness."""

    def validate(self, tasks: list[Task]) -> None:
        """
        Validate the entire dependency graph.
        
        Checks for:
        - Unknown dependency references
        - Duplicate refs
        - Self-dependencies
        - Circular dependencies (cycles)
        
        Args:
            tasks: List of all tasks to validate
            
        Raises:
            DependencyError: If any validation rule is violated
        """
        self._validate_duplicate_refs(tasks)
        self._validate_self_dependencies(tasks)
        self._validate_all_dependencies_exist(tasks)
        self._validate_no_cycles(tasks)

    def _validate_duplicate_refs(self, tasks: list[Task]) -> None:
        """Check for duplicate ref values across tasks."""
        seen_refs = set()
        for task in tasks:
            if task.ref:
                if task.ref in seen_refs:
                    raise DependencyError(f"Duplicate ref '{task.ref}' found in multiple tasks")
                seen_refs.add(task.ref)

    def _validate_self_dependencies(self, tasks: list[Task]) -> None:
        """Check for tasks that depend on themselves."""
        for task in tasks:
            if task.ref and task.ref in task.depends_on:
                raise DependencyError(
                    f"Task with ref '{task.ref}' ({task.title}) cannot depend on itself"
                )

    def _validate_all_dependencies_exist(self, tasks: list[Task]) -> None:
        """Check that all referenced dependencies exist."""
        ref_to_task = {task.ref: task for task in tasks if task.ref}
        
        for task in tasks:
            for dep_ref in task.depends_on:
                if dep_ref not in ref_to_task:
                    raise DependencyError(
                        f"Task '{task.title}' (ref='{task.ref}') depends on unknown ref '{dep_ref}'"
                    )

    def _validate_no_cycles(self, tasks: list[Task]) -> None:
        """
        Detect cycles in the dependency graph using DFS.
        
        Raises:
            DependencyError: If a cycle is detected
        """
        ref_to_task = {task.ref: task for task in tasks if task.ref}
        visited = set()
        rec_stack = set()

        def dfs(ref: str, path: list[str]) -> None:
            if ref in rec_stack:
                cycle_start = path.index(ref)
                cycle = path[cycle_start:] + [ref]
                raise DependencyError(
                    f"Circular dependency detected: {' -> '.join(cycle)}"
                )
            
            if ref in visited:
                return
            
            visited.add(ref)
            rec_stack.add(ref)
            
            task = ref_to_task.get(ref)
            if task:
                for dep_ref in task.depends_on:
                    dfs(dep_ref, path + [ref])
            
            rec_stack.remove(ref)

        for task in tasks:
            if task.ref and task.ref not in visited:
                dfs(task.ref, [])


class DependencyResolver:
    """Resolves task ordering with dependencies using topological sort."""

    def topological_sort(self, tasks: list[Task]) -> list[Task]:
        """
        Perform topological sort on tasks while preserving relative order of independent tasks.
        
        Uses Kahn's algorithm to maintain the baseline order for independent tasks.
        
        Args:
            tasks: Ordered list of tasks (baseline priority order)
            
        Returns:
            List of tasks sorted to respect dependencies while maintaining relative order
            of independent tasks
        """
        ref_to_task = {task.ref: task for task in tasks if task.ref}
        task_by_id = {task.id: task for task in tasks}
        
        in_degree = defaultdict(int)
        adj_list = defaultdict(list)
        
        task_set = set(task.id for task in tasks)
        
        for task in tasks:
            in_degree[task.id] = 0
        
        for task in tasks:
            for dep_ref in task.depends_on:
                if dep_ref in ref_to_task:
                    dep_task = ref_to_task[dep_ref]
                    if dep_task.id in task_set:
                        adj_list[dep_task.id].append(task.id)
                        in_degree[task.id] += 1
        
        queue = deque([t.id for t in tasks if in_degree[t.id] == 0])
        
        sorted_ids = []
        while queue:
            task_id = queue.popleft()
            sorted_ids.append(task_id)
            
            for neighbor_id in adj_list[task_id]:
                in_degree[neighbor_id] -= 1
                if in_degree[neighbor_id] == 0:
                    queue.append(neighbor_id)
        
        result = [task_by_id[task_id] for task_id in sorted_ids if task_id in task_by_id]
        return result


def resolve_task_dependencies(tasks: list[Task]) -> list[Task]:
    """
    Validate and resolve task dependencies.
    
    Validates the dependency graph, then returns tasks in dependency-resolved order
    while preserving the relative order of independent tasks.
    
    Args:
        tasks: Ordered list of tasks
        
    Returns:
        Dependency-resolved ordered list of tasks
        
    Raises:
        DependencyError: If dependency graph is invalid
    """
    validator = DependencyValidator()
    validator.validate(tasks)
    
    resolver = DependencyResolver()
    return resolver.topological_sort(tasks)
