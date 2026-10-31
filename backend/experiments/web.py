"""File-backed authenticated experiment management. Never rewrites historical runs."""
import argparse
import datetime
import json
import re
import threading
import uuid
from pathlib import Path

from config import PROJECT_DIR
from experiments.agents import resolve_client
from experiments.cases import order_report as case
from experiments.recording import usage_summary, sha256
from experiments.runner import run, validate_plan


class WebError(Exception):
    def __init__(self, message, status=400):
        super().__init__(message)
        self.status = status


def now():
    return datetime.datetime.now(datetime.timezone.utc).isoformat()


def safe(value):
    """Do not expose auth/connection fields, URLs or common credential tokens."""
    if isinstance(value, dict):
        return {k: ('[已隐藏]' if re.search(r'key|authorization|password|secret|token$|base_url|endpoint|headers', k, re.I)
                    and not k.endswith('tokens') else safe(v)) for k, v in value.items()}
    if isinstance(value, list):
        return [safe(v) for v in value]
    if isinstance(value, str):
        if value.lstrip().startswith(('{', '[')):
            try:
                parsed = json.loads(value)
                if isinstance(parsed, (dict, list)):
                    return json.dumps(safe(parsed), ensure_ascii=False)
            except ValueError:
                pass
        value = re.sub(r'(?:https?|postgresql|mysql|redis|mongodb)://[^\s"<>]+', '[连接地址已隐藏]', value)
        return re.sub(r'(?i)((?:Bearer|Basic)\s+\S+|sk-[\w-]{8,}|(?:api_key|password|access_token)\s*[:=]\s*[^\s,;]+)', '[凭据已隐藏]', value)
    return value


def read_json(path, default=None):
    if not path.exists():
        return default
    if path.is_symlink() or path.stat().st_size > 4 * 1024 * 1024:
        raise WebError('记录损坏或超出大小限制', 422)
    try:
        value = json.loads(path.read_text(encoding='utf-8'))
        if not isinstance(value, dict):
            raise ValueError()
        return value
    except (ValueError, OSError):
        raise WebError('记录损坏，无法读取', 422)


def page(path, cursor=0, limit=50):
    """Byte cursor, bounded reads, leave an actively appended partial line untouched."""
    if type(cursor) is not int or cursor < 0 or type(limit) is not int or not 1 <= limit <= 100:
        raise WebError('游标或数量非法')
    if not path.exists():
        if cursor:
            raise WebError('日志游标已失效', 409)
        return dict(items=[], cursor=0, more=False)
    if path.is_symlink():
        raise WebError('拒绝符号链接日志', 422)
    size = path.stat().st_size
    if cursor > size:
        raise WebError('日志游标已失效', 409)
    items = []
    budget = 0
    with path.open('rb') as f:
        if cursor:
            f.seek(cursor - 1)
            if f.read(1) != b'\n':
                raise WebError('游标必须指向记录边界')
        f.seek(cursor)
        while len(items) < limit and budget < 2 * 1024 * 1024:
            start = f.tell()
            raw = f.readline(2 * 1024 * 1024 + 1)
            if not raw:
                break
            if len(raw) > 2 * 1024 * 1024:
                raise WebError('日志记录过大，无法展示', 422)
            if not raw.endswith(b'\n'):
                if start + len(raw) < size:
                    raise WebError('日志记录损坏', 422)
                break
            try:
                row = json.loads(raw)
                if not isinstance(row, dict):
                    raise ValueError()
            except (ValueError, UnicodeDecodeError):
                raise WebError('日志记录损坏', 422)
            cursor = f.tell()
            budget += len(raw)
            items.append(dict(offset=start, next_cursor=cursor, record=row))
    return dict(items=items, cursor=cursor, more=cursor < size)


class Experiments:
    def __init__(self, root=None, metadata=None, executor=run, model_check=None):
        self.root = Path(root or PROJECT_DIR / 'runs').resolve()
        self.metadata = Path(metadata or PROJECT_DIR / 'data' / 'experiment-web').resolve()
        self.executor = executor
        self.model_check = model_check or (lambda member: resolve_client(member, None))
        self.instance = uuid.uuid4().hex
        self.lock = threading.RLock()
        self.active = None
        self.cache = {}

    def paths(self, rid):
        if not re.fullmatch(r'[a-f0-9]{32}', rid):
            raise WebError('实验不存在或无权访问', 404)
        root = self.root / rid
        if root.is_symlink() or not root.resolve().is_relative_to(self.root):
            raise WebError('实验目录非法', 422)
        return root, self.metadata / (rid + '.json')

    def meta(self, rid, user):
        root, path = self.paths(rid)
        meta = read_json(path, {})
        if meta.get('owner_id') != user:
            raise WebError('实验不存在或无权访问', 404)
        return root, meta

    def save_meta(self, rid, meta):
        self.metadata.mkdir(parents=True, exist_ok=True)
        path = self.paths(rid)[1]
        temp = path.with_suffix('.tmp')
        temp.write_text(json.dumps(meta), encoding='utf-8')
        temp.replace(path)

    def assign(self, rid, user):
        """Explicit local operator action, never a web claim-by-ID endpoint."""
        with self.lock:
            root, path = self.paths(rid)
            if path.exists():
                raise WebError('实验已有关联，禁止覆盖')
            manifest = read_json(root / 'manifest.json')
            if not manifest:
                raise WebError('历史实验不存在', 404)
            self.save_meta(rid, dict(owner_id=user, status='historical',
                                    created_at=datetime.datetime.fromtimestamp(
                                        (root / 'manifest.json').stat().st_mtime,
                                        datetime.timezone.utc).isoformat(), assigned_at=now()))

    def config(self, body):
        if not isinstance(body, dict) or set(body) != {'case', 'agents'} or body['case'] != 'order_report':
            raise WebError('仅接受order_report场景与成员配置')
        agents = body['agents']
        if not isinstance(agents, list) or len(agents) != 2:
            raise WebError('必须配置两名成员')
        fields = {'id', 'provider_id', 'model', 'max_rounds', 'timeout'}
        for member in agents:
            if not isinstance(member, dict) or set(member) != fields:
                raise WebError('成员配置字段非法')
            if member['id'] not in ('data_worker', 'report_worker'):
                raise WebError('成员非法')
        config = {'agents': [dict(m, prompt_file=f"prompts/{m['id']}.md",
                                 allowed_tools=['read_file', 'write_file', 'list_dir']) for m in agents]}
        try:
            validate_plan(case.TASKS, config['agents'])
            for member in config['agents']:
                self.model_check(member)  # resolves only, no HTTP calls
        except Exception:
            raise WebError('成员配置或指定模型不可用，请检查启用状态及后端配置')
        return config

    def create(self, body, user):
        config = self.config(body)
        with self.lock:
            if self.active is not None:
                raise WebError('已有网页实验正在执行，请等待或停止后再启动', 409)
            rid = uuid.uuid4().hex
            control = threading.Event()
            meta = dict(owner_id=user, status='queued', created_at=now(),
                        instance=self.instance, case='order_report', agents=body['agents'])
            self.save_meta(rid, meta)
            self.active = (rid, control)
            try:
                thread = threading.Thread(target=self._execute, args=(rid, config, meta, control), daemon=True)
                thread.start()
            except Exception:
                self.active = None
                meta.update(status='execution_failed', error='后台任务启动失败')
                self.save_meta(rid, meta)
                raise WebError('后台任务启动失败', 500)
            return rid

    def _execute(self, rid, config, meta, control):
        try:
            with self.lock:
                meta['status'] = 'stopping' if control.is_set() else 'running'
                self.save_meta(rid, meta)
            result = self.executor(config, self.root, run_id=rid, cancel_event=control)
            nodes = result.get('nodes', {})
            status = ('stopped' if control.is_set() else
                      'execution_failed' if not result.get('evidence_complete') or 'execution_failed' in nodes.values() else
                      'validation_failed' if 'validation_failed' in nodes.values() else 'completed')
            meta.update(status=status, finished_at=now())
        except Exception:
            meta.update(status='execution_failed', error='实验执行异常，请查看后端记录', finished_at=now())
        finally:
            with self.lock:
                try:
                    # Serialize finalization with stop(): a stop that won this lock
                    # cannot be overwritten by an already-computed completed state.
                    if control.is_set():
                        meta['status'] = 'stopped'
                    self.save_meta(rid, meta)
                finally:
                    control.set()
                    self.active = None

    def stop(self, rid, user):
        with self.lock:
            _, meta = self.meta(rid, user)
            if self.active and self.active[0] == rid:
                self.active[1].set()
                meta['status'] = 'stopping'
                self.save_meta(rid, meta)
                return dict(status='stopping')
            return dict(status=self.status(rid, meta, {}))

    def status(self, rid, meta, summary):
        if meta.get('status') in ('queued', 'running', 'stopping'):
            return meta['status'] if self.active and self.active[0] == rid else 'interrupted'
        if meta.get('status') == 'historical':
            if not summary:
                return 'interrupted'
            nodes = summary.get('nodes', {})
            for status in ('execution_failed', 'validation_failed', 'stopped'):
                if status in nodes.values():
                    return status
            return 'completed'
        return meta.get('status', 'interrupted')

    def _incremental(self, rid, root):
        cached = self.cache.setdefault(rid, dict(events=0, requests=0, nodes={}, calls={}, runtime={}, artifacts={}, inputs={}, node_models={}))
        # Bounded work per poll. Never rescan requests.jsonl from the beginning.
        for stream in ('events', 'requests'):
            for _ in range(4):
                part = page(root / (stream + '.jsonl'), cached[stream], 100)
                for item in part['items']:
                    row = item['record']
                    kind, nid = row.get('event_type'), row.get('node_id')
                    model = row.get('returned_model')
                    if kind == 'response_data':
                        data = row.get('data') or {}
                        model = data.get('model') or (data.get('message') or {}).get('model')
                    if model and nid:
                        models = cached['node_models'].setdefault(nid, [])
                        if model not in models: models.append(model)
                    if stream == 'events':
                        if kind == 'node_state': cached['nodes'][nid] = row.get('status')
                        if kind == 'runtime_validation': cached['runtime'][nid] = row.get('result')
                        if kind == 'artifact_published':
                            a = row.get('artifact', {})
                            cached['artifacts'][a.get('artifact_id')] = a
                        if kind == 'node_inputs': cached['inputs'][nid] = row.get('inputs', [])
                    elif kind in ('request_start', 'request_end') and row.get('request_id'):
                        cached['calls'][row['request_id']] = {k: v for k, v in row.items() if k != 'payload'}
                cached[stream] = part['cursor']
                if not part['more']: break
        return cached

    def detail(self, rid, user, full=False, index=True):
        with self.lock:
            root, meta = self.meta(rid, user)
            manifest = read_json(root / 'manifest.json', {})
            summary = read_json(root / 'summary.json', {})
            cached = self._incremental(rid, root) if index else self.cache.get(rid, {})
            status = self.status(rid, meta, summary)
            nodes = summary.get('nodes') or cached.get('nodes') or {n: 'pending' for n in 'ABCD'}
            if status == 'interrupted':
                nodes = {n: ('interrupted' if s in ('pending', 'ready', 'running', 'produced') else s) for n, s in nodes.items()}
            result = dict(run_id=rid, case=manifest.get('case', meta.get('case', 'order_report')),
                          created_at=meta.get('created_at'), status=status, nodes=nodes,
                          members=[{k: m.get(k) for k in ('id', 'provider_id', 'model', 'max_rounds', 'timeout')}
                                   for m in manifest.get('members', meta.get('agents', []))],
                          final=summary.get('final', {}), passed=summary.get('passed'),
                          evidence_complete=summary.get('evidence_complete'),
                          execution_backend=manifest.get('execution_backend'),
                          usage=summary.get('usage', usage_summary(list(cached.get('calls', {}).values()))),
                          node_models=cached.get('node_models', {}), error=meta.get('error'))
            if full:
                validation = read_json(root / 'validation.json', {})
                result.update(runtime=validation.get('runtime', summary.get('runtime_validation', cached.get('runtime', {}))),
                              artifacts=read_json(root / 'artifacts.json', cached.get('artifacts', {})),
                              tasks=manifest.get('tasks', case.TASKS), inputs=cached.get('inputs', {}),
                              request_errors=summary.get('request_errors', []))
            return safe(result)

    def listing(self, user):
        items = []
        if not self.metadata.exists(): return items
        for path in sorted(self.metadata.glob('*.json'), key=lambda p: p.stat().st_mtime, reverse=True):
            meta = {}
            try:
                meta = read_json(path, {})
                if meta.get('owner_id') == user:
                    items.append(self.detail(path.stem, user, index=False))
            except WebError as e:
                # Only expose damaged run ID if ownership is still verifiable.
                if 'meta' in locals() and meta.get('owner_id') == user:
                    items.append(dict(run_id=path.stem, status='corrupt', error=str(e)))
        return items

    def logs(self, rid, user, stream, cursor, limit, raw=False):
        root, _ = self.meta(rid, user)
        if stream not in ('events', 'requests'):
            raise WebError('记录类型非法')
        part = page(root / (stream + '.jsonl'), cursor, limit)
        if not raw:
            part['items'] = [item for item in part['items'] if item['record'].get('event_type') not in
                             ('response_data', 'agent_answer_delta', 'agent_reasoning_delta', 'agent_tool_call', 'agent_tool_result')]
        for item in part['items']:
            row = item['record']
            if not raw:
                allowed = ('timestamp', 'event_type', 'node_id', 'agent_id', 'request_id', 'tool_call_id',
                           'status', 'name', 'model', 'returned_model', 'transport_attempt', 'error_type', 'http_status')
                item['record'] = {k: row[k] for k in allowed if k in row}
            item['record'] = safe(item['record'])
        return part

    def artifact(self, rid, user, artifact_id):
        root, _ = self.meta(rid, user)
        artifacts = read_json(root / 'artifacts.json', {})
        if not artifacts:
            artifacts = self._incremental(rid, root)['artifacts']
        artifact = artifacts.get(artifact_id)
        if not isinstance(artifact, dict):
            raise WebError('产物不存在', 404)
        # Map only the declared fixed-case artifact identity, not a caller path.
        allowed = {f"{t['id']}/{n}": Path('nodes') / t['id'] / n for t in case.TASKS for n in t['outputs']}
        relative = allowed.get(artifact_id)
        if relative is None or artifact.get('path') != relative.as_posix():
            raise WebError('产物路径非法', 422)
        path = root / relative
        if any(p.is_symlink() for p in (root, *path.parents[:3], path)) or not path.resolve().is_relative_to(root.resolve()):
            raise WebError('拒绝符号链接或越界产物', 422)
        if not path.is_file() or path.stat().st_size > 4 * 1024 * 1024:
            raise WebError('产物不存在或过大', 422)
        if sha256(path) != artifact.get('sha256'):
            raise WebError('产物内容已改变，哈希不匹配', 422)
        return path


manager = Experiments()


def main():
    parser = argparse.ArgumentParser(description='将无归属历史实验明确关联到已有账号（不改动runs）')
    parser.add_argument('--assign-owner', required=True)
    parser.add_argument('--username', required=True)
    args = parser.parse_args()
    import db
    user = db.get_user_by_name(args.username)
    if not user: parser.error('账号不存在')
    try:
        manager.assign(args.assign_owner, user['id'])
    except WebError as e:
        parser.error(str(e))
    print('历史实验已关联；原始记录未改动。')


if __name__ == '__main__':
    main()
