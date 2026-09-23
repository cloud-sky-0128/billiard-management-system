from billiard_app import create_app

app = create_app()

if __name__ == "__main__":
    import os

    from billiard_app.maintenance import start_backup_scheduler

    backup_stop = start_backup_scheduler(
        app.config["DATABASE"], os.environ.get("BILLIARD_BACKUP_DIR", "")
    )
    try:
        app.run(use_reloader=False)
    finally:
        backup_stop.set()
