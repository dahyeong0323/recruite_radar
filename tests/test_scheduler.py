from app.scheduler import configure_scheduler


class Service:
    async def collect_all(self): pass
    async def refresh_active(self): pass
    async def send_digest(self): pass
    async def send_deadline_reminders(self): pass


def test_required_jobs_are_scheduled():
    scheduler = configure_scheduler(Service())
    assert {job.id for job in scheduler.get_jobs()} == {"collect-all", "refresh-active", "daily-digest", "deadline-reminders"}
    scheduler.shutdown(wait=False)
