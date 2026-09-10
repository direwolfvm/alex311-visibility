"""Start the submission job from the web service.

The browser cannot live in the public web image, so filing happens in a Cloud
Run job. Until now a person started that job by hand, which was fine while a
person also had to release each request. Now that a tester's own press is the
last human step, something has to start the job within seconds of it, or
"real-time" means "whenever someone remembers".

Cloud Run's own API does that, authenticated with the token the metadata server
hands the service. Failure here is never fatal: a request that was released but
whose job did not start is still released, and the next kick or the safety-net
schedule picks it up. That is why every call returns a string rather than
raising.
"""
from __future__ import annotations

import json
import logging
import os
import urllib.error
import urllib.request

log = logging.getLogger("alex311.job_runner")

JOB_ENV = "ALEX311_SUBMIT_JOB"        # e.g. alex311-submit; unset = do not kick
REGION_ENV = "ALEX311_REGION"
PROJECT_ENV = "ALEX311_PROJECT"

METADATA = "http://metadata.google.internal/computeMetadata/v1"
TOKEN_URL = f"{METADATA}/instance/service-accounts/default/token"
PROJECT_URL = f"{METADATA}/project/project-id"
TIMEOUT = 8


def _metadata(url: str) -> str | None:
    req = urllib.request.Request(url, headers={"Metadata-Flavor": "Google"})
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT) as r:
            return r.read().decode()
    except Exception as e:                       # not on Cloud Run, or no network
        log.debug("metadata %s unavailable: %s", url, e)
        return None


def configured() -> bool:
    """Whether this process can start the job at all.

    False in development and in tests, where the environment variable is
    absent. Callers use it to say "queued" instead of "filing" rather than to
    decide whether the request was accepted.
    """
    return bool(os.environ.get(JOB_ENV))


def kick(job: str | None = None) -> str:
    """Ask Cloud Run to run the submission job once. Returns what happened.

    The string is for the log and for the response the tester sees; nothing
    branches on it.
    """
    job = job or os.environ.get(JOB_ENV)
    if not job:
        return "no job configured"

    project = os.environ.get(PROJECT_ENV) or _metadata(PROJECT_URL)
    region = os.environ.get(REGION_ENV, "us-east4")
    if not project:
        return "no project id"

    token = _metadata(TOKEN_URL)
    if not token:
        return "no credentials"
    try:
        access_token = json.loads(token)["access_token"]
    except Exception:
        return "unreadable credentials"

    url = (f"https://run.googleapis.com/v2/projects/{project}/locations/{region}"
           f"/jobs/{job}:run")
    req = urllib.request.Request(url, data=b"{}", method="POST", headers={
        "Authorization": f"Bearer {access_token}",
        "Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT) as r:
            name = json.loads(r.read().decode()).get("name", "")
        log.info("started %s: %s", job, name)
        return f"started {name.rsplit('/', 1)[-1] or job}"
    except urllib.error.HTTPError as e:
        body = e.read().decode()[:300]
        log.error("could not start %s: %s %s", job, e.code, body)
        return f"could not start the job ({e.code})"
    except Exception as e:
        log.error("could not start %s: %s", job, e)
        return "could not start the job"
