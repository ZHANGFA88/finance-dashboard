#!/usr/bin/env bash
# 搭建筹码分析独立环境 .cyqenv（隔离 akshare 重依赖，不污染主服务）
# 用法: bash scripts/setup_cyqenv.sh
set -e
cd "$(dirname "$0")/.."

echo "==> 创建独立虚拟环境 .cyqenv"
python3 -m venv .cyqenv

echo "==> 升级 pip"
.cyqenv/bin/pip install --upgrade pip -q

echo "==> 安装筹码分析依赖 (akshare/pandas/numpy/matplotlib)"
.cyqenv/bin/pip install -r scripts/cyq-requirements.txt

echo "==> 验证：跑通示例 600206"
env no_proxy='*' NO_PROXY='*' HTTP_PROXY='' HTTPS_PROXY='' ALL_PROXY='' \
  .cyqenv/bin/python scripts/cyq_report.py 600206 | head -c 300
echo ""
echo "==> 完成。筹码功能已就绪（主后端会自动通过 subprocess 调用）"
