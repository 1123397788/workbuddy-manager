"""把 release.yml 的「组装发布目录」步骤**真跑一遍**（到 tar 为止）。

为什么值得单独跑：那段 shell 里既有文件搬运，又有「从载体 Release 取上游源码并
塞进包内」的逻辑——它是用户拿到上游源码的唯一常规渠道，写错了不会报错，只会
让包里的 `upstream/` 悄悄少掉（新装用户于是装不上）。

本机没有 `zip`，所以只跑到 `tar czf` 那一步；验证的是包内结构与 upstream/ 内容。
"""
from __future__ import annotations

import os
import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def main() -> int:
    import yaml
    d = yaml.safe_load((ROOT / '.github' / 'workflows' / 'release.yml').read_text(encoding='utf-8'))
    job = next(iter(d['jobs'].values()))
    step = next(s for s in job['steps'] if s.get('id') == 'pack')
    script = step['run']
    # 去掉 GitHub 表达式与 zip（本机没有 zip），其余原样执行
    script = script.replace('${{ steps.vars.outputs.tag }}', 'v9.9.9-test')
    script = script.replace('${{ github.repository }}', 'ithtelab/workbuddy-manager')
    script = '\n'.join(l for l in script.splitlines() if 'zip -qr' not in l)
    script = script.replace('rm -rf "$STAGE"', 'rm -rf "$STAGE"')  # 保持原样

    work = ROOT / 'dev' / '.pack-test'
    if work.exists():
        import shutil
        shutil.rmtree(work, ignore_errors=True)
    work.mkdir(parents=True)
    # 搭一个「checkout 现场」：按 CI 里 checkout 的结果复制需要的路径。
    # 不用 git archive + tar 解：本机 tar 对中文文件名会报 Invalid empty pathname
    # （Windows 上的编码怪癖，CI 跑在 Linux 没这问题）。
    repo = work / 'repo'
    repo.mkdir()
    for rel in ('server', 'deploy', 'docs', 'web'):
        src = ROOT / rel
        if rel == 'web':
            # 只要 web/out（CI 里也是构建产物），源码目录不必带
            shutil.copytree(ROOT / 'web' / 'out', repo / 'web' / 'out')
            continue
        shutil.copytree(src, repo / rel,
                        ignore=shutil.ignore_patterns('__pycache__', '.pytest_cache'))
    for name in ('.env.example', 'README.md', 'README.en.md', 'CHANGELOG.md', 'LICENSE',
                 'Dockerfile', 'docker-compose.yml'):
        src = ROOT / name
        if src.is_file():
            shutil.copyfile(src, repo / name)

    print('=== 执行组装步骤（zip 那行跳过）===')
    r = subprocess.run(['bash', '-c', script], cwd=repo, capture_output=True,
                       text=True, encoding='utf-8', errors='replace')
    print(r.stdout[-3000:] if r.stdout else '')
    if r.stderr:
        print(r.stderr[-1500:], file=sys.stderr)
    if r.returncode != 0:
        print(f'组装步骤退出码 {r.returncode}', file=sys.stderr)
        return 1

    stage = repo / 'workbuddy-manager-v9.9.9-test'
    problems: list[str] = []

    def check(ok: bool, label: str, detail: str = '') -> None:
        print(f'  {"✓" if ok else "✗"}  {label}' + (f'\n       {detail}' if detail else ''))
        if not ok:
            problems.append(label)

    check(stage.is_dir(), '组装出发布目录', str(stage.name))
    up = stage / 'upstream'
    files = sorted(p for p in up.rglob('*') if p.is_file()) if up.is_dir() else []
    check(up.is_dir() and len(files) > 200, 'upstream/ 已内嵌（来自载体 Release）',
          f'文件数={len(files)}')
    for name in ('docker-compose.yml', 'Dockerfile', 'LICENSE', 'scripts/task_runner.py'):
        check((up / name).is_file(), f'upstream/{name} 就位')
    check(not (up / '.git').exists(), '内嵌的 upstream/ 不含 .git')
    check(not (up / 'config.json').exists() and not (up / 'auths').exists()
          and not (up / 'data').exists(), '内嵌的 upstream/ 不含运行时数据与凭据')
    check((stage / '.version').is_file(), '.version 已写入')
    check((stage / 'server' / 'main.py').is_file(), 'server/ 已打包')

    # 打成 tar 再验一次（发布产物就是这个）。用 tarfile 读清单：本机 tar 的输出
    # 含非 UTF-8 文件名时会让 text 解码失败（harness 的坑，不是包的问题）。
    subprocess.run(['tar', 'czf', f'{stage.name}.tar.gz', stage.name], cwd=repo, check=True)
    import tarfile
    with tarfile.open(repo / f'{stage.name}.tar.gz', 'r:gz') as tf:
        names = tf.getnames()
    check(f'{stage.name}/upstream/scripts/task_runner.py' in names,
          '发布 tar 包内含上游源码')
    check(f'{stage.name}/deploy/install.sh' in names, '发布 tar 包内含安装脚本')
    size = (repo / f'{stage.name}.tar.gz').stat().st_size
    print(f'\n发布包大小：{size / 1024 / 1024:.1f} MB')
    print('=== 结果 ===')
    if problems:
        print(f'✗ {len(problems)} 项未通过：' + '、'.join(problems))
        return 1
    print('ALL CHECKS PASSED')
    return 0


if __name__ == '__main__':
    sys.exit(main())
