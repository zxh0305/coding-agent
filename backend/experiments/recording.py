"""Append-only evidence and transport-level request observation."""
import datetime
import hashlib
import json
import threading
import uuid


class EvidenceError(RuntimeError):
    pass


def sha256(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


class Recorder:
    def __init__(self, root, run_id):
        self.root, self.run_id = root, run_id
        self.incomplete = False
        self.lock = threading.Lock()
        self.requests = []
        for name in ('events.jsonl', 'requests.jsonl'):
            (root / name).touch(exist_ok=False)

    def append(self, filename, event):
        row = dict(event, run_id=self.run_id,
                   timestamp=datetime.datetime.now(datetime.timezone.utc).isoformat())
        try:
            with self.lock, (self.root / filename).open('a', encoding='utf-8') as f:
                f.write(json.dumps(row, ensure_ascii=False, allow_nan=False) + '\n')
                f.flush()
        except Exception as e:
            self.incomplete = True
            raise EvidenceError('实验记录写入失败') from e
        return row

    def event(self, kind, context=None, **fields):
        return self.append('events.jsonl', dict(context or {}, event_type=kind, **fields))

    def save(self, filename, value):
        try:
            target = self.root / filename
            tmp = target.with_suffix(target.suffix + '.tmp')
            tmp.write_text(json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False), encoding='utf-8')
            tmp.replace(target)
        except Exception as e:
            self.incomplete = True
            raise EvidenceError('实验结果写入失败') from e


class RequestObserver:
    """One identity per actual HTTP attempt, including fallback/retry requests.

    Auth headers, URLs, keys and error bodies are deliberately never exported.
    Raw provider usage is authoritative; absent fields remain absent/unknown.
    """
    def __init__(self, recorder, context):
        self.recorder, self.context = recorder, context
        self.current = None

    def __call__(self, kind, info):
        if self.recorder.incomplete:
            raise EvidenceError('证据已不完整，禁止继续模型请求')
        if kind == 'start':
            self.current = dict(self.context, request_id=uuid.uuid4().hex,
                                requested_model=info['payload'].get('model'),
                                transport_attempt=info['attempt'],
                                returned_model=None, provider_request_id=None,
                                raw_usage=[], usage_status='unknown', status='incomplete')
            self.recorder.append('requests.jsonl', dict(self.current, event_type='request_start',
                                                       payload=info['payload']))
            self.recorder.requests.append(self.current)
        elif kind == 'error':
            self.finish('error', **info)
        elif kind == 'response':
            response = info['response']
            headers = getattr(response, 'headers', {})
            self.current['provider_request_id'] = headers.get('x-request-id') or headers.get('request-id')
            self.recorder.append('requests.jsonl', dict(self.current, event_type='response_headers'))
            return ObservedResponse(response, self)

    def data(self, value):
        if not isinstance(value, dict):
            return
        msg = value.get('message') or {}
        model = value.get('model') or msg.get('model')
        if model:
            self.current['returned_model'] = model
        usage = value.get('usage') or msg.get('usage')
        if usage:
            self.current['raw_usage'].append(usage)
            self.current['usage_status'] = 'reported'
        self.recorder.append('requests.jsonl', dict(self.context,
            request_id=self.current['request_id'], event_type='response_data', data=value))

    def finish(self, status, **fields):
        self.current.update(event_type='request_end', status=status, **fields)
        self.recorder.append('requests.jsonl', self.current)


class ObservedResponse:
    def __init__(self, response, observer):
        self.response, self.observer = response, observer
        self.ended = False
        self.terminal = False

    def __getattr__(self, key):
        return getattr(self.response, key)

    def __enter__(self):
        return self

    def __exit__(self, kind, error, tb):
        try:
            if not self.ended:
                self.ended = True
                self.observer.finish('error' if kind else ('completed' if self.terminal else 'interrupted'),
                                     error_type=kind.__name__ if kind else None)
        finally:
            self.response.close()

    def __iter__(self):
        for raw in self.response:
            line = raw.decode('utf-8').strip()
            if line.startswith('data:'):
                data = line[5:].strip()
                if data == '[DONE]':
                    self.terminal = True
                else:
                    try:
                        value = json.loads(data)
                    except ValueError:
                        value = None
                    if value is not None:
                        self.observer.data(value)
                        if value.get('type') == 'message_stop':
                            self.terminal = True
            yield raw

    def read(self, *args):
        raw = self.response.read(*args)
        try:
            self.observer.data(json.loads(raw))
            self.terminal = True
        except (ValueError, UnicodeDecodeError):
            pass
        return raw


def usage_summary(requests):
    """Sum reported counters only, retaining unknown request/field counts."""
    groups = {}
    for request in requests:
        key = (request['agent_id'], request['provider'], request['model'])
        group = groups.setdefault(key, dict(agent_id=key[0], provider=key[1], model=key[2],
                                            requests=0, unknown_usage_requests=0, returned_models=[], tokens={}))
        group['requests'] += 1
        returned = request.get('returned_model')
        if returned and returned not in group['returned_models']:
            group['returned_models'].append(returned)
        if request['usage_status'] == 'unknown':
            group['unknown_usage_requests'] += 1
        # Anthropic start and delta use different counters; later values replace
        # earlier counters within ONE request, never sum repeated snapshots.
        merged = {}
        for usage in request['raw_usage']:
            merged.update(usage)
        for field in ('prompt_tokens', 'completion_tokens', 'total_tokens', 'input_tokens',
                      'output_tokens', 'prompt_cache_hit_tokens', 'prompt_cache_miss_tokens',
                      'cache_read_input_tokens', 'cache_creation_input_tokens'):
            value = merged.get(field)
            if isinstance(value, (int, float)) and not isinstance(value, bool):
                group['tokens'][field] = group['tokens'].get(field, 0) + value
        cached = (merged.get('prompt_tokens_details') or {}).get('cached_tokens')
        if isinstance(cached, (int, float)):
            group['tokens']['cached_tokens'] = group['tokens'].get('cached_tokens', 0) + cached
    return list(groups.values())
