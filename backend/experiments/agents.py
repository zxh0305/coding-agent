"""Reuse Agent's loop with a narrow, enforced file-only execution boundary."""
import json
import sqlite3
from pathlib import Path

from agent import Agent
from config import PROJECT_DIR
from llm_client import create_client
from tools import error_result
from experiments.recording import sha256

FILE_TOOLS = frozenset({'read_file', 'write_file', 'list_dir'})


def resolve_client(config, observer, database=None):
    # Read-only connection: never initializes/migrates or silently selects defaults.
    path = Path(database or PROJECT_DIR / 'data' / 'agent_data.db').resolve()
    with sqlite3.connect(path.as_uri() + '?mode=ro', uri=True) as conn:
        conn.row_factory = sqlite3.Row
        provider = conn.execute('SELECT * FROM providers WHERE id=?',
                                (config['provider_id'],)).fetchone()
        model = conn.execute('SELECT * FROM provider_models WHERE provider_id=? AND name=?',
                             (config['provider_id'], config['model'])).fetchone()
    if not provider or not provider['enabled'] or not model or not model['enabled']:
        raise ValueError('指定供应商或模型不存在/未启用，实验不允许回退')
    provider = dict(provider)
    if not provider.get('api_key'):
        raise ValueError('指定供应商未配置密钥')
    if provider.get('api_format') not in ('openai', 'anthropic'):
        raise ValueError('不支持的模型协议')
    client = create_client(provider['api_format'], api_key=provider['api_key'],
                           base_url=provider['base_url'], model=config['model'],
                           timeout=config.get('timeout', 60),
                           reasoning_replay=bool(provider.get('reasoning_replay')))
    client.request_observer = observer
    return client


class ExperimentAgent(Agent):
    def __init__(self, *, recorder, context, readable, writable, **kwargs):
        super().__init__(**kwargs)
        self.recorder, self.experiment_context = recorder, context
        self.readable = {p.resolve() for p in readable}
        self.writable = {p.resolve() for p in writable}
        self.input_reads = set()
        # No attachment/personal-memory bridges in experiment mode.
        self.ctx.bridge_roots = lambda: {}

    def _memory_blocks(self):
        return ''

    def _maybe_compact(self, force=False):
        # Small fixed tasks: no implicit extra model calls or hidden summaries.
        return None

    def _run_tool(self, call):
        context = dict(self.experiment_context, tool_call_id=call.get('id'),
                       request_id=getattr(self.llm, 'experiment_request_id', None))
        observer = getattr(self.llm, 'request_observer', None)
        if observer and observer.current:
            context['request_id'] = observer.current['request_id']
        self.recorder.event('tool_request', context, call=call)
        name = (call.get('function') or {}).get('name')
        try:
            if name not in FILE_TOOLS or name not in self.allowed_tools:
                raise ValueError('工具不在本实验成员白名单内')
            args = json.loads(call['function'].get('arguments') or '{}')
            if not isinstance(args, dict):
                raise ValueError('工具参数必须为对象')
            raw = args.get('path', '.' if name == 'list_dir' else '')
            if not isinstance(raw, str) or not raw:
                raise ValueError('文件路径不能为空')
            path = (self.ctx.workspace / raw).resolve()
            if not path.is_relative_to(self.ctx.workspace.resolve()):
                raise ValueError('禁止访问当前节点工作目录之外的路径')
            if name == 'write_file' and path not in self.writable:
                raise ValueError('仅允许写入本节点声明的输出，输入只读')
            if name == 'read_file' and path not in self.readable | self.writable:
                raise ValueError('仅允许读取声明的输入与输出')
            if name == 'list_dir' and path not in {
                    self.ctx.workspace.resolve(), *(p.parent for p in self.readable | self.writable)}:
                raise ValueError('禁止枚举未声明目录')
        except (ValueError, KeyError, TypeError) as e:
            result = error_result(str(e))
        else:
            result = super()._run_tool(call)
        fields = {}
        if name == 'read_file' and json.loads(result).get('ok'):
            fields = dict(path=str(path.relative_to(self.ctx.workspace)), sha256=sha256(path))
            self.input_reads.add(path)
        self.recorder.event('tool_response', context, name=name, result=result, **fields)
        return result
