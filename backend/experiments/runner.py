"""Run from backend: python -m experiments.runner --case order_report --config FILE."""
import argparse
import copy
import hashlib
import json
import shutil
import subprocess
import uuid
import threading
from pathlib import Path

from config import PROJECT_DIR
from experiments.agents import ExperimentAgent, FILE_TOOLS, resolve_client
from experiments.cases import order_report as case
from experiments.recording import EvidenceError, Recorder, RequestObserver, sha256, usage_summary


def validate_plan(tasks, configs):
    members = {c['id']: c for c in configs}
    if len(members) != len(configs):
        raise ValueError('成员ID重复')
    for member in configs:
        permitted = {'id', 'provider_id', 'model', 'prompt_file', 'allowed_tools', 'max_rounds', 'timeout'}
        if set(member) - permitted:
            raise ValueError('成员配置包含不支持的字段（禁止在实验配置中放密钥）')
        if not all(isinstance(member.get(k), str) and member[k].strip()
                   for k in ('id', 'provider_id', 'model', 'prompt_file')):
            raise ValueError('成员必须显式指定ID、供应商、模型和提示词文件')
        tools = member.get('allowed_tools')
        if not isinstance(tools, list) or not tools or not set(tools) <= FILE_TOOLS:
            raise ValueError('第一阶段仅允许read_file/write_file/list_dir')
        for name, default, maximum in [('max_rounds', 12, 100), ('timeout', 60, 600)]:
            value = member.get(name, default)
            if type(value) is not int or not 1 <= value <= maximum:
                raise ValueError(f'{name}超出允许范围')
    ids = [t['id'] for t in tasks]
    if len(set(ids)) != len(ids):
        raise ValueError('节点ID重复')
    seen = {}
    for task in tasks:
        if task['agent_id'] not in members:
            raise ValueError('节点成员不存在')
        if not task['outputs'] or not task['inputs'] or not task.get('validator'):
            raise ValueError('节点缺少输入、输出或验收器')
        if not set(task['depends_on']) <= seen.keys():
            raise ValueError('任务图有环、未知依赖或不符合固定拓扑顺序')
        for name in task['outputs']:
            if Path(name).name != name or name in ('', '.', '..'):
                raise ValueError('输出必须为本节点文件名')
        if len(set(task['outputs'])) != len(task['outputs']):
            raise ValueError('输出声明重复')
        for name in task['inputs']:
            parts = Path(name).parts
            if Path(name).is_absolute() or '..' in parts or not parts:
                raise ValueError('输入路径非法')
            if len(parts) == 2:
                parent, filename = parts
                if parent not in task['depends_on'] or filename not in seen[parent]['outputs']:
                    raise ValueError('输入未引用已声明上游产物')
            elif len(parts) != 1 or not (case.ROOT / name).is_file():
                raise ValueError('输入文件不存在')
        seen[task['id']] = task
    return members


def run(config, output_root, config_base=None, client_factory=resolve_client, *,
        run_id=None, cancel_event=None):
    config = copy.deepcopy(config)
    if set(config) != {'agents'} or not isinstance(config['agents'], list):
        raise ValueError('配置必须且仅包含agents数组')
    tasks = copy.deepcopy(case.TASKS)
    members = validate_plan(tasks, config['agents'])
    prompts = {}
    base = Path(config_base or case.ROOT)
    for mid, member in members.items():
        path = (base / member['prompt_file']).resolve()
        prompts[mid] = path.read_text(encoding='utf-8')
    run_id = run_id or uuid.uuid4().hex
    if not isinstance(run_id, str) or len(run_id) != 32 or any(c not in "0123456789abcdef" for c in run_id):
        raise ValueError("实验编号非法")
    owns_cancel = cancel_event is None
    cancel_event = cancel_event if cancel_event is not None else threading.Event()
    root = Path(output_root).resolve() / run_id
    root.mkdir(parents=True, exist_ok=False)
    recorder = Recorder(root, run_id)
    states = {t['id']: 'pending' for t in tasks}
    runtime, artifacts, workspaces = {}, {}, {}
    contexts = {t['id']: dict(node_id=t['id'], attempt_id=uuid.uuid4().hex,
                             agent_id=t['agent_id'], provider=members[t['agent_id']]['provider_id'],
                             model=members[t['agent_id']]['model']) for t in tasks}
    source_paths = list((Path(__file__).parent).rglob('*.py'))
    source_paths += [PROJECT_DIR / 'backend' / name for name in
                     ('agent.py', 'llm_client.py', 'tools.py', 'code_tools.py', 'permissions.py')]
    versions = {str(p.relative_to(PROJECT_DIR)): sha256(p) for p in source_paths}
    try:
        revision = subprocess.run(['git', 'rev-parse', 'HEAD'], cwd=PROJECT_DIR,
                                  capture_output=True, text=True, check=True).stdout.strip()
    except (OSError, subprocess.SubprocessError):
        revision = None
    snapshot = root / 'case'
    snapshot.mkdir()
    for filename in ('orders.csv', 'rules.md'):
        shutil.copyfile(case.ROOT / filename, snapshot / filename)
    execution_backend = 'configured_models' if client_factory is resolve_client else 'injected_client_factory'
    manifest = dict(schema_version=1, case='order_report', run_id=run_id,
                    execution_backend=execution_backend,
                    tasks=tasks, members=config['agents'], prompts=prompts,
                    prompt_hashes={mid: hashlib.sha256(p.encode()).hexdigest()
                                   for mid, p in prompts.items()},
                    source_hashes=versions, git_revision=revision,
                    input_hashes={p.name: sha256(p) for p in snapshot.iterdir()},
                    execution='serial', memory='disabled', compaction='disabled',
                    final_evaluation='host_only_after_execution', monetary_cost='not_evaluated')
    interrupted = False
    try:
        recorder.save('manifest.json', manifest)
        for task in tasks:
            nid = task['id']
            context = contexts[nid]
            def state(value):
                states[nid] = value
                recorder.event('node_state', context, status=value)
            if interrupted or cancel_event.is_set():
                state('stopped')
                continue
            if any(states[d] != 'validated' for d in task['depends_on']):
                state('blocked')
                continue
            state('ready')
            work = root / 'nodes' / nid
            work.mkdir(parents=True)
            workspaces[nid] = work
            readable = []
            input_records = []
            for name in task['inputs']:
                parts = Path(name).parts
                if len(parts) == 1:
                    source = snapshot / name
                    expected_hash = manifest['input_hashes'][name]
                else:
                    source = workspaces[parts[0]] / parts[1]
                    expected_hash = artifacts[name]['sha256']
                if source.is_symlink() or sha256(source) != expected_hash:
                    raise EvidenceError('输入产物在发布后被修改')
                target = work / 'inputs' / name
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(source, target)
                if sha256(target) != expected_hash:
                    raise EvidenceError('输入复制校验失败')
                readable.append(target)
                input_records.append(dict(source=name, path=str(target.relative_to(root)), sha256=expected_hash))
            recorder.event('node_inputs', context, inputs=input_records)
            agent = None
            try:
                state('running')
                observer = RequestObserver(recorder, context)
                client = client_factory(members[task['agent_id']], observer)
                agent = ExperimentAgent(llm=client, system_prompt=prompts[task['agent_id']],
                    max_rounds=members[task['agent_id']].get('max_rounds', 12),
                    verbose=False, vision_supported=False, workspace=work, context_window=0,
                    allowed_tools=tuple(members[task['agent_id']]['allowed_tools']),
                    model_tag=(context['provider'], context['model']), recorder=recorder,
                    context=context, readable=readable, writable=[work / n for n in task['outputs']])
                prompt = (f"节点 {nid}: {case.GOALS[nid]}。\n"
                          f"输入（先读取inputs/rules.md）：{json.dumps(['inputs/' + n for n in task['inputs']], ensure_ascii=False)}\n"
                          f"输出（相对当前工作目录）：{json.dumps(task['outputs'])}。\n"
                          '仅根据输入与规则完成任务，不得访问验收器、其他节点或隐藏答案。')
                done = None
                for kind, payload in agent.run(prompt, cancel_event=cancel_event):
                    recorder.event('agent_' + kind, context, payload=payload)
                    if kind == 'done':
                        done = payload
                if recorder.incomplete:
                    raise EvidenceError('执行期间记录失败')
                if cancel_event.is_set():
                    state('stopped')
                    interrupted = True
                    continue
                if observer.current and observer.current['status'] != 'completed':
                    raise RuntimeError('最后一次模型请求未完整结束')
                if not done:
                    raise RuntimeError('执行循环未返回完成事件')
                if done.get('stopped'):
                    state('stopped')
                    interrupted = True
                    continue
                state('produced')
                runtime[nid] = case.runtime_validate(task, work)
                unread = set(readable) - agent.input_reads
                if unread:
                    runtime[nid]['passed'] = False
                    runtime[nid]['errors'].append('声明输入未实际读取: ' + ', '.join(
                        str(p.relative_to(work)) for p in sorted(unread)))
                recorder.event('runtime_validation', context, result=runtime[nid])
                if not runtime[nid]['passed']:
                    state('validation_failed')
                    continue
                # Inputs must still match the versions actually handed over.
                if any(sha256(root / r['path']) != r['sha256'] for r in input_records):
                    raise EvidenceError('只读输入在执行时被修改')
                for name in task['outputs']:
                    path = work / name
                    record = dict(artifact_id=f'{nid}/{name}', node_id=nid,
                                  attempt_id=context['attempt_id'], path=str(path.relative_to(root)),
                                  sha256=sha256(path), inputs=input_records,
                                  validator=task['validator'], validation_status='runtime_validated')
                    artifacts[f'{nid}/{name}'] = record
                    recorder.event('artifact_published', context, artifact=record)
                state('validated')
            except KeyboardInterrupt:
                if agent:
                    agent.stop()
                interrupted = True
                state('stopped')
            except EvidenceError:
                raise
            except Exception as e:
                recorder.event('node_error', context, error_type=type(e).__name__)
                state('stopped' if cancel_event.is_set() else 'execution_failed')
        final = ({n: dict(passed=False, status='not_evaluated') for n in states}
                 if cancel_event.is_set() else case.final_validate(workspaces, snapshot))
        recorder.save('validation.json', dict(runtime=runtime, final=final))
    except (EvidenceError, KeyboardInterrupt):
        recorder.incomplete = True
        for nid in states:
            if states[nid] in ('pending', 'ready', 'running', 'produced'):
                states[nid] = 'stopped'
        final = {}
    summary = dict(run_id=run_id, directory=str(root), nodes=states,
                   execution_backend=execution_backend,
                   assignments={t['id']: contexts[t['id']] for t in tasks},
                   passed=(not recorder.incomplete and all(s == 'validated' for s in states.values())
                           and bool(final) and all(r['passed'] for r in final.values())),
                   evidence_complete=not recorder.incomplete,
                   usage=usage_summary(recorder.requests),
                   unknown_usage_requests=[r['request_id'] for r in recorder.requests if r['usage_status'] == 'unknown'],
                   request_errors=[dict(request_id=r['request_id'], node_id=r['node_id'],
                                        status=r['status'], error_type=r.get('error_type'),
                                        http_status=r.get('http_status'))
                                   for r in recorder.requests if r['status'] != 'completed'],
                   runtime_validation=runtime,
                   unevaluated=[n for n in states if final.get(n, {}).get('status') != 'evaluated'],
                   artifacts=list(artifacts), final=final)
    try:
        recorder.save('artifacts.json', artifacts)
        recorder.save('summary.json', summary)
    except EvidenceError:
        summary['evidence_complete'] = False
        summary['passed'] = False
    if owns_cancel:
        cancel_event.set()
    return summary


def main():
    parser = argparse.ArgumentParser(description='智序：固定任务图实验入口')
    parser.add_argument('--case', choices=['order_report'], required=True)
    parser.add_argument('--config', type=Path, required=True)
    parser.add_argument('--output-root', type=Path, default=PROJECT_DIR / 'runs')
    args = parser.parse_args()
    try:
        config = json.loads(args.config.read_text(encoding='utf-8'))
        result = run(config, args.output_root, args.config.resolve().parent)
    except (OSError, ValueError) as e:
        parser.exit(2, f'实验配置或输出目录错误：{type(e).__name__}\n')
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if result['passed'] else 1


if __name__ == '__main__':
    raise SystemExit(main())
