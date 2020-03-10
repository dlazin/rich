from abc import ABC, abstractmethod
from collections import deque
from contextlib import contextmanager
from dataclasses import dataclass, replace, field
from datetime import timedelta
from math import floor
import sys
from time import monotonic
from threading import Event, RLock, Thread
from typing import (
    Any,
    Callable,
    Deque,
    Dict,
    List,
    Optional,
    NamedTuple,
    Tuple,
    NewType,
    Union,
)

from .bar import Bar
from .console import Console, RenderableType
from . import filesize
from .live_render import LiveRender
from .table import Table
from .text import Text


TaskID = NewType("TaskID", int)


class ProgressColumn(ABC):
    """Base class for a widget to use in progress display."""

    max_refresh: Optional[float] = None

    def __init__(self) -> None:
        self._renderable_cache: Dict[TaskID, Tuple[float, RenderableType]] = {}
        self._update_time: Optional[float] = None

    def __call__(self, task: "Task") -> RenderableType:
        """Called by the Progress object to return a renderable for the given task.
        
        Args:
            task (Task): An object containing information regarding the task.
        
        Returns:
            RenderableType: Anything renderable (including str).
        """
        current_time = monotonic()
        if self.max_refresh is not None:
            try:
                timestamp, renderable = self._renderable_cache[task.id]
            except KeyError:
                pass
            else:
                if timestamp + self.max_refresh > current_time:
                    return renderable

        renderable = self.render(task)
        self._renderable_cache[task.id] = (current_time, renderable)
        return renderable

    @abstractmethod
    def render(self, task: "Task") -> RenderableType:
        """Should return a renderable object."""


class BarColumn(ProgressColumn):
    """Renders a progress bar."""

    def __init__(self, bar_width: Optional[int] = 40) -> None:
        self.bar_width = bar_width
        super().__init__()

    def render(self, task: "Task") -> Bar:
        """Gets a progress bar widget for a task."""
        return Bar(total=task.total, completed=task.completed, width=self.bar_width)


class TimeRemainingColumn(ProgressColumn):
    """Renders estimated time remaining."""

    # Only refresh twice a second to prevent jitter
    max_refresh = 0.5

    def render(self, task: "Task") -> Text:
        """Show time remaining."""
        remaining = task.time_remaining
        if remaining is None:
            return Text("?", style="progress.remaining")
        remaining_delta = timedelta(seconds=floor(remaining))
        return Text(str(remaining_delta), style="progress.remaining")


class FileSizeColumn(ProgressColumn):
    """Renders human readable filesize."""

    def render(self, task: "Task") -> Text:
        """Show data completed."""
        data_size = filesize.decimal(int(task.completed))
        return Text(data_size, style="progress.data")


class TransferSpeedColumn(ProgressColumn):
    """Renders human readable transfer speed."""

    def render(self, task: "Task") -> Text:
        """Show data transfer speed."""
        speed = task.speed
        if speed is None:
            return Text("?", style="progress.data.speed")
        data_speed = filesize.decimal(int(speed))
        return Text(f"{data_speed}/s", style="progress.data.speed")


class _ProgressSample(NamedTuple):
    """Sample of progress for a given time."""

    timestamp: float
    completed: float


@dataclass
class Task:
    """Stores information regarding a progress task."""

    id: TaskID
    name: str
    total: float
    completed: float
    visible: bool = True
    fields: Dict[str, Any] = field(default_factory=dict)
    start_time: Optional[float] = None
    stop_time: Optional[float] = None

    _progress: Deque[_ProgressSample] = field(default_factory=deque)

    @property
    def remaining(self) -> float:
        """Get the number of steps remaining."""
        return self.total - self.completed

    @property
    def elapsed(self) -> Optional[float]:
        """Time elapsed since task was started, or ``None`` if the task hasn't started."""
        if self.start_time is None:
            return None
        if self.stop_time is not None:
            return self.stop_time - self.start_time
        return monotonic() - self.start_time

    @property
    def finished(self) -> bool:
        """Check if the task has completed."""
        return self.completed >= self.total

    @property
    def percentage(self) -> float:
        """Get progress of task as a percantage."""
        if not self.total:
            return 0.0
        completed = (self.completed / self.total) * 100.0
        completed = min(100, max(0.0, completed))
        return completed

    @property
    def speed(self) -> Optional[float]:
        """Get the estimated speed in steps per second."""
        if self.start_time is None:
            return 0.0
        progress = list(self._progress)
        if not progress:
            return None
        total_time = progress[-1].timestamp - progress[0].timestamp
        if total_time == 0:
            return None
        total_completed = sum(sample.completed for sample in progress[1:])
        speed = total_completed / total_time
        return speed

    @property
    def time_remaining(self) -> Optional[float]:
        """Get estimated time to completion, or ``None`` if no data."""
        if self.finished:
            return 0.0
        speed = self.speed
        if speed is None:
            return None
        estimate = round(self.remaining / int(speed))
        return estimate


class RefreshThread(Thread):
    """A thread that calls refresh() on the Process object at regular intervals."""

    def __init__(self, progress: "Progress", refresh_per_second: int = 10) -> None:
        self.progress = progress
        self.refresh_per_second = refresh_per_second
        self.done = Event()
        super().__init__()

    def stop(self) -> None:
        self.done.set()
        self.join()

    def run(self) -> None:
        while not self.done.wait(1.0 / self.refresh_per_second):
            self.progress.refresh()


class Progress:
    """Renders an auto-updating progress bar(s).
    
    Args:
        console (Console, optional): Optional Console instance. Default will create own internal Console instance.
        auto_refresh (bool, optional): Enable auto refresh. If disabled, you will need to call `refresh()`.
        refresh_per_second (int, optional): Number of times per second to refresh the progress information. Defaults to 15.
        speed_estimate_period: (float, optional): Period (in seconds) used to calculate the speed estimate. Defaults to 30.
    """

    def __init__(
        self,
        *columns: Union[str, ProgressColumn],
        console: Console = None,
        auto_refresh: bool = True,
        refresh_per_second: int = 15,
        speed_estimate_period: float = 30.0,
    ) -> None:
        self.columns = columns or (
            "{task.name}",
            BarColumn(),
            "[progress.percentage]{task.percentage:>3.0f}%",
            TimeRemainingColumn(),
        )
        self.console = console or Console(file=sys.stderr)
        self.auto_refresh = auto_refresh
        self.refresh_per_second = refresh_per_second
        self.speed_estimate_period = speed_estimate_period
        self._tasks: Dict[TaskID, Task] = {}
        self._live_render = LiveRender(self._table)
        self._task_index: TaskID = TaskID(0)
        self._lock = RLock()
        self._refresh_thread: Optional[RefreshThread] = None
        self._refresh_count = 0

    @property
    def tasks_ids(self) -> List[TaskID]:
        """Get a list of task IDs."""
        with self._lock:
            return list(self._tasks.keys())

    @property
    def finished(self) -> bool:
        """Check if all tasks have been completed."""
        with self._lock:
            if not self._tasks:
                return True
            return all(task.finished for task in self._tasks.values())

    def __enter__(self) -> "Progress":
        self.console.show_cursor(False)
        if self.auto_refresh:
            self._refresh_thread = RefreshThread(self, self.refresh_per_second)
            self._refresh_thread.start()
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        try:
            if self.auto_refresh:
                self._refresh_thread.stop()
                self._refresh_thread = None
            self.refresh()
            self.console.line()
        finally:
            self.console.show_cursor(True)

    def start(self, task_id: TaskID) -> None:
        """Start a task.

        Starts a task (used when calculating elapsed time). You may need to call this manually,
        if you called `add_task` with ``start=False``.
        
        Args:
            task_id (TaskID): ID of task.
        """
        with self._lock:
            task = self._tasks[task_id]
            task.start_time = monotonic()

    def stop(self, task_id: TaskID) -> None:
        """Stop a task.

        This will freeze the elapsed time on the task.
        
        Args:
            task_id (TaskID): ID of task.
        """
        task = self._tasks[task_id]
        with self._lock:
            current_time = monotonic()
            if task.start_time is None:
                task.start_time = current_time
            task.stop_time = current_time

    def update(
        self,
        task_id: TaskID,
        *,
        total: float = None,
        completed: float = None,
        advance: float = None,
        visible: bool = None,
        **fields: Any,
    ) -> None:
        """Update information associated with a task.
        
        Args:
            task_id (TaskID): Task id (return by add_task).
            total (float, optional): Updates task.total if not None.
            completed (float, optional): Updates task.completed if not None.
            advance (float, optional): Add a value to task.completed if not None.
            visible (bool, optional): Set visible flag if not None.
        """
        current_time = monotonic()
        with self._lock:
            task = self._tasks[task_id]
            completed_start = task.completed

            if total is not None:
                task.total = total
            if advance is not None:
                task.completed += advance
            if completed is not None:
                task.completed = completed
            if visible is not None:
                task.visible = True
            task.fields.update(fields)

            update_completed = task.completed - completed_start
            old_sample_time = current_time - self.speed_estimate_period
            _progress = task._progress

            while _progress and _progress[0].timestamp < old_sample_time:
                _progress.popleft()
            task._progress.append(_ProgressSample(current_time, update_completed))

    def refresh(self) -> None:
        """Refresh (render) the progress information."""
        with self._lock:
            self._live_render.set_renderable(self._table)
            self.console.print(self._live_render)
            self._refresh_count += 1

    @property
    def _table(self) -> Table:
        """Get a table to render the Progress display."""
        table = Table.grid()
        table.padding = (0, 1, 0, 0)
        for column in self.columns:
            table.add_column()
        for _, task in self._tasks.items():
            if task.visible:
                row: List[RenderableType] = []
                for index, column in enumerate(self.columns):
                    if isinstance(column, str):
                        row.append(column.format(task=task))
                        table.columns[index].no_wrap = True
                    else:
                        widget = column(task)
                        row.append(widget)
                        if isinstance(widget, (str, Text)):
                            table.columns[index].no_wrap = True
                table.add_row(*row)
        return table

    def add_task(
        self,
        name: str,
        start: bool = True,
        total: int = 100,
        completed: int = 0,
        visible: bool = True,
        **fields: str,
    ) -> TaskID:
        """Add a new 'task' to the Progress display.
        
        Args:
            name (str): The name of the task.
            start (bool, optional): Start the task immediately (to calculate elapsed time). If set to False,
                you will need to call `start` manually. Defaults to True.
            total (int, optional): Number of total steps in the progress if know. Defaults to 100.
            completed (int, optional): Number of steps completed so far.. Defaults to 0.
            visible (bool, optional): Enable display of the task. Defaults to True.
        
        Returns:
            TaskID: An ID you can use when calling `update`.
        """
        with self._lock:
            task = Task(
                self._task_index, name, total, completed, visible=visible, fields=fields
            )
            self._tasks[self._task_index] = task
            if start:
                self.start(self._task_index)
            self.refresh()
            try:
                return self._task_index
            finally:
                self._task_index = TaskID(int(self._task_index) + 1)

    def remove_task(self, task_id: TaskID) -> None:
        """Delete a task if it exists.
        
        Args:
            task_id (TaskID): A task ID.
        
        """
        with self._lock:
            del self._tasks[task_id]


if __name__ == "__main__":

    import time

    with Progress() as progress:

        task1 = progress.add_task("[red]Downloading", total=1000)
        task2 = progress.add_task("[green]Processing", total=1000)
        task3 = progress.add_task("[cyan]Cooking", total=1000)

        while not progress.finished:
            progress.update(task1, advance=0.5)
            progress.update(task2, advance=0.3)
            progress.update(task3, advance=0.9)
            time.sleep(0.02)