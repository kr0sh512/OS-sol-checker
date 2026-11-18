from __future__ import annotations

from datetime import UTC

from ..domain.policies import display_text


def admin_dashboard(snapshot, transient: dict, archived_solutions: list[dict]) -> dict:
    solutions_by_task = {}
    for row in archived_solutions:
        solution = {
            **row,
            "source": display_text(row["source"]),
            "first_accepted_at": row["first_accepted_at"].astimezone(UTC).strftime("%d.%m.%Y %H:%M UTC"),
        }
        solutions_by_task.setdefault(row["task_id"], []).append(solution)

    task_titles = {
        task.id: task.title
        for topic in snapshot.topics
        for task in topic.tasks
    }
    task_ids = list(task_titles)
    known = set(task_ids)
    task_ids.extend(sorted((set(transient["tasks"]) | set(solutions_by_task)) - known))

    tasks = []
    for task_id in task_ids:
        transient_task = transient["tasks"].get(task_id, {})
        solutions = solutions_by_task.get(task_id, [])
        tasks.append({
            "id": task_id,
            "title": task_titles.get(task_id, task_id),
            "in_catalog": task_id in task_titles,
            "submissions": transient_task.get("submissions", 0),
            "accepted": transient_task.get("accepted", 0),
            "archived_successes": sum(item["successful_submissions"] for item in solutions),
            "solutions": solutions,
        })

    return {
        "total_submissions": transient["total_submissions"],
        "total_users": transient["total_users"],
        "total_successes": sum(task["archived_successes"] for task in tasks),
        "total_solutions": sum(len(task["solutions"]) for task in tasks),
        "tasks": tasks,
    }
