from __future__ import annotations

try:
    from apscheduler.schedulers.asyncio import AsyncIOScheduler
    from apscheduler.triggers.cron import CronTrigger
    from apscheduler.triggers.interval import IntervalTrigger
except ModuleNotFoundError:  # local fixture tests can run before deployment dependencies are installed
    class _Job:
        def __init__(self, job_id: str) -> None:
            self.id = job_id

    class _Trigger:
        def __init__(self, **kwargs) -> None:
            self.kwargs = kwargs

    class IntervalTrigger(_Trigger):
        pass

    class CronTrigger(_Trigger):
        pass

    class AsyncIOScheduler:
        def __init__(self, timezone: str = "UTC") -> None:
            self.timezone = timezone
            self._jobs: list[_Job] = []
            self.running = False

        def add_job(self, func, trigger, *, id: str, replace_existing: bool = False) -> None:
            self._jobs = [job for job in self._jobs if job.id != id]
            self._jobs.append(_Job(id))

        def get_jobs(self) -> list[_Job]:
            return list(self._jobs)

        def start(self) -> None:
            self.running = True

        def shutdown(self, wait: bool = False) -> None:
            self.running = False


def configure_scheduler(service, timezone: str = "Asia/Seoul") -> AsyncIOScheduler:
    scheduler = AsyncIOScheduler(timezone=timezone)
    scheduler.add_job(service.collect_all, IntervalTrigger(hours=2, timezone=timezone), id="collect-all", replace_existing=True)
    scheduler.add_job(service.refresh_active, CronTrigger(hour=3, minute=0, timezone=timezone), id="refresh-active", replace_existing=True)
    scheduler.add_job(service.send_digest, CronTrigger(hour=20, minute=30, timezone=timezone), id="daily-digest", replace_existing=True)
    scheduler.add_job(service.send_deadline_reminders, CronTrigger(hour=9, minute=0, timezone=timezone), id="deadline-reminders", replace_existing=True)
    return scheduler
