"""Task 작업 구조: Task 상태판(상태기계) + 렌더링.

Project=Channel, Task=Thread. Task의 생명주기를 상태기계로 관리하고,
단계마다 상태판(System 봇이 갱신하는 메시지)을 렌더링한다.

flow: 생성 → 리더확정 → 목표확정 → 분배(Todo) → 완료 → 보고
"""
from dataclasses import dataclass
from enum import Enum
from typing import List, Optional


class Status(str, Enum):
    CREATED = "생성"
    LEADER = "리더확정"
    GOAL = "목표확정"
    ASSIGNED = "분배"
    DONE = "완료"
    REPORTED = "보고"


class TaskError(Exception):
    """Task 흐름 규약 위반."""


@dataclass
class Todo:
    todo_id: int
    text: str
    assignee: Optional[str] = None
    done: bool = False


class TaskBoard:
    """Task 하나의 생명주기 + 상태판."""

    def __init__(self, task_id, title: str):
        self.task_id = str(task_id)
        self.title = title
        self.status = Status.CREATED
        self.leader: Optional[str] = None
        self.goal: Optional[str] = None
        self.todos: List[Todo] = []
        self.report_text: Optional[str] = None
        self._seq = 0
        self.history: List[Status] = [Status.CREATED]

    def _advance(self, status: Status):
        self.status = status
        self.history.append(status)

    def set_leader(self, leader: str):
        if self.status != Status.CREATED:
            raise TaskError("리더는 생성 직후에만 정합니다.")
        self.leader = leader
        self._advance(Status.LEADER)

    def set_goal(self, goal: str):
        if self.status != Status.LEADER:
            raise TaskError("목표는 리더 확정 후에 정합니다.")
        self.goal = goal
        self._advance(Status.GOAL)

    def add_todo(self, text: str, assignee: Optional[str] = None) -> Todo:
        if self.status not in (Status.GOAL, Status.ASSIGNED):
            raise TaskError("Todo는 목표 확정 후에 추가합니다.")
        self._seq += 1
        todo = Todo(self._seq, text, assignee)
        self.todos.append(todo)
        return todo

    def assign(self, todo_id: int, assignee: str):
        self._todo(todo_id).assignee = assignee
        if self.status == Status.GOAL:
            self._advance(Status.ASSIGNED)

    def complete(self, todo_id: int):
        self._todo(todo_id).done = True
        if self.todos and all(t.done for t in self.todos):
            self._advance(Status.DONE)

    def report(self, text: str):
        if self.status != Status.DONE:
            raise TaskError("보고는 완료(모든 Todo done) 후에 합니다.")
        self.report_text = text
        self._advance(Status.REPORTED)

    def _todo(self, todo_id: int) -> Todo:
        for t in self.todos:
            if t.todo_id == todo_id:
                return t
        raise TaskError(f"Todo {todo_id} 가 없습니다.")

    def render(self) -> str:
        """System 봇이 올리는 상태판 텍스트."""
        lines = [
            f"📋 Task #{self.task_id} — {self.title}",
            f"상태: {self.status.value}",
            f"리더: {self.leader or '(모집중)'}",
            f"목표: {self.goal or '-'}",
        ]
        if self.todos:
            lines.append("Todo:")
            for t in self.todos:
                box = "x" if t.done else " "
                who = f" @{t.assignee}" if t.assignee else ""
                lines.append(f"  [{box}] {t.text}{who}")
        if self.report_text:
            lines.append(f"보고: {self.report_text}")
        return "\n".join(lines)
