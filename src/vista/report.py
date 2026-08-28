"""L4 会话报告 —— 单文件 HTML，零外部依赖，离线可看。

设计取向刻意做成"示波器 / 仪表盘"：一次 agent 运行本质上是一段可观测的过程，
最值得看的量是**上下文压力随步数的变化**。因此签名元素是那条 token 波形——
压缩发生时曲线上会切出一个明显的凹口，θ 阈值线画成虚线横贯全图。
这一眼就能说明 Anchor Compression 在做什么，比任何文字都直观。

实现约束：
    - 不引任何 CDN、不引 web font（报告必须能在离线环境、断网答辩现场打开）
    - 数据以 JSON 内嵌，渲染用原生 JS + 手绘 SVG
    - 不使用任何前端框架
"""

from __future__ import annotations

import html
import json
from pathlib import Path

from .memory.archive import load_session

# ---------------------------------------------------------------------------
CSS = """
:root{
  --ink:#0f1720; --panel:#16212e; --panel-2:#1b2836; --line:#24344a;
  --text:#d7e2ee; --muted:#7d8fa5; --dim:#4c6076;
  --signal:#5ec8d8; --warn:#e8a33d; --fail:#e2607a; --ok:#6fcf97; --violet:#9b8cf0;
  --mono: ui-monospace, SFMono-Regular, "SF Mono", Menlo, Consolas, "Liberation Mono", monospace;
  --sans: -apple-system, BlinkMacSystemFont, "Segoe UI", "PingFang SC", "Hiragino Sans GB",
          "Microsoft YaHei", "Noto Sans CJK SC", sans-serif;
}
*{box-sizing:border-box}
html,body{margin:0;padding:0;background:var(--ink);color:var(--text);font-family:var(--sans)}
body{padding:28px 20px 80px;line-height:1.55;-webkit-font-smoothing:antialiased}
.wrap{max-width:1080px;margin:0 auto}

/* ---------- 标题区 ---------- */
.masthead{display:flex;align-items:flex-start;gap:18px;flex-wrap:wrap;
  border-bottom:1px solid var(--line);padding-bottom:18px;margin-bottom:22px}
.brand{font-family:var(--mono);font-size:13px;letter-spacing:.32em;color:var(--signal);
  text-transform:uppercase}
.brand b{font-weight:600}
.masthead h1{font-size:20px;font-weight:600;margin:6px 0 0;line-height:1.4;flex:1 1 420px;
  min-width:260px;word-break:break-word}
.sid{font-family:var(--mono);font-size:12px;color:var(--dim);margin-top:6px}
.verdict{font-family:var(--mono);font-size:12px;padding:6px 12px;border-radius:2px;
  border:1px solid currentColor;white-space:nowrap;align-self:center}
.v-ok{color:var(--ok)} .v-warn{color:var(--warn)} .v-fail{color:var(--fail)}

/* ---------- 指标条 ---------- */
.metrics{display:grid;grid-template-columns:repeat(auto-fit,minmax(112px,1fr));
  gap:1px;background:var(--line);border:1px solid var(--line);margin-bottom:26px}
.metric{background:var(--panel);padding:12px 14px}
.metric .k{font-size:11px;color:var(--muted);letter-spacing:.06em}
.metric .v{font-family:var(--mono);font-size:19px;margin-top:3px;color:var(--text)}
.metric .v small{font-size:12px;color:var(--dim)}

/* ---------- 区块 ---------- */
section{margin:30px 0}
h2{font-size:12px;font-weight:600;letter-spacing:.22em;text-transform:uppercase;
  color:var(--muted);margin:0 0 4px;font-family:var(--mono)}
.sub{font-size:13px;color:var(--dim);margin:0 0 14px}

/* ---------- 波形图 ---------- */
.scope{background:var(--panel);border:1px solid var(--line);padding:14px 8px 6px}
.scope svg{display:block;width:100%;height:auto}
.legend{display:flex;gap:18px;flex-wrap:wrap;font-family:var(--mono);font-size:11px;
  color:var(--muted);padding:10px 10px 4px}
.legend i{display:inline-block;width:16px;height:2px;vertical-align:middle;margin-right:6px}

/* ---------- 徽章 ---------- */
.badges{display:flex;flex-wrap:wrap;gap:1px;background:var(--line);
  border:1px solid var(--line)}
.badge{background:var(--panel);padding:10px 16px;flex:1 1 140px}
.badge .n{font-family:var(--mono);font-size:17px}
.badge .t{font-size:11px;color:var(--muted);margin-top:2px}
.badge.hot .n{color:var(--warn)} .badge.bad .n{color:var(--fail)}
.badge.good .n{color:var(--ok)}

/* ---------- 时间线 ---------- */
.timeline{border:1px solid var(--line);border-bottom:none}
.row{border-bottom:1px solid var(--line);background:var(--panel)}
.row>summary{list-style:none;cursor:pointer;padding:9px 14px;display:flex;
  gap:12px;align-items:baseline;font-family:var(--mono);font-size:12.5px}
.row>summary::-webkit-details-marker{display:none}
.row>summary:hover{background:var(--panel-2)}
.row .step{color:var(--dim);min-width:34px}
.row .tag{min-width:96px;color:var(--signal)}
.row .desc{color:var(--text);flex:1;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.row .meta{color:var(--dim);font-size:11px;white-space:nowrap}
.row[data-k="tool_error"] .tag,.row[data-k="verify_fail"] .tag{color:var(--fail)}
.row[data-k="compaction"] .tag{color:var(--warn)}
.row[data-k="verify"] .tag{color:var(--ok)}
.row[data-k="assistant"] .tag{color:var(--violet)}
.body{padding:2px 14px 14px 60px;background:var(--panel-2)}
.body pre{font-family:var(--mono);font-size:11.5px;color:var(--muted);margin:8px 0 0;
  white-space:pre-wrap;word-break:break-word;max-height:340px;overflow:auto;
  border-left:2px solid var(--line);padding-left:12px}
.body .lbl{font-family:var(--mono);font-size:10.5px;letter-spacing:.14em;
  text-transform:uppercase;color:var(--dim);margin-top:10px}

/* ---------- 表格 ---------- */
table{width:100%;border-collapse:collapse;font-size:12.5px}
th,td{text-align:left;padding:7px 12px;border-bottom:1px solid var(--line)}
th{font-family:var(--mono);font-size:11px;color:var(--muted);font-weight:500;
  letter-spacing:.08em;text-transform:uppercase}
td{font-family:var(--mono);color:var(--text)}
td.n{text-align:right;color:var(--muted)}
.bar{height:3px;background:var(--signal);opacity:.55;margin-top:4px}

footer{margin-top:44px;padding-top:16px;border-top:1px solid var(--line);
  font-family:var(--mono);font-size:11px;color:var(--dim)}
a{color:var(--signal)}
@media (prefers-reduced-motion:no-preference){
  .row>summary{transition:background .12s ease}
}
"""

JS = """
(function(){
  const D = JSON.parse(document.getElementById('vista-data').textContent);
  const NS = 'http://www.w3.org/2000/svg';
  const el = (n,a)=>{const e=document.createElementNS(NS,n);
    for(const k in (a||{})) e.setAttribute(k,a[k]); return e;};

  function waveform(){
    const host = document.getElementById('scope');
    const pts = D.series || [];
    if(pts.length < 2){ host.innerHTML =
      '<div style="padding:24px;color:#4c6076;font-family:var(--mono);font-size:12px">'+
      '本次会话步数太少，没有可绘制的上下文曲线。</div>'; return; }

    const W=1000,H=280,PL=58,PR=18,PT=18,PB=32;
    const iw=W-PL-PR, ih=H-PT-PB;
    const maxT = Math.max(D.budget||1, ...pts.map(p=>p.tokens))*1.06;
    const maxS = Math.max(...pts.map(p=>p.step));
    const X = s => PL + (maxS<=1?0:(s-1)/(maxS-1))*iw;
    const Y = t => PT + ih - (t/maxT)*ih;

    const svg = el('svg',{viewBox:`0 0 ${W} ${H}`, role:'img',
      'aria-label':'上下文 token 随步数的变化'});

    // 网格
    for(let i=0;i<=4;i++){
      const y = PT + ih*i/4, v = maxT*(1-i/4);
      svg.appendChild(el('line',{x1:PL,y1:y,x2:W-PR,y2:y,stroke:'#24344a','stroke-width':1}));
      const tx = el('text',{x:PL-10,y:y+4,'text-anchor':'end',fill:'#4c6076',
        'font-size':11,'font-family':'var(--mono)'});
      tx.textContent = (v/1000).toFixed(v>=10000?0:1)+'k';
      svg.appendChild(tx);
    }

    // θ 阈值线
    if(D.threshold>0 && D.threshold<maxT){
      const y=Y(D.threshold);
      svg.appendChild(el('line',{x1:PL,y1:y,x2:W-PR,y2:y,stroke:'#e8a33d',
        'stroke-width':1.5,'stroke-dasharray':'6 5',opacity:.8}));
      const t=el('text',{x:W-PR,y:y-7,'text-anchor':'end',fill:'#e8a33d',
        'font-size':11,'font-family':'var(--mono)'});
      t.textContent='θ = '+(D.theta*100).toFixed(0)+'% 压缩阈值';
      svg.appendChild(t);
    }

    // 面积 + 折线
    let d='', area=`M ${X(pts[0].step)} ${PT+ih}`;
    pts.forEach((p,i)=>{ const x=X(p.step), y=Y(p.tokens);
      d += (i?' L ':'M ')+x+' '+y; area += ' L '+x+' '+y; });
    area += ` L ${X(pts[pts.length-1].step)} ${PT+ih} Z`;

    const grad = el('linearGradient',{id:'g1',x1:0,y1:0,x2:0,y2:1});
    grad.appendChild(el('stop',{offset:'0%','stop-color':'#5ec8d8','stop-opacity':.30}));
    grad.appendChild(el('stop',{offset:'100%','stop-color':'#5ec8d8','stop-opacity':.02}));
    const defs = el('defs'); defs.appendChild(grad); svg.appendChild(defs);
    svg.appendChild(el('path',{d:area,fill:'url(#g1)'}));
    svg.appendChild(el('path',{d:d,fill:'none',stroke:'#5ec8d8','stroke-width':2,
      'stroke-linejoin':'round'}));

    // 压缩事件
    (D.compactions||[]).forEach(c=>{
      const x=X(c.step);
      svg.appendChild(el('line',{x1:x,y1:PT,x2:x,y2:PT+ih,stroke:'#e8a33d',
        'stroke-width':1,'stroke-dasharray':'3 3',opacity:.75}));
      svg.appendChild(el('circle',{cx:x,cy:Y(c.after||0)+0,r:3.5,fill:'#e8a33d'}));
      const t=el('text',{x:Math.min(x+7,W-PR-120),y:PT+15,fill:'#e8a33d',
        'font-size':11,'font-family':'var(--mono)'});
      t.textContent = (c.before/1000).toFixed(1)+'k → '+(c.after/1000).toFixed(1)+'k';
      svg.appendChild(t);
    });

    // 验收事件
    (D.verifies||[]).forEach(v=>{
      const x=X(v.step);
      svg.appendChild(el('circle',{cx:x,cy:PT+ih+13,r:4,
        fill: v.passed ? '#6fcf97' : '#e2607a'}));
    });

    // x 轴
    svg.appendChild(el('line',{x1:PL,y1:PT+ih,x2:W-PR,y2:PT+ih,stroke:'#24344a'}));
    const step = Math.max(1, Math.ceil(maxS/12));
    for(let s=1;s<=maxS;s+=step){
      const t=el('text',{x:X(s),y:H-8,'text-anchor':'middle',fill:'#4c6076',
        'font-size':11,'font-family':'var(--mono)'});
      t.textContent=s; svg.appendChild(t);
    }
    host.appendChild(svg);
  }

  function timeline(){
    const host=document.getElementById('timeline');
    (D.timeline||[]).forEach(r=>{
      const d=document.createElement('details');
      d.className='row'; d.setAttribute('data-k', r.kind);
      const s=document.createElement('summary');
      s.innerHTML='<span class="step">'+(r.step||'')+'</span>'+
                  '<span class="tag">'+r.tag+'</span>'+
                  '<span class="desc"></span>'+
                  '<span class="meta">'+(r.meta||'')+'</span>';
      s.querySelector('.desc').textContent = r.desc || '';
      d.appendChild(s);
      const b=document.createElement('div'); b.className='body';
      (r.blocks||[]).forEach(bl=>{
        const l=document.createElement('div'); l.className='lbl'; l.textContent=bl[0];
        const p=document.createElement('pre'); p.textContent=bl[1];
        b.appendChild(l); b.appendChild(p);
      });
      d.appendChild(b); host.appendChild(d);
    });
  }

  waveform(); timeline();
})();
"""


# ---------------------------------------------------------------------------
def _fmt_int(n) -> str:
    try:
        return f"{int(n):,}"
    except (TypeError, ValueError):
        return "0"


def _fmt_ms(ms) -> str:
    try:
        s = int(ms) / 1000
    except (TypeError, ValueError):
        return "-"
    if s < 60:
        return f"{s:.1f}s"
    return f"{int(s // 60)}m{int(s % 60):02d}s"


_STATUS_TEXT = {
    "success": ("验收通过", "v-ok"),
    "answered": ("已回答", "v-ok"),
    "steps_exhausted": ("步数耗尽", "v-warn"),
    "budget_exhausted": ("预算耗尽", "v-warn"),
    "stuck": ("无进展终止", "v-fail"),
    "verify_exhausted": ("验收未通过", "v-fail"),
    "parse_failure": ("解析失败", "v-fail"),
    "api_failure": ("接口失败", "v-fail"),
    "interrupted": ("用户中断", "v-warn"),
    "error": ("内部错误", "v-fail"),
}

_TAG = {
    "assistant": "模型决策",
    "tool": "工具",
    "tool_error": "工具失败",
    "compaction": "上下文压缩",
    "verify": "验收通过",
    "verify_fail": "验收未通过",
    "note": "系统干预",
    "todo": "任务清单",
    "task": "任务",
}


def _build_timeline(data: dict) -> list[dict]:
    rows: list[dict] = []
    step = 0
    verifies = {v.get("step"): v for v in data.get("verifies", [])}
    compactions = {c.get("step"): c for c in data.get("compactions", [])}

    for ev in data.get("events", []):
        kind = ev.get("kind")
        content = ev.get("content") or ""
        if kind == "task":
            rows.append({"kind": "task", "step": "", "tag": _TAG["task"],
                         "desc": content[:160], "meta": "",
                         "blocks": [["任务原文", content]]})
            continue
        if kind == "assistant":
            step += 1
            calls = ev.get("calls") or []
            names = ", ".join(c.get("name", "") for c in calls) or "（无工具调用）"
            blocks = []
            if content.strip():
                blocks.append(["模型输出", content])
            for c in calls:
                blocks.append([f"调用 {c.get('name','')}",
                               json.dumps(c.get("arguments", {}), ensure_ascii=False, indent=2)])
            rows.append({"kind": "assistant", "step": step, "tag": _TAG["assistant"],
                         "desc": names, "meta": f"{ev.get('tokens',0)} tok",
                         "blocks": blocks})
            continue
        if kind == "tool_result":
            ok = bool((ev.get("meta") or {}).get("ok", True))
            code = ev.get("code") or "OK"
            rows.append({
                "kind": "tool" if ok else "tool_error",
                "step": "", "tag": ev.get("tool_name") or "tool",
                "desc": content.strip().split("\n")[0][:180],
                "meta": ("OK" if ok else code) + f" · {ev.get('tokens',0)} tok",
                "blocks": [["结果", content]],
            })
            continue
        if kind == "compaction":
            m = ev.get("meta") or {}
            rows.append({
                "kind": "compaction", "step": "", "tag": _TAG["compaction"],
                "desc": f"{m.get('before_tokens',0)} → {m.get('after_tokens',0)} tokens；"
                        f"丢弃 {m.get('n_reclaimable',0)} 条可重取内容，保留 {m.get('n_anchors',0)} 个锚点",
                "meta": "weak 模型" if m.get("llm_used") else "确定性降级",
                "blocks": [["压缩产物", content]],
            })
            continue
        if kind == "verify":
            passed = bool((ev.get("meta") or {}).get("passed"))
            rows.append({
                "kind": "verify" if passed else "verify_fail", "step": "",
                "tag": _TAG["verify"] if passed else _TAG["verify_fail"],
                "desc": content.strip().split("\n")[0][:180], "meta": "",
                "blocks": [["验收报告", content]],
            })
            continue
        if kind in ("note", "todo"):
            rows.append({"kind": kind, "step": "", "tag": _TAG.get(kind, kind),
                         "desc": content.strip().split("\n")[0][:180], "meta": "",
                         "blocks": [["内容", content]]})
    # 补上未进入事件流的验收/压缩（理论上不会发生，作为兜底）
    for s, v in verifies.items():
        if not any(r["kind"].startswith("verify") for r in rows):
            rows.append({"kind": "verify" if v.get("passed") else "verify_fail",
                         "step": s, "tag": "验收", "desc": v.get("command", ""),
                         "meta": "", "blocks": [["输出", v.get("output", "")]]})
    _ = compactions
    return rows


def build_report(session_dir: Path) -> str:
    """把一次会话的 JSONL 渲染成单文件 HTML。"""
    data = load_session(session_dir)
    meta = data.get("meta") or {}
    end = data.get("end") or {}
    cfg = meta.get("config") or {}
    ctx_cfg = cfg.get("context") or {}

    series = end.get("context_series") or data.get("context") or []
    series = [{"step": s.get("step", i + 1), "tokens": s.get("tokens", 0)}
              for i, s in enumerate(series) if s.get("tokens")]

    comps = []
    for c in data.get("compactions", []):
        # context_before/after 是"完整上下文"口径，与曲线一致；
        # before_tokens/after_tokens 是"历史事件"口径，仅在前者缺失时兜底。
        comps.append({
            "step": c.get("step", 0),
            "before": c.get("context_before") or c.get("before_tokens", 0),
            "after": c.get("context_after") or c.get("after_tokens", 0),
            "dropped": c.get("n_reclaimable", 0),
            "anchors": c.get("n_anchors", 0),
        })

    verifies = [{"step": v.get("step", 0), "passed": bool(v.get("passed"))}
                for v in data.get("verifies", [])]

    budget = cfg.get("context_budget") or 0
    if not budget:
        mw = ((cfg.get("model") or {}).get("context_window")) or 128000
        budget = int(mw * 0.7)
    theta = float(ctx_cfg.get("theta") or 0.6)

    payload = {
        "series": series,
        "compactions": comps,
        "verifies": verifies,
        "budget": budget,
        "threshold": int(budget * theta),
        "theta": theta,
        "timeline": _build_timeline(data),
    }

    status = end.get("status", "unknown")
    label, klass = _STATUS_TEXT.get(status, (status, "v-warn"))
    if status == "success" and not end.get("verified", True):
        label, klass = "完成（未经真实验证）", "v-warn"

    tool_stats = end.get("tool_stats") or {}
    llm_stats = end.get("llm") or {}
    by_role = llm_stats.get("by_role") or {}

    metrics = [
        ("状态", f'<span class="{klass}">{html.escape(label)}</span>'),
        ("步数", _fmt_int(end.get("steps", 0))),
        ("输入 token", _fmt_int(end.get("total_in", 0))),
        ("输出 token", _fmt_int(end.get("total_out", 0))),
        ("成本", f"${float(end.get('cost', 0) or 0):.4f}"),
        ("耗时", _fmt_ms(end.get("wall_ms", 0))),
        ("模型", html.escape(str(meta.get("model", "-"))[:22])),
    ]

    badges = [
        ("上下文压缩", end.get("compactions", 0), "hot"),
        ("文件快照", end.get("snapshots", 0), ""),
        ("指纹拦截", tool_stats.get("stale_blocked", 0), "bad"),
        ("old_str 未命中", tool_stats.get("no_match", 0), "bad"),
        ("old_str 歧义", tool_stats.get("ambiguous", 0), "bad"),
        ("权限拒绝", tool_stats.get("permission_denied", 0), "bad"),
        ("危险命令拦截", tool_stats.get("blocked_command", 0), "bad"),
        ("命令超时", tool_stats.get("timeouts", 0), ""),
        ("验收尝试", len(verifies), "good"),
    ]

    by_tool = tool_stats.get("by_tool") or {}
    max_calls = max(by_tool.values()) if by_tool else 1
    tool_rows = "".join(
        f'<tr><td>{html.escape(k)}</td><td class="n">{v}</td>'
        f'<td style="width:45%"><div class="bar" style="width:{max(2, v * 100 // max_calls)}%"></div></td></tr>'
        for k, v in sorted(by_tool.items(), key=lambda kv: -kv[1])
    ) or '<tr><td colspan="3" style="color:var(--dim)">本次没有工具调用</td></tr>'

    role_rows = "".join(
        f'<tr><td>{html.escape(r)}</td><td class="n">{d.get("calls",0)}</td>'
        f'<td class="n">{_fmt_int(d.get("in",0))}</td><td class="n">{_fmt_int(d.get("out",0))}</td>'
        f'<td class="n">${float(d.get("cost",0) or 0):.4f}</td></tr>'
        for r, d in by_role.items()
    ) or '<tr><td colspan="5" style="color:var(--dim)">无记录</td></tr>'

    rm = meta.get("repomap") or {}
    vp = meta.get("verify_plan") or {}
    facts = [
        ("工作区", str(meta.get("cwd", "-"))),
        ("Provider", f'{meta.get("provider","-")}（main={meta.get("model","-")}，weak={meta.get("weak_model","-")}）'),
        ("验收方式", f'{vp.get("mode","-")}：{vp.get("test") or vp.get("lint") or "—"}'),
        ("L1 仓库索引", f'{rm.get("n_files",0)} 文件 / {rm.get("n_defs",0)} 符号 / {rm.get("n_edges",0)} 条引用边'
                        + ("（tree-sitter）" if rm.get("tree_sitter") else "（正则抽取）")
                        if rm.get("enabled") else f'未启用：{rm.get("reason","-")}'),
        ("L3 命中技能卡", ", ".join(meta.get("skills") or []) or "无"),
        ("消融模式", "裸基线（仅 bash + finish）" if meta.get("baseline_mode") else "完整配置"),
        ("改动文件", ", ".join(end.get("mutated") or []) or "无"),
        ("终止原因", str(end.get("reason", "-"))),
    ]
    fact_rows = "".join(
        f'<tr><td style="color:var(--muted);width:150px">{html.escape(k)}</td>'
        f'<td>{html.escape(str(v))}</td></tr>' for k, v in facts
    )

    task = str(meta.get("task", "(无任务描述)"))
    sid = str(meta.get("session_id") or data.get("session_id") or "")
    payload_json = json.dumps(payload, ensure_ascii=False).replace("</", "<\\/")

    return f"""<!DOCTYPE html>
<html lang="zh-CN"><head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>VISTA 会话报告 · {html.escape(sid)}</title>
<style>{CSS}</style>
</head><body><div class="wrap">

<header class="masthead">
  <div style="flex:1 1 460px;min-width:280px">
    <div class="brand">VISTA <b>SESSION REPORT</b></div>
    <h1>{html.escape(task)}</h1>
    <div class="sid">{html.escape(sid)} · {html.escape(str(meta.get('vista_version','')))}</div>
  </div>
  <div class="verdict {klass}">{html.escape(label)}</div>
</header>

<div class="metrics">
  {''.join(f'<div class="metric"><div class="k">{k}</div><div class="v">{v}</div></div>' for k, v in metrics)}
</div>

<section>
  <h2>Context Pressure</h2>
  <p class="sub">上下文 token 随步数的变化。橙色虚线是压缩触发阈值 θ；每一处纵向虚线是一次
     Anchor Compression，曲线在该处切出的凹口就是被释放的可重取内容。
     底部圆点是 Verify-Gate 的每次验收（绿=通过，红=未通过）。</p>
  <div class="scope"><div id="scope"></div>
    <div class="legend">
      <span><i style="background:#5ec8d8"></i>上下文 token</span>
      <span><i style="background:#e8a33d"></i>压缩事件 / θ 阈值</span>
      <span><i style="background:#6fcf97"></i>验收通过</span>
      <span><i style="background:#e2607a"></i>验收未通过</span>
    </div>
  </div>
</section>

<section>
  <h2>Signals</h2>
  <p class="sub">scaffold 层的拦截与保护动作。指纹拦截次数尤其值得关注——
     它代表有多少次"基于陈旧内容的错误编辑"在发生前就被挡住了。</p>
  <div class="badges">
    {''.join(f'<div class="badge {c}"><div class="n">{n}</div><div class="t">{t}</div></div>'
             for t, n, c in badges)}
  </div>
</section>

<section>
  <h2>Run Facts</h2>
  <table><tbody>{fact_rows}</tbody></table>
</section>

<section>
  <h2>Tool Usage</h2>
  <table><thead><tr><th>工具</th><th style="text-align:right">调用次数</th><th></th></tr></thead>
  <tbody>{tool_rows}</tbody></table>
</section>

<section>
  <h2>Model Routing</h2>
  <p class="sub">main 负责推理与决策；weak 只做信息重组（上下文压缩、技能卡蒸馏），不做决策。</p>
  <table><thead><tr><th>角色</th><th style="text-align:right">调用</th>
  <th style="text-align:right">输入</th><th style="text-align:right">输出</th>
  <th style="text-align:right">成本</th></tr></thead>
  <tbody>{role_rows}</tbody></table>
</section>

<section>
  <h2>Trajectory</h2>
  <p class="sub">点击任意一行可展开完整的参数与结果。</p>
  <div class="timeline" id="timeline"></div>
</section>

<footer>由 VISTA 生成 · 单文件报告，无外部依赖，可离线打开 · 数据源：trajectory.jsonl</footer>
</div>
<script id="vista-data" type="application/json">{payload_json}</script>
<script>{JS}</script>
</body></html>"""


def write_report(session_dir: Path, out: Path | None = None) -> Path:
    path = Path(out) if out else Path(session_dir) / "report.html"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(build_report(session_dir), encoding="utf-8")
    return path
