"""Task Context/Archive 파일 Guide.

Project/Task의 작업 맥락(Context)과 완료 기록(Archive)을 파일시스템에 저장·참조한다.
- Context: 진행 중 작업 맥락(누적 메모). `tasks/<id>/context.md`
- Archive: 완료된 Task의 최종 기록. `archive/<id>.md`
"""
from pathlib import Path
from typing import List, Optional


class TaskStore:
    """Task 맥락/기록 파일 저장소(파일시스템 Guide)."""

    def __init__(self, base_dir):
        self.base = Path(base_dir)

    def _context_path(self, task_id) -> Path:
        return self.base / "tasks" / str(task_id) / "context.md"

    def _archive_path(self, task_id) -> Path:
        return self.base / "archive" / f"{task_id}.md"

    # --- Context (진행 중 맥락) ---

    def save_context(self, task_id, text: str) -> Path:
        p = self._context_path(task_id)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text, encoding="utf-8")
        return p

    def load_context(self, task_id) -> Optional[str]:
        p = self._context_path(task_id)
        return p.read_text(encoding="utf-8") if p.exists() else None

    def append_context(self, task_id, line: str) -> Path:
        prev = self.load_context(task_id) or ""
        return self.save_context(task_id, f"{prev}{line}\n")

    # --- Archive (완료 기록) ---

    def archive(self, task_id, content: str) -> Path:
        p = self._archive_path(task_id)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(content, encoding="utf-8")
        return p

    def load_archive(self, task_id) -> Optional[str]:
        p = self._archive_path(task_id)
        return p.read_text(encoding="utf-8") if p.exists() else None

    def list_archived(self) -> List[str]:
        d = self.base / "archive"
        return sorted(p.stem for p in d.glob("*.md")) if d.exists() else []
