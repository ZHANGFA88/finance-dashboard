#!/usr/bin/env bash
# 金融大屏 微信推送公共库
# 用法: source push_lib.sh; wx_push "消息内容"
set -euo pipefail

OPENCLAW_BIN="${OPENCLAW_BIN:-/opt/homebrew/bin/openclaw}"
WX_TARGET="${WX_TARGET:-o9cq80zOXMiSDvE8M5PAioQH4PjM@im.wechat}"
API_BASE="${API_BASE:-http://127.0.0.1:8770}"

wx_push() {
  local msg="$1"
  "$OPENCLAW_BIN" message send --channel wechat --target "$WX_TARGET" --message "$msg" 2>&1
}
