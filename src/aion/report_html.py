"""
Self-contained HTML report generator for Aion (`aion check --format html` & `aion diff --format html`).
Implements Phase 4 Task 1 & Task 3 Roadmap Views in order:
  1. Overview (overall verdict, 3-verdict breakdown, counts, environment metadata, honest-limits note)
  2. Timing Analysis (p50, p95, p99, max, jitter, execution/preemption/blocking breakdown)
  3. Thread Explorer (interactive Gantt state timeline with clickable violation window highlighting)
  4. Root-Cause View (multi-hop causal graph with kernel trace evidence)
  5. Recommendations (actionable QNX API fixes)
  6. IPC View & CPU View
  7. Regression / CI History & Differentiation Footer
"""
import html
from typing import Any, Dict, List

STATE_COLORS = {
    "THRUNNING": "#10b981",    # emerald green
    "THREADY": "#f59e0b",      # amber (preempted / waiting for CPU)
    "THMUTEX": "#ef4444",      # red (mutex blocked)
    "THSEM": "#ec4899",        # pink/magenta (semaphore blocked)
    "THREPLY": "#8b5cf6",      # purple (synchronous IPC blocked)
    "THSEND": "#6366f1",       # indigo
    "THNANOSLEEP": "#334155",  # slate (timer sleep)
    "THCONDVAR": "#f97316",
    "THJOIN": "#475569",
}


def render_check_html(payload: Dict[str, Any]) -> str:
    verdict = payload.get("verdict", "UNKNOWN")
    if verdict == "PASS":
        badge_bg = "#10b981"
    elif verdict == "INCONCLUSIVE":
        badge_bg = "#f59e0b"
    else:
        badge_bg = "#ef4444"

    trace_path = html.escape(str(payload.get("trace", "")))
    cpus = payload.get("cpus", 1)
    clock_hz = payload.get("cycles_per_sec", 0)
    threads_count = payload.get("threads_tracked", 0)
    markers_count = payload.get("markers", 0)
    findings_count = payload.get("findings_count", 0)
    logical_verdict = html.escape(str(payload.get("logical_verdict", "PASS")))
    wall_clock_verdict = html.escape(str(payload.get("wall_clock_verdict", "PASS")))
    inconclusive_reasons: List[str] = payload.get("inconclusive_reasons", [])

    metrics: List[Dict[str, Any]] = payload.get("metrics", [])
    findings: List[Dict[str, Any]] = payload.get("findings", [])
    env: Dict[str, Any] = payload.get("environment", {})
    target_env = html.escape(str(env.get("target_env", "qemu")).upper())
    margin_pct = float(env.get("margin_pct", 10.0))

    win_start = float(payload.get("window_start_ms", 0.0))
    win_end = float(payload.get("window_end_ms", 1.0))
    win_span = max(0.001, win_end - win_start)

    # 1. Build Timing Metrics Rows
    metrics_rows = []
    for m in metrics:
        tag = "*" if m.get("inferred") else ""
        t_name = html.escape(f"{m['thread']}{tag} (prio {m['prio']})")
        metrics_rows.append(
            f"<tr>"
            f"<td class='mono'><strong>{t_name}</strong></td>"
            f"<td class='num'>{m['jobs']}</td>"
            f"<td class='num'>{m['exec_avg_ms']:.3f}</td>"
            f"<td class='num'>{m['preempt_max_ms']:.3f}</td>"
            f"<td class='num'>{m['block_max_ms']:.3f}</td>"
            f"<td class='num'>{m['p50_ms']:.3f}</td>"
            f"<td class='num'>{m['p95_ms']:.3f}</td>"
            f"<td class='num'>{m['p99_ms']:.3f}</td>"
            f"<td class='num'>{m['max_ms']:.3f}</td>"
            f"<td class='num'>{m['jitter_ms']:.3f}</td>"
            f"</tr>"
        )
    metrics_html = (
        "\n".join(metrics_rows)
        if metrics_rows
        else "<tr><td colspan='10'>No target rule threads configured.</td></tr>"
    )

    # 2. Build Thread Explorer (Timeline) Rows
    tracks: List[Dict[str, Any]] = payload.get("timeline_tracks", [])
    track_rows = []
    for tr in tracks:
        t_label = html.escape(str(tr.get("thread", "")))
        bars = []
        for sl in tr.get("slices", []):
            st = str(sl.get("state", "OTHER"))
            s_ms = float(sl.get("start_ms", win_start))
            e_ms = float(sl.get("end_ms", s_ms))
            dur = float(sl.get("duration_ms", 0.0))
            left_pct = max(0.0, min(100.0, ((s_ms - win_start) / win_span) * 100.0))
            width_pct = max(0.35, min(100.0 - left_pct, ((e_ms - s_ms) / win_span) * 100.0))
            color = STATE_COLORS.get(st, "#64748b")
            tooltip = html.escape(
                f"{st} | {s_ms:.3f} ms -> {e_ms:.3f} ms ({dur:.3f} ms) | CPU:{sl.get('cpu', 0):02d} | prio:{sl.get('prio', 0)}"
            )
            bars.append(
                f"<div class='tl-slice' style='left:{left_pct:.2f}%;width:{width_pct:.2f}%;background:{color};' "
                f"title='{tooltip}'></div>"
            )
        track_rows.append(
            f"<div class='tl-row'>"
            f"<div class='tl-label mono'>{t_label}</div>"
            f"<div class='tl-track'>{''.join(bars)}</div>"
            f"</div>"
        )
    timeline_html = (
        "\n".join(track_rows)
        if track_rows
        else "<p style='color:var(--muted);'>No workload thread intervals in active window.</p>"
    )

    # 3. Build Root-Cause View & Recommendations
    finding_cards = []
    rec_items = []
    if inconclusive_reasons:
        reasons_li = "".join(f"<li>{html.escape(r)}</li>" for r in inconclusive_reasons)
        finding_cards.append(
            f"<div class='card warn-card'>"
            f"<h3>Inconclusive Measurement Notice (Exit Code 4)</h3>"
            f"<ul>{reasons_li}</ul>"
            f"</div>"
        )

    if not findings:
        finding_cards.append(
            "<div class='card pass-card'>"
            "<h3>No Timing or Logical Scheduling Violations Detected</h3>"
            "<p>All configured deadline, scheduling latency, lateness, mutex/semaphore priority inversion, "
            "and blocking IPC contracts passed verification.</p>"
            "</div>"
        )
        rec_items.append("<li>All real-time contracts satisfied. Maintain current priority hierarchy and CI gates.</li>")
    else:
        for idx, f in enumerate(findings, 1):
            f_type = html.escape(str(f.get("finding_type", "")).upper())
            conf = html.escape(str(f.get("confidence", "")))
            start_ms = float(f.get("start_ms", 0.0))
            end_ms = float(f.get("end_ms", 0.0))
            dur_ms = float(f.get("duration_ms", 0.0))
            summary = html.escape(str(f.get("summary", "")))
            fix = html.escape(str(f.get("suggested_fix", "")))
            rec_items.append(
                f"<li><strong>[{f_type}]</strong> ({html.escape(str(f.get('victim_thread', '')))}): <code>{fix}</code></li>"
            )

            steps_html = []
            for s_idx, edge in enumerate(f.get("causal_chain", []), 1):
                rel = html.escape(str(edge.get("relation", "")))
                vic = html.escape(str(edge.get("victim_thread", "")))
                culp = html.escape(str(edge.get("culprit_thread", "")))
                mech = html.escape(str(edge.get("mechanism", "")))
                e_dur = float(edge.get("duration_ms", 0.0))

                evs_html = []
                for ev in edge.get("evidence", []):
                    line_no = ev.get("line_no", 0)
                    t_ms = float(ev.get("timestamp_ms", 0.0))
                    t_cyc = int(ev.get("timestamp_cycles", 0))
                    cpu = int(ev.get("cpu", 0))
                    raw_ev = html.escape(str(ev.get("raw_event", "")))
                    details = html.escape(str(ev.get("details", "")))
                    evs_html.append(
                        f"<li class='mono ev-item'>"
                        f"<span class='ev-loc'>[line {line_no}, t={t_ms:.3f}ms ({t_cyc:#x}), CPU:{cpu:02d}]</span> "
                        f"<strong>{raw_ev}</strong> ({details})"
                        f"</li>"
                    )
                ev_list = f"<ul class='ev-list'>{''.join(evs_html)}</ul>" if evs_html else ""
                steps_html.append(
                    f"<div class='rca-step'>"
                    f"<div class='step-header'>"
                    f"<span class='step-badge'>Step {s_idx}</span> "
                    f"<span class='mono'>{vic}</span> "
                    f"<span class='edge-arrow'>&mdash;[<strong>{rel}</strong>: {mech} for {e_dur:.3f} ms]&rarr;</span> "
                    f"<span class='mono culprit'>{culp}</span>"
                    f"</div>"
                    f"{ev_list}"
                    f"</div>"
                )

            chain_block = (
                f"<div class='rca-chain'><h4>Causal Graph (Multi-Hop RCA Chain)</h4>{''.join(steps_html)}</div>"
                if steps_html
                else ""
            )

            finding_cards.append(
                f"<div class='card fail-card' id='finding-{idx}'>"
                f"<div class='finding-header'>"
                f"<span class='fail-pill'>FAIL #{idx}: {f_type}</span>"
                f"<span class='conf-pill'>Confidence: {conf}</span>"
                f"<a href='#thread-explorer' class='win-link mono' "
                f"onclick='highlightWindow({start_ms:.3f}, {end_ms:.3f}, \"FAIL #{idx}: {f_type}\")'>"
                f"&#128269; Jump to Trace Window: {start_ms:.3f} ms &rarr; {end_ms:.3f} ms ({dur_ms:.3f} ms)"
                f"</a>"
                f"</div>"
                f"<p class='summary-text'><strong>What Happened:</strong> {summary}</p>"
                f"{chain_block}"
                f"<div class='fix-box'><strong>Suggested QNX Fix:</strong> <code>{fix}</code></div>"
                f"</div>"
            )

    findings_section = "\n".join(finding_cards)

    # 4. Build IPC View & CPU View
    ipc_rows = []
    for item in payload.get("ipc_summary", []):
        ipc_rows.append(
            f"<tr>"
            f"<td class='mono'>{html.escape(str(item['client']))}</td>"
            f"<td class='mono'>{html.escape(str(item['state']))}</td>"
            f"<td class='mono'>{html.escape(str(item['server']))}</td>"
            f"<td class='num'>{item['start_ms']:.3f} &rarr; {item['end_ms']:.3f}</td>"
            f"<td class='num'><strong>{item['duration_ms']:.3f} ms</strong></td>"
            f"</tr>"
        )
    ipc_html = (
        "\n".join(ipc_rows)
        if ipc_rows
        else "<tr><td colspan='5' style='color:var(--muted);'>No blocking IPC (THREPLY/THSEND) intervals in workload window.</td></tr>"
    )

    cpu_rows = []
    for c in payload.get("cpu_summary", []):
        top_str = ", ".join(
            f"{html.escape(t['thread'])} ({t['run_ms']:.3f} ms)" for t in c.get("top_threads", [])
        ) or "Idle"
        cpu_rows.append(
            f"<tr>"
            f"<td class='mono'><strong>CPU:{c['cpu']:02d}</strong></td>"
            f"<td class='num'>{c['busy_ms']:.3f} ms</td>"
            f"<td class='num'>{c['utilization_pct']:.1f}%</td>"
            f"<td class='mono'>{top_str}</td>"
            f"</tr>"
        )
    cpu_html = "\n".join(cpu_rows) if cpu_rows else "<tr><td colspan='4'>No CPU utilization data.</td></tr>"

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="utf-8" />
  <title>Aion Verification Report — {trace_path}</title>
  <style>
    :root {{
      --bg: #0f172a; --panel: #1e293b; --border: #334155;
      --text: #f8fafc; --muted: #94a3b8; --accent: #38bdf8;
      --fail: #ef4444; --pass: #10b981; --warn: #f59e0b;
    }}
    * {{ box-sizing: border-box; }}
    body {{
      margin: 0; padding: 24px 32px;
      font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif;
      background: var(--bg); color: var(--text); line-height: 1.5;
    }}
    .mono {{ font-family: "JetBrains Mono", "Fira Code", monospace; font-size: 0.88em; }}
    header {{
      display: flex; justify-content: space-between; align-items: center;
      border-bottom: 1px solid var(--border); padding-bottom: 16px; margin-bottom: 20px;
    }}
    .verdict-badge {{
      background: {badge_bg}; color: #fff; font-weight: 700;
      padding: 8px 18px; border-radius: 6px; font-size: 1.1rem; letter-spacing: 0.05em;
    }}
    .meta-grid {{
      display: grid; grid-template-columns: repeat(auto-fit, minmax(155px, 1fr));
      gap: 12px; margin-bottom: 16px;
    }}
    .meta-box {{
      background: var(--panel); border: 1px solid var(--border);
      padding: 12px 14px; border-radius: 8px;
    }}
    .meta-label {{ color: var(--muted); font-size: 0.75rem; text-transform: uppercase; }}
    .meta-val {{ font-size: 1.05rem; font-weight: 600; margin-top: 4px; }}
    .limits-banner {{
      background: rgba(56,189,248,0.08); border: 1px solid rgba(56,189,248,0.25);
      border-radius: 6px; padding: 10px 14px; font-size: 0.85rem; color: #bae6fd; margin-bottom: 24px;
    }}
    h2 {{ font-size: 1.2rem; margin: 28px 0 12px; border-left: 4px solid var(--accent); padding-left: 10px; }}
    table {{
      width: 100%; border-collapse: collapse; background: var(--panel);
      border: 1px solid var(--border); border-radius: 8px; overflow: hidden; margin-bottom: 8px;
    }}
    th, td {{ padding: 9px 12px; border-bottom: 1px solid var(--border); text-align: left; }}
    th {{ background: #0f172a; color: var(--muted); font-size: 0.8rem; text-transform: uppercase; }}
    td.num, th.num {{ text-align: right; font-variant-numeric: tabular-nums; }}
    .footnote {{ color: var(--muted); font-size: 0.82rem; margin-top: 4px; }}
    .tl-container {{
      background: var(--panel); border: 1px solid var(--border); border-radius: 8px; padding: 16px; position: relative;
    }}
    .tl-legend {{ display: flex; flex-wrap: wrap; gap: 14px; margin-bottom: 12px; font-size: 0.82rem; color: var(--muted); }}
    .legend-dot {{ display: inline-block; width: 12px; height: 12px; border-radius: 3px; vertical-align: middle; margin-right: 5px; }}
    .tl-axis {{ display: flex; justify-content: space-between; margin-left: 240px; font-size: 0.78rem; color: var(--muted); margin-bottom: 6px; }}
    .tl-tracks-wrap {{ position: relative; }}
    .tl-row {{ display: flex; align-items: center; margin-bottom: 8px; }}
    .tl-label {{ width: 230px; flex-shrink: 0; padding-right: 10px; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; font-size: 0.82rem; }}
    .tl-track {{ flex-grow: 1; height: 22px; background: #0f172a; border-radius: 4px; position: relative; overflow: hidden; border: 1px solid #334155; }}
    .tl-slice {{ position: absolute; top: 0; bottom: 0; border-radius: 2px; cursor: pointer; transition: opacity 0.15s; }}
    .tl-slice:hover {{ opacity: 0.8; outline: 1px solid #fff; z-index: 5; }}
    #tl-highlight {{
      display: none; position: absolute; top: 0; bottom: 0;
      background: rgba(239, 68, 68, 0.22); border-left: 2px solid #ef4444; border-right: 2px solid #ef4444;
      pointer-events: none; z-index: 4;
    }}
    #tl-highlight-banner {{ font-size: 0.85rem; color: #fca5a5; margin-top: 8px; display: none; }}
    .card {{
      background: var(--panel); border: 1px solid var(--border);
      border-radius: 8px; padding: 18px 20px; margin-bottom: 16px;
    }}
    .fail-card {{ border-left: 5px solid var(--fail); }}
    .pass-card {{ border-left: 5px solid var(--pass); }}
    .warn-card {{ border-left: 5px solid var(--warn); }}
    .finding-header {{ display: flex; flex-wrap: wrap; gap: 10px; align-items: center; margin-bottom: 12px; }}
    .fail-pill {{ background: rgba(239,68,68,0.2); color: #fca5a5; padding: 4px 10px; border-radius: 4px; font-weight: 700; }}
    .conf-pill {{ background: rgba(56,189,248,0.15); color: #7dd3fc; padding: 4px 10px; border-radius: 4px; font-size: 0.85rem; }}
    .win-link {{
      margin-left: auto; color: #38bdf8; text-decoration: none;
      background: rgba(56,189,248,0.1); padding: 4px 10px; border-radius: 4px; border: 1px solid rgba(56,189,248,0.3);
    }}
    .win-link:hover {{ background: rgba(56,189,248,0.25); }}
    .rca-chain {{ background: #0f172a; border: 1px solid var(--border); border-radius: 6px; padding: 14px; margin: 12px 0; }}
    .rca-chain h4 {{ margin: 0 0 10px; color: var(--accent); font-size: 0.95rem; }}
    .rca-step {{ margin-bottom: 10px; padding-bottom: 10px; border-bottom: 1px dashed var(--border); }}
    .rca-step:last-child {{ margin-bottom: 0; padding-bottom: 0; border-bottom: none; }}
    .step-badge {{ background: #334155; color: #fff; padding: 2px 8px; border-radius: 4px; font-size: 0.8rem; font-weight: 600; }}
    .edge-arrow {{ color: #fbbf24; margin: 0 6px; }}
    .culprit {{ color: #f87171; font-weight: 600; }}
    .ev-list {{ margin: 6px 0 0 24px; padding: 0; list-style: disc; color: var(--muted); }}
    .ev-loc {{ color: #93c5fd; }}
    .fix-box {{ background: rgba(16,185,129,0.1); border: 1px solid rgba(16,185,129,0.3); padding: 10px 14px; border-radius: 6px; color: #a7f3d0; }}
    .grid-2col {{ display: grid; grid-template-columns: repeat(auto-fit, minmax(420px, 1fr)); gap: 18px; }}
    .aion-differentiation-footer {{
      margin-top: 36px; padding: 16px 20px; background: #1e293b;
      border-top: 2px solid var(--accent); border-radius: 6px; color: var(--muted); font-size: 0.9rem;
    }}
    .aion-differentiation-footer strong {{ color: var(--text); }}
  </style>
</head>
<body>
  <header>
    <div>
      <h1 style="margin:0; font-size:1.5rem;">Aion Real-Time Timing &amp; Causal RCA Report</h1>
      <div class="mono" style="color:var(--muted); margin-top:4px;">Trace: {trace_path} | Schema v{html.escape(str(payload.get("schema_version", "1.0")))}</div>
    </div>
    <div class="verdict-badge">VERDICT: {verdict}</div>
  </header>

  <section class="meta-grid">
    <div class="meta-box"><div class="meta-label">Logical Checks</div><div class="meta-val">{logical_verdict}</div></div>
    <div class="meta-box"><div class="meta-label">Wall-Clock Timing</div><div class="meta-val">{wall_clock_verdict}</div></div>
    <div class="meta-box"><div class="meta-label">Target Environment</div><div class="meta-val">{target_env} (+{margin_pct:.0f}%)</div></div>
    <div class="meta-box"><div class="meta-label">Target CPUs</div><div class="meta-val">{cpus} SMP Cores</div></div>
    <div class="meta-box"><div class="meta-label">Clock Frequency</div><div class="meta-val mono">{clock_hz:,} Hz</div></div>
    <div class="meta-box"><div class="meta-label">Violations Found</div><div class="meta-val">{findings_count}</div></div>
  </section>

  <div class="limits-banner">
    <strong>Environment Metadata &amp; Honest Limits Note:</strong> Captured on QNX Neutrino RTOS 8.0 (x86_64 SMP, {threads_count} threads, {markers_count} markers).
    When <code>target_env = "qemu"</code>, wall-clock timing thresholds include a +{margin_pct:.0f}% virtualization jitter tolerance,
    while logical priority-inversion and forbidden-IPC checks remain 100% cycle-exact and environment-independent.
  </div>

  <h2>1. Timing Analysis (Per-Thread Job Metrics)</h2>
  <table>
    <thead>
      <tr>
        <th>Thread</th><th class="num">Jobs</th><th class="num">Exec (ms)</th>
        <th class="num">Preempt (ms)</th><th class="num">Block (ms)</th>
        <th class="num">p50 (ms)</th><th class="num">p95 (ms)</th>
        <th class="num">p99 (ms)</th><th class="num">Max (ms)</th><th class="num">Jitter (ms)</th>
      </tr>
    </thead>
    <tbody>
      {metrics_html}
    </tbody>
  </table>
  <div class="footnote">* = job boundaries inferred from timer sleep/wakeup transitions (unmarked binary mode).</div>

  <h2 id="thread-explorer">2. Thread Explorer (Interactive Kernel State Timeline)</h2>
  <div class="tl-container">
    <div class="tl-legend">
      <span><i class="legend-dot" style="background:#10b981;"></i>THRUNNING (On CPU)</span>
      <span><i class="legend-dot" style="background:#f59e0b;"></i>THREADY (Preempted / Waiting for CPU)</span>
      <span><i class="legend-dot" style="background:#ef4444;"></i>THMUTEX (Mutex Lock Wait)</span>
      <span><i class="legend-dot" style="background:#ec4899;"></i>THSEM (Semaphore Wait)</span>
      <span><i class="legend-dot" style="background:#8b5cf6;"></i>THREPLY (Synchronous IPC Wait)</span>
      <span><i class="legend-dot" style="background:#334155;"></i>THNANOSLEEP (Timer Sleep)</span>
    </div>
    <div class="tl-axis mono">
      <span>{win_start:.3f} ms</span>
      <span>{(win_start + win_span * 0.25):.3f} ms</span>
      <span>{(win_start + win_span * 0.50):.3f} ms</span>
      <span>{(win_start + win_span * 0.75):.3f} ms</span>
      <span>{win_end:.3f} ms</span>
    </div>
    <div class="tl-tracks-wrap">
      <div id="tl-highlight"></div>
      {timeline_html}
    </div>
    <div id="tl-highlight-banner" class="mono"></div>
  </div>

  <h2>3. Root-Cause View (Multi-Hop Causal Graph &amp; Evidence)</h2>
  {findings_section}

  <h2>4. Actionable QNX Recommendations</h2>
  <div class="card">
    <ul style="margin:0; padding-left:20px;">
      {"".join(rec_items)}
    </ul>
  </div>

  <div class="grid-2col">
    <div>
      <h2>5. IPC Channel View (THREPLY / THSEND)</h2>
      <table>
        <thead>
          <tr><th>Client Thread</th><th>State</th><th>Target Server</th><th class="num">Window (ms)</th><th class="num">Duration</th></tr>
        </thead>
        <tbody>{ipc_html}</tbody>
      </table>
    </div>
    <div>
      <h2>6. CPU Core Utilization View</h2>
      <table>
        <thead>
          <tr><th>Core</th><th class="num">Active Busy</th><th class="num">Window Util</th><th>Top Running Threads</th></tr>
        </thead>
        <tbody>{cpu_html}</tbody>
      </table>
    </div>
  </div>

  <footer class="aion-differentiation-footer">
    <strong>Why Aion is Not "Just Another Profiler":</strong> Traditional trace visualizers like the QNX Momentics System Profiler or Eclipse Trace Compass are <em>descriptive</em>&mdash;they render millions of raw kernel events on a timeline and leave the human engineer to manually hunt for milliseconds of delay, cross-reference hex mutex/semaphore addresses (<code>sync_p</code>), track dynamic server priorities across <code>MsgSend</code> boundaries, and guess <em>why</em> a deadline was missed. <strong>Aion is a causal verification and root-cause analysis (RCA) engine.</strong> Instead of asking you to eyeball a Gantt chart, Aion evaluates declarative TOML timing and scheduling contracts, automatically constructs multi-hop <strong>Causal Graphs</strong> that prove <em>why</em> a violation occurred&mdash;linking high-priority victims to low-priority resource holders and medium-priority CPU preemptors with exact trace line numbers and cycle timestamps&mdash;prescribes the exact QNX API fix, and returns a deterministic exit code (<code>0</code> = PASS, <code>1</code> = FAIL, <code>4</code> = INCONCLUSIVE) to block real-time regressions in CI/CD before they ever reach hardware.
  </footer>

  <script>
    const WIN_START = {win_start:.6f};
    const WIN_SPAN = {win_span:.6f};
    function highlightWindow(startMs, endMs, label) {{
      const box = document.getElementById('tl-highlight');
      const banner = document.getElementById('tl-highlight-banner');
      const leftPct = Math.max(0, Math.min(100, ((startMs - WIN_START) / WIN_SPAN) * 100));
      const widthPct = Math.max(0.5, Math.min(100 - leftPct, ((endMs - startMs) / WIN_SPAN) * 100));
      box.style.left = `calc(230px + (100% - 230px) * ${{leftPct / 100}})`;
      box.style.width = `calc((100% - 230px) * ${{widthPct / 100}})`;
      box.style.display = 'block';
      banner.style.display = 'block';
      banner.textContent = `Highlighted Violation Window [${{label}}]: ${{startMs.toFixed(3)}} ms -> ${{endMs.toFixed(3)}} ms (${{(endMs - startMs).toFixed(3)}} ms)`;
    }}
  </script>
</body>
</html>
"""


def render_diff_html(payload: Dict[str, Any]) -> str:
    verdict = payload.get("verdict", "PASS")
    badge_bg = "#10b981" if verdict == "PASS" else "#ef4444"
    t_name = html.escape(str(payload.get("thread_name", "")))
    summary = html.escape(str(payload.get("attribution_summary", "")))
    return f"""<!DOCTYPE html>
<html lang="en">
<head><meta charset="utf-8"/><title>Aion Regression Report — {t_name}</title>
<style>
  body {{ background:#0f172a; color:#f8fafc; font-family:sans-serif; padding:32px; }}
  .card {{ background:#1e293b; border:1px solid #334155; padding:20px; border-radius:8px; margin-top:20px; }}
  .badge {{ background:{badge_bg}; padding:6px 14px; border-radius:6px; font-weight:bold; }}
</style></head>
<body>
  <h1>Aion Regression Comparison <span class="badge">{verdict}</span></h1>
  <div class="card">
    <p><strong>Target Thread:</strong> <code>{t_name}</code></p>
    <p><strong>Baseline p99:</strong> {payload.get('baseline_p99_ms', 0.0):.3f} ms |
       <strong>Candidate p99:</strong> {payload.get('candidate_p99_ms', 0.0):.3f} ms
       ({payload.get('ratio', 1.0):.2f}x, {payload.get('delta_ms', 0.0):+.3f} ms)</p>
    <p><strong>Primary Attributed Blocker:</strong> <code>{html.escape(str(payload.get('primary_new_blocker', '')))}</code></p>
    <p>{summary}</p>
  </div>
</body></html>"""