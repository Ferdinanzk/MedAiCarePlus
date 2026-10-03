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
    from app.jobs.taken_confirmation_job import check_taken_confirmations
    from app.jobs.reachy_task_job import run_reachy_task_maintenance
    from app.jobs.dose_confirmation_job import run_dose_confirmation_maintenance
    from app.jobs.conversation_retention_job import purge_old_transcripts
    from app.jobs.after_chat_job import run_after_chat_sweep

    scheduler.add_job(
        check_missed_doses,
        IntervalTrigger(minutes=1),
        id="missed_doses",
        replace_existing=True,
    )
    scheduler.add_job(
        check_taken_confirmations,
        IntervalTrigger(minutes=1),
        id="taken_confirmations",
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
    scheduler.add_job(
        run_reachy_task_maintenance,
        IntervalTrigger(minutes=1),
        id="reachy_tasks",
        replace_existing=True,
    )
    scheduler.add_job(
        run_dose_confirmation_maintenance,
        IntervalTrigger(minutes=1),
        id="dose_confirmations",
        replace_existing=True,
    )
    scheduler.add_job(
        purge_old_transcripts,
        CronTrigger(hour=3, minute=30),
        id="conversation_retention",
        replace_existing=True,
    )
    scheduler.add_job(
        run_after_chat_sweep,
        IntervalTrigger(minutes=10),
        id="after_chat_sweep",
        replace_existing=True,
    )
    scheduler.start()
    print(f"[Scheduler] Started at {datetime.now(TZ_TAIPEI)}")


def stop_scheduler():
    scheduler.shutdown()
