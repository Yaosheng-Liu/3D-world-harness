"""Publish lightweight progress and live token counters without copying sessions."""
import argparse
import json
from pathlib import Path
import time

from common import matrix, read, write


def live_usage(home):
    snapshots=[]
    for path in (home/'sessions').rglob('*.jsonl'):
        last=None
        for line in path.open():
            try:row=json.loads(line)
            except json.JSONDecodeError:continue
            payload=row.get('payload',{})
            if row.get('type')=='event_msg' and payload.get('type')=='token_count' and payload.get('info'):
                last=payload['info'].get('total_token_usage')
        if last:snapshots.append(last)
    return {key:sum(row[key] for row in snapshots) if snapshots and all(key in row for row in snapshots) else None
            for key in ('input_tokens','output_tokens','cached_input_tokens','reasoning_output_tokens','total_tokens')}


def report(root):
    protocol=read(root/'protocol.json')
    rows=[]
    for scene in protocol['scenes']:
        for spec in matrix(protocol):
            identifier=f"{scene}__{spec['condition']}__{spec['model']}__{spec['effort']}"
            trial=root/'runs'/identifier
            state=read(trial/'status.json') if (trial/'status.json').exists() else {'stage':'queued','checkpoints':[]}
            usage={role:live_usage(trial/'homes'/role) for role in ('planner','modeler','reviewer')}
            rows.append({'id':identifier,'scene':scene,**spec,'stage':state['stage'],
                         'checkpoint':state.get('checkpoint'),'frozen_checkpoints':len(state['checkpoints']),
                         'error':state.get('error'),'live_role_usage':usage})
    terminal=sum(r['stage'] in ('modeled','failed') for r in rows)
    evaluations=len(list((root/'evaluation_results').glob('*/M*/complete.json')))
    result={'updated_at':time.time(),'expected_trials':len(rows),'terminal_trials':terminal,
            'modeled_trials':sum(r['stage']=='modeled' for r in rows),
            'failed_trials':sum(r['stage']=='failed' for r in rows),
            'evaluated_checkpoints':evaluations,'trials':rows}
    write(root/'progress.json',result)
    lines=['# WorldHarness 统一重跑进度','',
           f"共 {len(rows)} 次试验；建模完成 {result['modeled_trials']}，失败 {result['failed_trials']}；已评价 {evaluations} 个检查点。",'',
           '| 场景 | 流程 | 建模模型 | 强度 | 当前阶段 | 冻结版本数 |',
           '|---|---|---|---|---|---|']
    for r in rows:
        lines.append(f"| {r['scene']} | {r['condition']} | {r['model']} | {r['effort']} | {r['stage']} | {r['frozen_checkpoints']} |")
    lines+=['','Token 分角色明细见 progress.json；数字来自各独立线程最新累计快照。','',
            '模型完成、评价完成与语义编辑核验分别记录；未完成的项目不计为成功。']
    (root/'STATUS.md').write_text('\n'.join(lines)+'\n')
    return result


def main():
    parser=argparse.ArgumentParser()
    parser.add_argument('--root',type=Path,required=True)
    parser.add_argument('--watch',action='store_true')
    args=parser.parse_args()
    while True:
        report(args.root)
        if not args.watch:break
        time.sleep(30)


if __name__=='__main__':main()
