"""
Gunicorn hooks. Read automatically from the working directory.

post_fork runs inside each worker after it is forked from the master, which
is the only place the background threads may start: a thread started in the
master is invisible to the worker that serves the pages (see app.start_once).
render.yaml configures ONE worker; this hook would start one set of threads
per worker, which is why that must stay one.
"""


def post_fork(server, worker):
    import app
    app.start_once()
