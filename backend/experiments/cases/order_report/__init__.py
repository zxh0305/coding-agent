"""Fixed case, deterministic contracts, host-only independent scoring."""
import csv
import io
import json
from collections import Counter
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP, localcontext
from pathlib import Path

ROOT = Path(__file__).resolve().parent
TASKS = [
    dict(id='A', agent_id='data_worker', depends_on=[],
         inputs=['orders.csv', 'rules.md'], outputs=['cleaned.csv', 'cleaning.json'], validator='cleaning_v1'),
    dict(id='B', agent_id='data_worker', depends_on=['A'],
         inputs=['A/cleaned.csv', 'A/cleaning.json', 'rules.md'], outputs=['sales.json'], validator='sales_v1'),
    dict(id='C', agent_id='report_worker', depends_on=['A'],
         inputs=['A/cleaned.csv', 'A/cleaning.json', 'rules.md'], outputs=['quality.json'], validator='quality_v1'),
    dict(id='D', agent_id='report_worker', depends_on=['B', 'C'],
         inputs=['B/sales.json', 'C/quality.json', 'rules.md'], outputs=['report.md'], validator='report_v1'),
]
GOALS = dict(A='清洗订单并记录逐行拒绝原因', B='按渠道计算订单数量和销售额',
             C='汇总输入数据质量与各拒绝原因次数', D='根据销售额与质量产物形成固定格式报告')


def expected(root=ROOT):
    """Host oracle: always recompute from immutable case input, never model output."""
    rows, rejected = [], []
    with (root / 'orders.csv').open(encoding='utf-8', newline='') as f:
        original = list(csv.DictReader(f))
    for i, row in enumerate(original, 1):
        oid, channel, raw = [row[k].strip() for k in ('order_id', 'channel', 'amount')]
        reason = None
        if not oid:
            reason = 'missing_order_id'
        elif not channel:
            reason = 'missing_channel'
        else:
            try:
                value = Decimal(raw)
                if not value.is_finite():
                    reason = 'invalid_amount'
                elif value < 0:
                    reason = 'negative_amount'
            except InvalidOperation:
                reason = 'invalid_amount'
        if reason:
            rejected.append(dict(row=i, reason=reason))
        else:
            with localcontext() as ctx:
                ctx.prec = 50
                amount = value.quantize(Decimal('.01'), rounding=ROUND_HALF_UP)
            rows.append(dict(order_id=oid, channel=channel, amount=f'{amount:.2f}'))
    cleaning = dict(input_rows=len(original), valid_rows=len(rows), rejected_rows=len(rejected), rejections=rejected)
    channels = sorted({r['channel'] for r in rows})
    sales = dict(channels=[dict(channel=c, count=sum(r['channel'] == c for r in rows),
                               amount=f"{sum((Decimal(r['amount']) for r in rows if r['channel'] == c), Decimal(0)):.2f}")
                          for c in channels], total_count=len(rows),
                 total_amount=f"{sum((Decimal(r['amount']) for r in rows), Decimal(0)):.2f}")
    quality = {k: cleaning[k] for k in ('input_rows', 'valid_rows', 'rejected_rows')}
    quality['reasons'] = dict(Counter(r['reason'] for r in rejected))
    lines = ['# 订单报告'] + [f'{k}: {quality[k]}' for k in ('input_rows', 'valid_rows', 'rejected_rows')]
    lines += [f'{k}: {sales[k]}' for k in ('total_count', 'total_amount')]
    lines += [f"channel: {r['channel']} | count: {r['count']} | amount: {r['amount']}" for r in sales['channels']]
    lines += [f'reason: {r} | count: {quality["reasons"][r]}' for r in sorted(quality['reasons'])]
    lines += ['sources: B/sales.json, C/quality.json']
    out = io.StringIO(newline='')
    writer = csv.DictWriter(out, fieldnames=['order_id', 'channel', 'amount'], lineterminator='\n')
    writer.writeheader()
    writer.writerows(rows)
    return {'cleaned.csv': out.getvalue(), 'cleaning.json': cleaning, 'sales.json': sales,
            'quality.json': quality, 'report.md': '\n'.join(lines) + '\n'}


def runtime_validate(task, work):
    """Public contract checks only; never consult independent answers here."""
    errors = []
    def count(value):
        if type(value) is not int or value < 0:
            raise ValueError('计数必须为非负整数')
    def amount(value):
        import re
        if not isinstance(value, str) or not re.fullmatch(r'[0-9]+\.[0-9]{2}', value):
            raise ValueError('金额必须为非负两位小数字符串')
    for name in task['outputs']:
        path = work / name
        try:
            if path.is_symlink() or not path.is_file():
                raise ValueError('输出文件缺失或不是普通文件')
            text = path.read_text(encoding='utf-8')
            if name.endswith('.json'):
                data = json.loads(text)
                if not isinstance(data, dict):
                    raise ValueError('必须为JSON对象')
                if name in ('cleaning.json', 'quality.json'):
                    for key in ('input_rows', 'valid_rows', 'rejected_rows'):
                        count(data[key])
                    if data['input_rows'] != data['valid_rows'] + data['rejected_rows']:
                        raise ValueError('行数不守恒')
                    if name == 'cleaning.json':
                        if not isinstance(data['rejections'], list):
                            raise ValueError('rejections必须为列表')
                        for rejection in data['rejections']:
                            count(rejection['row'])
                            if rejection['row'] < 1 or not isinstance(rejection['reason'], str):
                                raise ValueError('拒绝记录格式错误')
                        if len(data['rejections']) != data['rejected_rows']:
                            raise ValueError('拒绝记录数量不一致')
                    else:
                        if not isinstance(data['reasons'], dict):
                            raise ValueError('reasons必须为对象')
                        for value in data['reasons'].values():
                            count(value)
                        if sum(data['reasons'].values()) != data['rejected_rows']:
                            raise ValueError('原因计数不一致')
                elif name == 'sales.json':
                    count(data['total_count'])
                    amount(data['total_amount'])
                    if not isinstance(data['channels'], list):
                        raise ValueError('channels必须为列表')
                    for channel in data['channels']:
                        if not isinstance(channel['channel'], str) or not channel['channel'].strip():
                            raise ValueError('渠道不能为空')
                        count(channel['count'])
                        amount(channel['amount'])
            elif name.endswith('.csv'):
                reader = csv.DictReader(io.StringIO(text))
                if reader.fieldnames != ['order_id', 'channel', 'amount']:
                    raise ValueError('CSV字段错误')
                for row in reader:
                    if set(row) != {'order_id', 'channel', 'amount'} or not row['order_id'] or not row['channel']:
                        raise ValueError('CSV记录格式错误')
                    amount(row['amount'])
            elif not text.strip():
                raise ValueError('报告为空')
        except (OSError, ValueError, KeyError, TypeError) as e:
            errors.append(f'{name}: {e}')
    return dict(passed=not errors, errors=errors, validator=task['validator'], scope='runtime')


def final_validate(workspaces, root=ROOT):
    oracle = expected(root)
    results = {}
    for task in TASKS:
        errors = []
        work = workspaces.get(task['id'])
        if work is None:
            results[task['id']] = dict(passed=False, status='not_evaluated')
            continue
        for name in task['outputs']:
            try:
                text = (work / name).read_text(encoding='utf-8')
                target = oracle[name]
                if name.endswith('.json'):
                    # Exact serialized types: true must not compare equal to integer 1.
                    actual = json.dumps(json.loads(text), sort_keys=True, ensure_ascii=False)
                    valid = actual == json.dumps(target, sort_keys=True, ensure_ascii=False)
                elif name.endswith('.csv'):
                    valid = list(csv.DictReader(io.StringIO(text))) == list(csv.DictReader(io.StringIO(target)))
                else:
                    prefixes = ('input_rows:', 'valid_rows:', 'rejected_rows:', 'total_count:',
                                'total_amount:', 'channel:', 'reason:', 'sources:')
                    actual = [line.strip() for line in text.splitlines() if line.strip().startswith(prefixes)]
                    valid = actual == target.splitlines()[1:] and '# 订单报告' in text.splitlines()
                if not valid:
                    errors.append(f'{name}: 独立计算结果不匹配')
            except (OSError, ValueError) as e:
                errors.append(f'{name}: {e}')
        results[task['id']] = dict(passed=not errors, status='evaluated', errors=errors)
    return results
