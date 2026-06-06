#!/usr/bin/env bash
# geo-insar Phase 1.0 启动前置环境核查
#
# 用法:
#   bash preflight_check.sh           # 输出 markdown 表格到 stdout
#   bash preflight_check.sh --json    # 输出 JSON,供 /api/preflight 消费
#
# 检查项:
#   1. HyP3 API 端点连通(走 OpenVPN 出口)
#   2. 端口 8084 空闲
#   3. NASA Earthdata 凭证 (~/.netrc 或 credentials.yaml)
#   4. Python 依赖:hyp3_sdk / asf_search / sqlalchemy / flask / rasterio / pyyaml / lxml / shapely
#   5. OpenVPN 出口能访问公网
#   6. 测试 KML 存在
#
# 退出码: 0 = 全部通过;1 = 至少 1 项失败

set -u

JSON_MODE=0
[[ "${1:-}" == "--json" ]] && JSON_MODE=1

# 检查结果数组
declare -a NAMES STATUSES DETAILS

record() {
  NAMES+=("$1")
  STATUSES+=("$2")
  DETAILS+=("$3")
}

# 1. HyP3 端点
hyp3_code=$(curl -sS -o /dev/null -w "%{http_code}" --max-time 15 https://hyp3-api.asf.alaska.edu/ 2>/dev/null || echo "000")
if [[ "$hyp3_code" =~ ^(200|301|302|401|403)$ ]]; then
  record "HyP3 API 端点" "pass" "https://hyp3-api.asf.alaska.edu/ 返回 $hyp3_code"
else
  record "HyP3 API 端点" "fail" "连不上 HyP3,返回码 $hyp3_code(检查 OpenVPN 出口)"
fi

# 2. 端口 8084 —— 区分"被本项目占用"(服务已在跑)和"被外部进程占用"(冲突)
PROJECT_DIR="/opt/deepexplor-services/geo-insar"
port_proc=""
port_pid=""
port_skip=""
if command -v lsof &>/dev/null; then
  port_line=$(lsof -nP -iTCP:8084 -sTCP:LISTEN 2>/dev/null | awk 'NR>1 {print $1"|"$2; exit}')
  port_proc="${port_line%%|*}"
  port_pid="${port_line##*|}"
elif command -v ss &>/dev/null; then
  # ss 输出形如: users:(("python3",pid=18540,fd=3))
  ss_field=$(ss -tlnp 2>/dev/null | awk '$4 ~ /:8084$/ {print $NF; exit}')
  port_proc=$(echo "$ss_field" | sed -nE 's/.*\("([^"]+)".*/\1/p')
  port_pid=$(echo "$ss_field"  | sed -nE 's/.*pid=([0-9]+).*/\1/p')
else
  port_skip="(无 lsof/ss,跳过)"
fi

if [[ -n "$port_skip" ]]; then
  record "端口 8084" "warn" "$port_skip"
elif [[ -z "$port_pid" ]]; then
  record "端口 8084" "pass" "空闲"
else
  # 判断 PID 是否属于本项目(看 cwd 或命令行)
  proc_args=$(ps -p "$port_pid" -o args= 2>/dev/null | sed 's/^ *//')
  proc_cwd=""
  if command -v lsof &>/dev/null; then
    proc_cwd=$(lsof -a -p "$port_pid" -d cwd -Fn 2>/dev/null | awk '/^n/ {sub(/^n/,""); print; exit}')
  fi
  if [[ "$proc_cwd" == "$PROJECT_DIR"* ]] || [[ "$proc_args" == *"$PROJECT_DIR"* ]]; then
    record "端口 8084" "pass" "geo-insar 服务运行中 (PID: $port_pid, $port_proc) —— 重启前请先停掉此进程"
  else
    record "端口 8084" "fail" "已被外部进程占用: $port_proc (PID: $port_pid)${proc_cwd:+, cwd=$proc_cwd}"
  fi
fi

# 3. Earthdata 凭证
CREDS_PATH=""
if [[ -f /opt/deepexplor-services/geo-insar/config/credentials.yaml ]]; then
  CREDS_PATH="/opt/deepexplor-services/geo-insar/config/credentials.yaml"
elif [[ -f /opt/deepexplor-services/geo-downloader/config/credentials.yaml ]]; then
  CREDS_PATH="/opt/deepexplor-services/geo-downloader/config/credentials.yaml"
fi

if [[ -n "$CREDS_PATH" ]]; then
  if grep -q "nasa_earthdata" "$CREDS_PATH" 2>/dev/null; then
    has_user=$(grep -A 3 "^nasa_earthdata:" "$CREDS_PATH" | grep "username:" | awk '{print $2}')
    if [[ -n "$has_user" && "$has_user" != "your_earthdata_username" ]]; then
      record "Earthdata 凭证" "pass" "$CREDS_PATH (用户: $has_user)"
    else
      record "Earthdata 凭证" "warn" "$CREDS_PATH 存在但 username 还是模板默认值,请填入真实值"
    fi
  else
    record "Earthdata 凭证" "fail" "$CREDS_PATH 缺少 nasa_earthdata 段"
  fi
else
  record "Earthdata 凭证" "fail" "找不到 credentials.yaml(geo-insar 和 geo-downloader 都没有)"
fi

# 4. Python 依赖
python3 -c "import sys; sys.exit(0)" 2>/dev/null || {
  record "Python 3" "fail" "找不到 python3"
}

check_pkg() {
  local pkg="$1"
  if python3 -c "import $pkg" 2>/dev/null; then
    local ver=$(python3 -c "import $pkg; print(getattr($pkg, '__version__', '?'))" 2>/dev/null)
    record "Python: $pkg" "pass" "$ver"
  else
    record "Python: $pkg" "fail" "未安装(运行 pip install $pkg)"
  fi
}
check_pkg "hyp3_sdk"
check_pkg "asf_search"
check_pkg "sqlalchemy"
check_pkg "flask"
check_pkg "rasterio"
check_pkg "yaml"
check_pkg "lxml"
check_pkg "shapely"

# 5. 公网出口
public_ip=$(curl -sS --max-time 10 https://api.ipify.org 2>/dev/null || echo "")
if [[ -n "$public_ip" ]]; then
  record "公网出口" "pass" "公网 IP: $public_ip"
else
  record "公网出口" "fail" "无法连接 api.ipify.org(检查 OpenVPN)"
fi

# 6. 测试 KML
test_kml="/opt/deepexplor-services/geo-insar/test_data/zhaoyuan_miaoshan.kml"
if [[ -f "$test_kml" ]]; then
  size=$(wc -c < "$test_kml" | tr -d ' ')
  record "测试 KML" "pass" "$test_kml ($size bytes)"
else
  record "测试 KML" "warn" "$test_kml 不存在(Phase 1 验证用,不影响服务启动)"
fi

# ── 输出 ────────────────────────────────────────
fails=0
warns=0
for s in "${STATUSES[@]}"; do
  [[ "$s" == "fail" ]] && ((fails++))
  [[ "$s" == "warn" ]] && ((warns++))
done

if [[ $JSON_MODE -eq 1 ]]; then
  echo -n '{"checks":['
  for i in "${!NAMES[@]}"; do
    [[ $i -gt 0 ]] && echo -n ","
    # JSON escape 简单版(假设字段不含 " \ 控制字符)
    name="${NAMES[$i]//\"/\\\"}"
    status="${STATUSES[$i]}"
    detail="${DETAILS[$i]//\"/\\\"}"
    detail="${detail//$'\n'/\\n}"
    echo -n "{\"name\":\"$name\",\"status\":\"$status\",\"detail\":\"$detail\"}"
  done
  echo "],\"fails\":$fails,\"warns\":$warns,\"total\":${#NAMES[@]}}"
else
  echo "# geo-insar 启动前置检查报告"
  echo ""
  echo "时间: $(date -u +"%Y-%m-%dT%H:%M:%SZ")"
  echo "检查项: ${#NAMES[@]}  失败: $fails  警告: $warns"
  echo ""
  echo "| 检查项 | 状态 | 详情 |"
  echo "|---|---|---|"
  for i in "${!NAMES[@]}"; do
    icon="✅"
    [[ "${STATUSES[$i]}" == "fail" ]] && icon="❌"
    [[ "${STATUSES[$i]}" == "warn" ]] && icon="⚠️"
    echo "| ${NAMES[$i]} | $icon ${STATUSES[$i]} | ${DETAILS[$i]} |"
  done
fi

# 落盘报告(非 JSON 模式)
if [[ $JSON_MODE -eq 0 ]]; then
  LOGS_DIR="/opt/deepexplor-services/geo-insar/logs"
  mkdir -p "$LOGS_DIR"
  ts=$(date -u +"%Y%m%dT%H%M%SZ")
  echo "[落盘] $LOGS_DIR/preflight_$ts.md" >&2
fi

[[ $fails -gt 0 ]] && exit 1 || exit 0
