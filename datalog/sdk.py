"""Small synchronous instrumentation client for Datalog's own HTTP API."""

from contextlib import contextmanager
from contextvars import ContextVar
import base64
import hashlib
import json
import os
import time
import uuid
import warnings
from urllib.parse import urlencode
from urllib.request import Request, urlopen


class DatalogClient:
    def __init__(
        self,
        base_url='http://127.0.0.1:4310',
        project='local',
        service='ai-app',
        env='dev',
        fail_open=True,
        api_key=None,
    ):
        self.base_url, self.project = base_url.rstrip('/'), project
        self.service, self.env, self.fail_open = service, env, fail_open
        self.api_key = api_key if api_key is not None else os.environ.get("DATALOG_INGESTION_KEY")

    def export(self, trace):
        try:
            headers = {"Content-Type": "application/json"}
            if self.api_key:
                headers["Authorization"] = "Bearer " + self.api_key
            req = Request(
                self.base_url + '/api/v1/ai/traces?' + urlencode({'project': self.project}),
                data=json.dumps(trace, allow_nan=False).encode(),
                headers=headers,
            )
            with urlopen(req, timeout=10) as response:
                return json.load(response)
        except Exception:
            if not self.fail_open:
                raise
            warnings.warn(
                "Datalog: trace export failed; the application run was preserved.", RuntimeWarning, stacklevel=2
            )
            return None

    def attach_media(self, trace_id, span_id, data, content_type):
        """Attach PNG or WAV bytes after the corresponding OTLP span is accepted."""
        headers = {"Content-Type": "application/json"}
        if self.api_key:
            headers["Authorization"] = "Bearer " + self.api_key
        payload = {
            "trace_id": trace_id,
            "span_id": span_id,
            "content_type": content_type,
            "sha256": hashlib.sha256(data).hexdigest(),
            "data": base64.b64encode(data).decode("ascii"),
        }
        req = Request(
            self.base_url + '/api/v1/trace/media?' + urlencode({'project': self.project}),
            data=json.dumps(payload).encode(),
            headers=headers,
        )
        with urlopen(req, timeout=10) as response:
            return json.load(response)

    @contextmanager
    def trace(self, name, agent=None, session_id=None, input=None, metadata=None):
        now, root_id = time.time(), uuid.uuid4().hex
        trace = {
            'id': uuid.uuid4().hex,
            'name': name,
            'service': self.service,
            'env': self.env,
            'start_time': now,
            'status': 'running',
            'input': input,
            'metadata': metadata or {},
            'spans': [],
        }
        if agent is not None:
            trace['agent'] = agent
        if session_id is not None:
            trace['session_id'] = session_id
        root = {
            'id': root_id,
            'name': agent or name,
            'kind': 'agent' if agent else 'workflow',
            'start_time': now,
            'status': 'running',
            'input': input,
        }
        trace['spans'].append(root)
        run = Run(trace, root_id)
        application_error = None
        try:
            yield run
        except BaseException as error:
            application_error = error
            trace['status'] = root['status'] = 'error'
            root['output'] = {'error_type': type(error).__name__, 'message': str(error)}
            raise
        else:
            trace['status'] = root['status'] = 'ok'
        finally:
            trace['end_time'] = root['end_time'] = time.time()
            if 'output' in trace and application_error is None:
                root['output'] = trace['output']
            try:
                self.export(trace)
            except Exception:
                if application_error is None:
                    raise
                warnings.warn(
                    "Datalog: export failed; the application's original exception was preserved.",
                    RuntimeWarning,
                    stacklevel=2,
                )


class Run:
    def __init__(self, trace, root_id):
        self.data = trace
        self.parent = ContextVar('datalog_parent_' + root_id, default=root_id)

    @property
    def trace_id(self):
        return self.data['id']

    def output(self, value):
        self.data['output'] = value

    @contextmanager
    def span(self, name, kind, **attributes):
        span = {
            'id': uuid.uuid4().hex,
            'parent_id': self.parent.get(),
            'name': name,
            'kind': kind,
            **attributes,
            'start_time': time.time(),
            'status': 'running',
        }
        self.data['spans'].append(span)
        token = self.parent.set(span['id'])
        try:
            yield span
        except BaseException as error:
            span['status'] = 'error'
            span['output'] = {'error_type': type(error).__name__, 'message': str(error)}
            raise
        else:
            span['status'] = 'ok'
        finally:
            span['end_time'] = time.time()
            self.parent.reset(token)
