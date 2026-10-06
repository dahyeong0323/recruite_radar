from __future__ import annotations

try:
    from apscheduler.schedulers.asyncio import AsyncIOScheduler
    from apscheduler.triggers.cron import CronTrigger
    from apscheduler.triggers.interval import IntervalTrigger
except ModuleNotFoundError:  # local fixture tests can run before deployment dependencies are installed
    class _Job:
        def __init__(self, job_id: str, trigger=None, **kwargs) -> None:
            self.id = job_id
            self.trigger = trigger
            self.__dict__.update(kwargs)

    class _Trigger:
        def __init__(self, **kwargs) -> None:
            self.kwargs = kwargs
            self.__dict__.update(kwargs)

    class IntervalTrigger(_Trigger):
        pass

    class CronTrigger(_Trigger):
        pass

    class AsyncIOScheduler:
        def __init__(self, timezone: str = "UTC") -> None:
            self.timezone = timezone
            self._jobs: list[_Job] = []
            self.running = False

        def add_job(self, func, trigger, *, id: str, replace_existing: bool = False, **kwargs) -> None:
            self._jobs = [job for job in self._jobs if job.id != id]
            self._jobs.append(_Job(id, trigger, **kwargs))

        def get_jobs(self) -> list[_Job]:
            return list(self._jobs)

        def start(self) -> None:
            self.running = True

        def shutdown(self, wait: bool = False) -> None:
            self.running = False


def configure_scheduler(service, timezone: str = "Asia/Seoul", event_service=None) -> AsyncIOScheduler:
    scheduler = AsyncIOScheduler(timezone=timezone)
    scheduler.add_job(service.collect_all, IntervalTrigger(hours=2, timezone=timezone), id="collect-all", replace_existing=True)
    scheduler.add_job(service.refresh_active, CronTrigger(hour=3, minute=0, timezone=timezone), id="refresh-active", replace_existing=True)
    scheduler.add_job(service.send_digest, CronTrigger(hour=20, minute=30, timezone=timezone), id="daily-digest", replace_existing=True)
    scheduler.add_job(service.send_deadline_reminders, CronTrigger(hour=9, minute=0, timezone=timezone), id="deadline-reminders", replace_existing=True)
    if event_service is not None:
        options = dict(replace_existing=True, max_instances=1, coalesce=True, misfire_grace_time=3600)
        scheduler.add_job(event_service.collect_due, IntervalTrigger(hours=1, timezone="Europe/Zurich"), id="event-discovery", **options)
        scheduler.add_job(event_service.refresh_events, IntervalTrigger(hours=6, timezone="Europe/Zurich"), id="event-refresh", **options)
        scheduler.add_job(event_service.discover_search, CronTrigger(hour="8,14,20" if event_service.settings.event_coverage_enabled else 8, minute=15, timezone="Europe/Zurich"), id="event-search", **options)
        scheduler.add_job(event_service.advance_lifecycle, IntervalTrigger(hours=1, timezone="Europe/Zurich"), id="event-lifecycle", **options)
        scheduler.add_job(event_service.dispatch_outbox, IntervalTrigger(minutes=5, timezone="Europe/Zurich"), id="event-outbox", **options)
        scheduler.add_job(event_service.send_digest, CronTrigger(hour=18, minute=30, timezone="Europe/Zurich"), id="event-digest", **options)
        scheduler.add_job(event_service.send_reminders, CronTrigger(hour=9, minute=15, timezone="Europe/Zurich"), id="event-reminders", **options)
    return scheduler
