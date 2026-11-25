"""One owner for demo identity, freshness and generation across all modules."""

import threading
import time

from datalog.core.validation import Invalid


class DemoService:
    def __init__(self, store):
        self.store = store
        self.lock = threading.Lock()

    def ensure(self, project):
        if project != 'demo':
            raise Invalid("Demo data is only available in the demo project")
        with self.lock, self.store.connect() as db:
            # Nonblocking across processes: a waiter must not exhaust the pool
            # while the seed workflow acquires its own transaction connections.
            locked = db.execute(
                "SELECT pg_try_advisory_xact_lock(hashtextextended(current_schema() || ':demo-refresh',0)) locked"
            ).fetchone()['locked']
            if not locked:
                raise Invalid("Demo is being prepared. Try again in a moment.")
            row = db.execute("SELECT * FROM demo_refresh WHERE project='demo'").fetchone()
            if row and row['refreshed_at'] >= time.time() - 600:
                return {'already_loaded': True, 'refreshed_at': row['refreshed_at'], 'seed_id': row['seed_id']}
            from datalog.seed_demo import seed

            return seed(self.store)
