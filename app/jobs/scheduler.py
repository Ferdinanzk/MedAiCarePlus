from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.interval import IntervalTrigger
from datetime import datetime
import pytz

TZ_TAIPEI = pytz.timezone("Asia/Taipei")

scheduler = AsyncIOScheduler(timezone=TZ_TAIPEI)


def start_scheduler():
    from app.jobs.missed_dose_job import check_missed_doses
    from app.jobs.weekly_summary_job import send_weekly_summaries
    from app.jobs.refill_reminder_job import check_refill_reminders
    from app.jobs.emotion_alert_job import check_negative_emotions

    scheduler.add_job(
        check_missed_doses,
        IntervalTrigger(minutes=15),
        id="missed_doses",
        replace_existing=True,
    )
    scheduler.add_job(
        send_weekly_summaries,
        CronTrigger(day_of_week="sun", hour=9, minute=0),
        id="weekly_summary",
        replace_existing=True,
    )
    scheduler.add_job(
        check_refill_reminders,
        CronTrigger(hour=8, minute=0),
        id="refill_reminder",
        replace_existing=True,
    )
    scheduler.add_job(
        check_negative_emotions,
        IntervalTrigger(minutes=30),
        id="emotion_alerts",
        replace_existing=True,
    )
    scheduler.start()
    print(f"[Scheduler] Started at {datetime.now(TZ_TAIPEI)}")


def stop_scheduler():
    scheduler.shutdown()
