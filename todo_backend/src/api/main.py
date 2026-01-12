from __future__ import annotations

import os
import re
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from fastapi import FastAPI, HTTPException, Path
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field


def _utc_now_iso() -> str:
    """Return current UTC time as ISO-8601 string with Z suffix."""
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _parse_db_path_from_connection_file(file_contents: str) -> str:
    """
    Parse the SQLite DB file path from db_connection.txt.

    Expected format includes a line like:
    # File path: /abs/path/to/myapp.db
    """
    print("This is parse db file")
    match = re.search(r"^\s*#\s*File path:\s*(.+?)\s*$", file_contents, flags=re.MULTILINE)
    if not match:
        raise ValueError("Could not find '# File path:' entry in db_connection.txt")
    return match.group(1).strip()


def _load_sqlite_db_path() -> str:
    """
    Load SQLite database path from ../database/db_connection.txt (relative to backend container root).
    """
    # This file lives at: todo_backend/src/api/main.py
    # We want: task-tracker-.../database/db_connection.txt which is sibling of todo_backend.
    container_root = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
    db_connection_txt = os.path.abspath(os.path.join(container_root, "..", "database", "db_connection.txt"))
    print("This is sqlite db path")
    try:
        with open(db_connection_txt, "r", encoding="utf-8") as f:
            contents = f.read()
    except FileNotFoundError as e:
        raise RuntimeError(
            f"db_connection.txt not found at expected location: {db_connection_txt}. "
            "Ensure the database container initialized and wrote db_connection.txt."
        ) from e

    db_path = _parse_db_path_from_connection_file(contents)
    if not os.path.isabs(db_path):
        # Should be absolute per generator, but guard just in case.
        db_path = os.path.abspath(os.path.join(os.path.dirname(db_connection_txt), db_path))
    return db_path


DB_PATH = _load_sqlite_db_path()


@contextmanager
def _get_conn() -> sqlite3.Connection:
    """
    Context manager for a SQLite connection with safe defaults.

    - Uses row_factory to access columns by name.
    - Ensures foreign_keys pragma is on (harmless for our schema).
    """
    conn = sqlite3.connect(DB_PATH, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    try:
        conn.execute("PRAGMA foreign_keys = ON;")
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def _row_to_task_dict(row: sqlite3.Row) -> Dict[str, Any]:
    """Convert a tasks row to API response shape."""
    return {
        "id": int(row["id"]),
        "title": str(row["title"]),
        "completed": bool(int(row["completed"])),
        "created_at": str(row["created_at"]),
        "updated_at": str(row["updated_at"]),
    }


class TaskBase(BaseModel):
    """Common fields for tasks."""
    title: str = Field(..., min_length=1, max_length=500, description="Task title/description.")
    completed: bool = Field(False, description="Whether the task is completed.")


class TaskCreate(BaseModel):
    """Payload to create a task."""
    title: str = Field(..., min_length=1, max_length=500, description="Task title/description.")
    completed: bool = Field(False, description="Whether the task is completed. Defaults to false.")


class TaskUpdate(BaseModel):
    """Payload to update a task. All fields optional."""
    title: Optional[str] = Field(None, min_length=1, max_length=500, description="Updated task title.")
    completed: Optional[bool] = Field(None, description="Updated completion status.")

    # Pydantic v2: simplest validation is handled by Field constraints.
    # Additional semantic validation happens in endpoint (must provide at least one field).


class Task(TaskBase):
    """Task model returned by API."""
    id: int = Field(..., description="Task ID.")
    created_at: str = Field(..., description="Creation timestamp (ISO-8601).")
    updated_at: str = Field(..., description="Last update timestamp (ISO-8601).")


openapi_tags = [
    {"name": "Health", "description": "Health check endpoints."},
    {"name": "Tasks", "description": "CRUD operations for to-do tasks."},
]


app = FastAPI(
    title="To-Do Backend API",
    description=(
        "FastAPI backend for a simple to-do application.\n\n"
        "Uses SQLite persistence. Database file path is read from ../database/db_connection.txt."
    ),
    version="0.1.0",
    openapi_tags=openapi_tags,
)

# CORS: allow frontend on port 3000 (as requested). Keep permissive defaults for dev safety.
app.add_middleware(
    CORSMiddleware,
    allow_origins=[
        "http://localhost:3000",
        "http://127.0.0.1:3000",
    ],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


# PUBLIC_INTERFACE
@app.get("/", tags=["Health"], summary="Health check", description="Returns a simple health check payload.")
def health_check() -> Dict[str, str]:
    """Health check endpoint used by orchestrators and smoke tests."""
    return {"message": "Healthy"}


def _ensure_tasks_table_exists() -> None:
    """Best-effort check to fail early if DB schema is missing."""
    with _get_conn() as conn:
        row = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name=?",
            ("tasks",),
        ).fetchone()
        if not row:
            raise RuntimeError(
                "SQLite database is reachable but tasks table does not exist. "
                "Run database/init_db.py in the database container workspace."
            )


# Validate DB at import time so the API fails fast in CI if misconfigured.
_ensure_tasks_table_exists()


# PUBLIC_INTERFACE
@app.get(
    "/tasks",
    response_model=List[Task],
    tags=["Tasks"],
    summary="List tasks",
    description="Returns all tasks ordered by id descending (most recent first).",
    operation_id="listTasks",
)
def list_tasks() -> List[Task]:
    """Return all tasks."""
    with _get_conn() as conn:
        rows = conn.execute(
            "SELECT id, title, completed, created_at, updated_at FROM tasks ORDER BY id DESC"
        ).fetchall()
    return [Task(**_row_to_task_dict(r)) for r in rows]


# PUBLIC_INTERFACE
@app.post(
    "/tasks",
    response_model=Task,
    status_code=201,
    tags=["Tasks"],
    summary="Create task",
    description="Creates a new task and returns it.",
    operation_id="createTask",
)
def create_task(payload: TaskCreate) -> Task:
    """Create a task with the provided title and optional completed status."""
    now = _utc_now_iso()
    completed_int = 1 if payload.completed else 0

    with _get_conn() as conn:
        cur = conn.execute(
            """
            INSERT INTO tasks (title, completed, created_at, updated_at)
            VALUES (?, ?, ?, ?)
            """,
            (payload.title, completed_int, now, now),
        )
        task_id = cur.lastrowid
        row = conn.execute(
            "SELECT id, title, completed, created_at, updated_at FROM tasks WHERE id=?",
            (task_id,),
        ).fetchone()

    if not row:
        # Extremely unlikely, but handle gracefully.
        raise HTTPException(status_code=500, detail="Task creation failed.")
    return Task(**_row_to_task_dict(row))


# PUBLIC_INTERFACE
@app.get(
    "/tasks/{id}",
    response_model=Task,
    tags=["Tasks"],
    summary="Get task",
    description="Returns a single task by id.",
    operation_id="getTask",
)
def get_task(
    id: int = Path(..., ge=1, description="Task ID."),
) -> Task:
    """Fetch a task by ID."""
    with _get_conn() as conn:
        row = conn.execute(
            "SELECT id, title, completed, created_at, updated_at FROM tasks WHERE id=?",
            (id,),
        ).fetchone()

    if not row:
        raise HTTPException(status_code=404, detail="Task not found.")
    return Task(**_row_to_task_dict(row))


# PUBLIC_INTERFACE
@app.put(
    "/tasks/{id}",
    response_model=Task,
    tags=["Tasks"],
    summary="Update task",
    description="Updates a task's title and/or completed status.",
    operation_id="updateTask",
)
def update_task(
    payload: TaskUpdate,
    id: int = Path(..., ge=1, description="Task ID."),
) -> Task:
    """Update an existing task by ID."""
    if payload.title is None and payload.completed is None:
        raise HTTPException(status_code=422, detail="At least one field (title or completed) must be provided.")

    with _get_conn() as conn:
        existing = conn.execute(
            "SELECT id, title, completed, created_at, updated_at FROM tasks WHERE id=?",
            (id,),
        ).fetchone()
        if not existing:
            raise HTTPException(status_code=404, detail="Task not found.")

        new_title = payload.title if payload.title is not None else str(existing["title"])
        new_completed_int = (
            (1 if payload.completed else 0) if payload.completed is not None else int(existing["completed"])
        )
        now = _utc_now_iso()

        conn.execute(
            """
            UPDATE tasks
               SET title = ?,
                   completed = ?,
                   updated_at = ?
             WHERE id = ?
            """,
            (new_title, new_completed_int, now, id),
        )

        row = conn.execute(
            "SELECT id, title, completed, created_at, updated_at FROM tasks WHERE id=?",
            (id,),
        ).fetchone()

    if not row:
        raise HTTPException(status_code=500, detail="Task update failed.")
    return Task(**_row_to_task_dict(row))


# PUBLIC_INTERFACE
@app.delete(
    "/tasks/{id}",
    status_code=204,
    tags=["Tasks"],
    summary="Delete task",
    description="Deletes a task by id.",
    operation_id="deleteTask",
)
def delete_task(
    id: int = Path(..., ge=1, description="Task ID."),
) -> None:
    """Delete a task by ID."""
    with _get_conn() as conn:
        cur = conn.execute("DELETE FROM tasks WHERE id=?", (id,))
        if cur.rowcount == 0:
            raise HTTPException(status_code=404, detail="Task not found.")
    return None


# PUBLIC_INTERFACE
@app.post(
    "/tasks/clear-completed",
    tags=["Tasks"],
    summary="Clear completed tasks",
    description="Deletes all tasks with completed=true and returns how many were removed.",
    operation_id="clearCompletedTasks",
)
def clear_completed_tasks() -> Dict[str, int]:
    """Delete all completed tasks."""
    with _get_conn() as conn:
        cur = conn.execute("DELETE FROM tasks WHERE completed = ?", (1,))
        deleted = int(cur.rowcount if cur.rowcount is not None else 0)
    return {"deleted": deleted}
