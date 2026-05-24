from __future__ import annotations
import html
import json
import re
from typing import Any

_DASHBOARD_TPL = """<!doctype html>
<html>
<head>
  <meta charset="utf-8">
  <title>P4 Dashboard</title>
  <style>
    :root {
      --color-running: #5b8def;
      --color-success: #2d8f6f;
      --color-failed: #b05656;
      --color-blocked: #f39c12;
      --flow-llm: #5b8def;
      --flow-llm-bg: #0d1624;
      --flow-tool: #2d8f6f;
      --flow-tool-bg: #0d1a16;
      --flow-system: #d99a2b;
      --flow-system-bg: #1a1510;
      --flow-observation: #8fa3b5;
      --flow-observation-bg: #11161a;
      --flow-frame: #36a6a6;
      --flow-frame-bg: #0d1919;
    }
    html { overflow-anchor: none; }
    body { margin: 0; font-family: ui-sans-serif, system-ui, sans-serif; color: #e8ecef; background: #111417; line-height: 1.5; }
    .shell { max-width: 1120px; margin: 0 auto; padding: 20px; }
    .card { background: #171c20; border: 1px solid #26303a; border-radius: 8px; padding: 16px; margin-bottom: 16px; }
    .headline { font-size: 24px; font-weight: 700; }
    .subtle { color: #92a3b3; font-size: 14px; }
    .pill { display: inline-block; padding: 4px 12px; border-radius: 999px; background: #22303d; font-size: 12px; margin: 4px 4px 0 0; }
    textarea { width: 100%; box-sizing: border-box; min-height: 80px; background: #0f1316; color: #f4f7fa; border: 1px solid #2d3944; border-radius: 8px; padding: 8px; font: inherit; }
    button { border: 0; border-radius: 4px; padding: 8px 16px; background: #2d8f6f; color: white; cursor: pointer; }
    pre { white-space: pre-wrap; font-family: ui-monospace, monospace; font-size: 12px; margin: 0; }

    .operation-card { border: 1px solid #2a3440; background: #12171b; border-radius: 8px; margin-bottom: 8px; overflow: hidden; }
    .operation-head { width: 100%; border: 0; background: transparent; color: inherit; text-align: left; padding: 12px; cursor: pointer; display: flex; justify-content: space-between; align-items: center; }
    .operation-status { font-size: 10px; text-transform: uppercase; padding: 2px 8px; border-radius: 999px; }
    .operation-status.running { background: var(--color-running); }
    .operation-status.success { background: var(--color-success); }
    .operation-status.failed { background: var(--color-failed); }
    .operation-status.blocked { background: var(--color-blocked); color: #111417; }
    .operation-card.blocked { border-color: var(--color-blocked); }
    .blocked-reason { margin: 0 12px 12px 12px; padding: 10px; border-left: 3px solid var(--color-blocked); background: #1a1510; color: #ffd08a; border-radius: 4px; }

    .operation-body.closed, .nested-content.closed { display: none; }
    .nested-block { margin: 8px 12px; border: 1px solid #2a3440; border-radius: 6px; background: #101519; }
    .nested-toggle { width: 100%; border: 0; background: transparent; color: #c6d2dc; text-align: left; padding: 8px; cursor: pointer; font-size: 12px; }
    .operation-output { max-height: 520px; overflow: auto; padding: 8px; background: #080a0c; border-top: 1px solid #2a3440; }
    .operation-output .flow-content { max-height: none; overflow: visible; }
    .result-output { max-height: 360px; overflow: auto; padding: 12px; background: #080a0c; border: 1px solid #2a3440; border-radius: 6px; }
    .bubble-meta { font-size: 11px; color: #92a3b3; padding: 0 12px 8px 12px; }
    .activity-row { padding: 8px; border-bottom: 1px solid #26303a; font-size: 13px; }
    .activity-row:last-child { border-bottom: 0; }

    .tool-pill { display: inline-block; background: #1a232e; padding: 2px 8px; border-radius: 4px; font-size: 11px; margin-right: 4px; border: 1px solid #2a3440; }
    .tool-result-meta { margin-bottom: 8px; }

    /* Flow Steps Styling */
    .flow-step { border-left: 2px solid #2d8f6f; padding-left: 12px; margin: 12px 0; }
    .flow-step-title { font-weight: 700; font-size: 13px; margin-bottom: 4px; display: flex; justify-content: space-between; color: #92a3b3; }
    .flow-phase { font-size: 10px; opacity: 0.7; }
    .flow-item { margin-top: 8px; }
    .flow-label { font-size: 10px; text-transform: uppercase; color: #5b6e7f; margin-bottom: 2px; display: flex; gap: 6px; align-items: center; }
    .depth-badge { display: inline-block; min-width: 34px; padding: 1px 5px; border-radius: 4px; border: 1px solid #2a3440; background: #121a20; color: #9fb4c7; font-size: 10px; text-align: center; }
    .depth-meta { color: #8ca0b1; text-transform: none; }
    .flow-content { background: #0c1013; padding: 8px; border-radius: 4px; border: 1px solid #1f272e; max-height: 420px; overflow: auto; }

    .flow-item.assistant_message .flow-content { border-left: 3px solid var(--flow-llm); background: var(--flow-llm-bg); color: #dcecff; }
    .flow-item.assistant_message .flow-label { color: #9ec5ff; font-weight: bold; }
    .flow-item.finish .flow-content { border-left: 2px solid var(--color-success); background: #0e1513; }
    .flow-item.system_note .flow-content, .flow-item.planning_note .flow-content { border-left: 3px solid var(--flow-system); background: var(--flow-system-bg); font-style: italic; color: #f2d5a1; }
    .flow-item.system_note .flow-label, .flow-item.planning_note .flow-label { color: #e7b65d; font-weight: bold; }
    .flow-item.observer_note .flow-content { background: #101820; border-left: 2px solid #3498db; color: #d9ecff; }
    .flow-item.observer_note .flow-label { color: #7fc8ff; font-weight: bold; }
    .flow-item.llm_output_issue .flow-content { border-left: 3px solid var(--flow-llm); background: var(--flow-llm-bg); color: #dcecff; font-style: normal; }
    .flow-item.llm_output_issue .flow-label { color: #9ec5ff; font-weight: bold; }
    .flow-item.llm_output_issue .flow-k { color: #9ec5ff; font-weight: 700; }
    .flow-content.llm-thinking { border-left: 3px solid var(--flow-llm); background: var(--flow-llm-bg); color: #dcecff; }
    .flow-item.runtime_event .flow-content { border-left: 3px solid var(--flow-observation); background: var(--flow-observation-bg); color: #d8e1e8; }
    .flow-item.runtime_event .flow-label { color: #b6c5d1; font-weight: bold; }
    .flow-item.live_stream .flow-content { border-left: 3px solid var(--flow-llm); background: var(--flow-llm-bg); color: #dcecff; }
    .flow-item.live_stream .flow-label { color: #9ec5ff; font-weight: bold; }
    .flow-item.llm .flow-content { border-left: 3px solid var(--flow-llm); background: var(--flow-llm-bg); color: #dcecff; }
    .flow-item.llm .flow-label, .flow-item.llm .flow-k { color: #9ec5ff; font-weight: bold; }
    .flow-item.tool .flow-content { border-left: 3px solid var(--flow-tool); background: var(--flow-tool-bg); color: #d8f3e6; }
    .flow-item.tool .flow-label, .flow-item.tool .flow-k { color: #78d8ad; font-weight: bold; }
    .flow-item.decision .flow-content { border-left: 3px solid var(--flow-system); background: var(--flow-system-bg); color: #f2d5a1; }
    .flow-item.decision .flow-label, .flow-item.decision .flow-k { color: #e7b65d; font-weight: bold; }
    .flow-item.observation .flow-content { border-left: 3px solid var(--flow-observation); background: var(--flow-observation-bg); color: #d8e1e8; }
    .flow-item.observation .flow-label, .flow-item.observation .flow-k { color: #b6c5d1; font-weight: bold; }
    .flow-item.frame_opened .flow-content, .flow-item.frame .flow-content { border-left: 3px solid var(--flow-frame); background: var(--flow-frame-bg); color: #d8f7f4; }
    .flow-item.frame_opened .flow-label, .flow-item.frame .flow-label { color: #7fd8d8; font-weight: bold; }
    .flow-item.frame_returned .flow-content, .flow-item.child_return .flow-content { border-left: 3px solid var(--flow-frame); background: var(--flow-frame-bg); color: #d8f7f4; }
    .flow-item.frame_returned .flow-label, .flow-item.child_return .flow-label { color: #7fd8d8; font-weight: bold; }

    /* Blocked status styling */
    .flow-item.system_note.blocked .flow-content, .flow-item.decision.blocked .flow-content { border-left: 3px solid var(--color-blocked); background: #1a1510; color: #ffd08a; }
    .flow-item.system_note.blocked .flow-label { color: var(--color-blocked); font-weight: bold; }
    .flow-item.decision.blocked .flow-label { color: var(--color-blocked); font-weight: bold; }

    .tool-stream-block { margin-top: 8px; }
    .flow-k { font-size: 10px; color: #5b6e7f; text-transform: uppercase; margin-bottom: 2px; }
    .tool-stream-block.stderr pre { color: #f58e8e; }
    .live-state { padding: 8px; border-radius: 4px; border: 1px solid #2a3440; margin-bottom: 8px; }
    .live-state.waiting { background: #12171b; border-left: 2px solid #92a3b3; }
    .live-state.running { background: #101519; border-left: 2px solid var(--color-running); }
    .commentator-panel { display: grid; gap: 10px; }
    .commentator-note { border: 1px solid #24445b; border-left: 3px solid #3498db; border-radius: 6px; background: #0f171d; overflow: hidden; }
    .commentator-head { display: flex; gap: 8px; justify-content: space-between; align-items: center; padding: 8px 10px; background: #101e28; color: #d9ecff; font-size: 12px; }
    .commentator-title { font-weight: 700; color: #9bd4ff; }
    .commentator-body { display: grid; gap: 6px; padding: 10px; }
    .commentator-line { display: grid; grid-template-columns: 132px 1fr; gap: 8px; align-items: start; }
    .commentator-line strong { color: #7fc8ff; font-size: 12px; }
    .commentator-line span { color: #e7f4ff; font-size: 13px; }
    .commentator-empty { padding: 10px; color: #92a3b3; border: 1px dashed #2a4050; border-radius: 6px; background: #101519; }
    .path-text { overflow-wrap: anywhere; }
    @media (max-width: 720px) {
      .shell { padding: 12px; }
      .commentator-line { grid-template-columns: 1fr; gap: 2px; }
      .operation-head { align-items: flex-start; gap: 8px; }
    }
  </style>
</head>
<body>
  <div class="shell">
    <section class="card">
      <div class="headline">P4 ダッシュボード</div>
      <div class="subtle">P4 の実行状況、ツール結果、判断の流れを確認できます。</div>
      <div id="pills">
        <div class="pill" id="statusPill">状態: __STATUS__</div>
        <div class="pill" id="modelPill">モデル: __MODEL__</div>
        <div class="pill" id="lastLlmPill">直近LLM: __LAST_LLM__</div>
        <div class="pill" id="workspacePill">作業場: __LLM_WORKSPACE__</div>
        <div class="pill" id="judgePill">judge: __JUDGE_METRICS__</div>
      </div>
    </section>

    
    <section class="card" id="contractProgressCard">
      <h2>Contract Progress</h2>
      <div id="contractProgressBody" style="margin-top:8px; padding:12px; background:#0f1316; border:1px solid #2d3944; border-radius:4px;">
        <div class="commentator-line"><strong>契約状態</strong> <span>__CONTRACT_STATE__</span></div>
        <div class="commentator-line"><strong>必要契約</strong> <span>__CONTRACT_REQUIRED__</span></div>
        <div class="commentator-line"><strong>ファイル作成</strong> <span>__CONTRACT_ARTIFACT__</span></div>
        <div class="commentator-line"><strong>コマンド実行</strong> <span>__CONTRACT_COMMAND__</span></div>
        <div class="commentator-line"><strong>表示出力</strong> <span>__CONTRACT_STDOUT__</span></div>
        <div class="commentator-line"><strong>ユーザ応答</strong> <span>__CONTRACT_RESULT__</span></div>
      </div>
    </section>

    <section class="card" id="latestResultCard">

      <h2>最終結果</h2>
      <div class="subtle" id="latestResultMeta">__LATEST_RESULT_META__</div>
      <pre id="latestResultBody" style="margin-top:8px; padding:12px; background:#0f1316; border:1px solid #2d3944; border-radius:4px; max-height:280px; overflow:auto; white-space:pre-wrap; word-break:break-word;">__LATEST_RESULT__</pre>
    </section>

    <section class="card">
      <h2>メッセージ送信</h2>
      <div style="display: flex; gap: 8px; margin-bottom: 8px;">
        <select id="modeSelect" style="flex: 1; background: #0f1316; color: white; border: 1px solid #2d3944; border-radius: 4px; padding: 4px;">
          <option value="p4_runtime">P4 runtime</option>
          <option value="native_chat">通常チャット</option>
          <option value="terminal_agent">ターミナルエージェント</option>
        </select>
        <select id="modelSelect" style="flex: 1; background: #0f1316; color: white; border: 1px solid #2d3944; border-radius: 4px; padding: 4px;">__MODEL_OPTIONS__</select>
        <select id="shellSelect" style="background: #0f1316; color: white; border: 1px solid #2d3944; border-radius: 4px; padding: 4px;">
           <option value="auto">自動シェル</option>
           <option value="zsh">zsh</option>
           <option value="bash">bash</option>
        </select>
      </div>
      <textarea id="messageText" placeholder="メッセージを入力..."></textarea>
      <button id="sendButton" type="button" onclick="sendMessage()" style="margin-top: 8px; width: 100%;">送信</button>
      <div id="chatResult" class="subtle" style="margin-top: 4px;"></div>
    </section>

    <section class="card">
      <h2 id="operationsSummary">実行操作 (__OP_COUNT__)</h2>
      <div id="operationsPanel">__OP_ROWS__</div>
    </section>
  </div>

  <script>
    let latestSnapshot = null;
    const openOperationIds = new Set();
    const closedOperationIds = new Set();
    const openNestedIds = new Set();
    const closedNestedIds = new Set();
    let suspendOperationsRenderUntil = 0;

    function esc(v) {
      return String(v ?? "").replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;").replace(/"/g, "&quot;");
    }

    function shortText(v, max = 900) {
      const text = String(v ?? "");
      return text.length > max ? text.slice(0, max) + `\n... (${text.length - max} chars hidden)` : text;
    }

    function syncModelSelect(models, selectedModel) {
      const select = document.getElementById("modelSelect");
      if (!select) return;
      const names = Array.isArray(models) ? models.filter(Boolean).map(String) : [];
      const current = select.value || selectedModel || "";
      const desired = names.includes(current) ? current : (names.includes(selectedModel) ? selectedModel : (names[0] || current));
      const existing = Array.from(select.options).map((option) => option.value);
      if (existing.length === names.length && existing.every((value, index) => value === names[index])) {
        if (desired) select.value = desired;
        return;
      }
      select.innerHTML = "";
      names.forEach((name) => {
        const option = document.createElement("option");
        option.value = name;
        option.textContent = name;
        select.appendChild(option);
      });
      if (desired) select.value = desired;
    }

    function shortPath(v) {
      const text = String(v ?? "");
      return text.replace(new RegExp("^/private/tmp/"), "/tmp/");
    }

    function latestResultText(snapshot) {
      const latest = snapshot.latest_result || {};
      if (latest.body) return latest.body;
      if (latest.summary) return latest.summary;
      return "直近結果はありません。";
    }

    function latestResultSummary(snapshot) {
      const latest = snapshot.latest_result || {};
      const session = snapshot.session || {};
      const summary = String(latest.summary || session.last_assistant_message || "").trim();
      return summary;
    }

    function contractRequiredText(progress) {
      const required = Array.isArray(progress?.required_contract) ? progress.required_contract : [];
      return required.length ? required.join(", ") : "未確定";
    }

    function contractMetricText(progress, key) {
      const required = Array.isArray(progress?.required_contract) ? progress.required_contract : [];
      const raw = String(progress?.[key] || "no");
      if (required.length && !required.includes(key)) return "not required";
      if (required.includes(key)) return raw === "yes" ? "satisfied" : "missing";
      return raw;
    }

    function toggleOperation(opId) {
      const card = document.querySelector(`.operation-card[data-operation-id="${opId}"]`);
      const body = card?.querySelector(".operation-body");
      if (!body) return;
      const closing = !body.classList.contains("closed");
      body.classList.toggle("closed", closing);
      if (closing) { openOperationIds.delete(opId); closedOperationIds.add(opId); }
      else { openOperationIds.add(opId); closedOperationIds.delete(opId); }
      suspendOperationsRenderUntil = Date.now() + 2000;
    }

    function toggleNested(nId) {
      const section = document.querySelector(`[data-nested-id="${nId}"]`);
      if (!section) return;
      const closing = !section.classList.contains("closed");
      section.classList.toggle("closed", closing);
      if (closing) { openNestedIds.delete(nId); closedNestedIds.add(nId); }
      else { openNestedIds.add(nId); closedNestedIds.delete(nId); }
      suspendOperationsRenderUntil = Date.now() + 2000;
    }

    document.addEventListener("click", (event) => {
      const button = event.target.closest("button.nested-toggle");
      if (!button || button.hasAttribute("onclick")) return;
      const content = button.nextElementSibling;
      if (!content) return;
      content.classList.toggle("closed");
    });

    function renderFlowSteps(childTasks, opId = "") {
      if (!childTasks || childTasks.length === 0) return '<div class="subtle" style="padding:8px;">フローはまだありません。</div>';
      
      function taskSummary(task) {
        const lines = [];
        for (const step of task.steps || []) {
          for (const item of step.items || []) {
            if (item.hidden) continue;
            const d = item.details || {};
            if (item.card_type === "llm" && d.tool_name) {
              if (Array.isArray(d.causal_notes) && d.causal_notes.length) {
                const note = d.causal_notes[d.causal_notes.length - 1] || {};
                lines.push(`P4判定: ${note.failure_type || note.title || "repaired"} / ${shortText(note.summary || "", 100)}`);
              }
              const includeAssistantSummary = !(Array.isArray(d.causal_notes) && d.causal_notes.length);
              lines.push(`LLM提案: ${d.tool_name}${includeAssistantSummary && d.assistant_message ? ` / ${shortText(d.assistant_message, 120)}` : ""}`);
            } else if (item.card_type === "finish" && d.acceptance) {
              lines.push(`完了判定: ${d.acceptance.message || d.acceptance.reason_code || "accepted"}`);
            } else if (item.label === "decision" && item.content) {
              lines.push(`最終応答: ${shortText(item.content, 140)}`);
            } else if (item.label === "observation" && item.content && lines.length === 0) {
              lines.push(shortText(item.content, 160));
            }
          }
        }
        const priority = lines.filter((line) => line.startsWith("P4判定:"));
        const tail = lines.slice(-3);
        const merged = [];
        for (const line of [...priority, ...tail]) {
          if (line && !merged.includes(line)) merged.push(line);
        }
        return merged.slice(0, 4).join("\\n") || `${task.status || "unknown"}`;
      }

      return childTasks.map((task, tIndex) => {
        const taskNum = tIndex + 1;
        const icon = task.status === "finished" ? "✓" : (task.status === "finished_with_warnings" || task.status === "blocked" || task.status === "failed" ? "⚠" : "●");
        const isClosed = (task.status === "finished" || task.status === "finished_with_warnings") ? "closed" : "";
        const taskId = `${opId}:task_${taskNum}`;
        const summary = taskSummary(task);
        
        let lastLlmInputSignature = "";
        const stepsHtml = (task.steps || []).map(step => {
          const itemsHtml = (step.items || []).filter((item) => !item.hidden).map((item, itemIndex) => {
            let renderItem = item;
            if (item.card_type === "llm" && item.details) {
              const d = item.details || {};
              const signature = JSON.stringify({
                role: d.role || "",
                transport: d.transport || "",
                schema_required: d.schema_required ?? "",
                model: d.model || ""
              });
              renderItem = {...item, details: {...d, p4_input_repeated: Boolean(lastLlmInputSignature && lastLlmInputSignature === signature)}};
              lastLlmInputSignature = signature;
            }
            return renderFlowItem(renderItem, `${opId}:${step.step_index || 0}:${itemIndex}`);
          }).join('');
          return `
          <section class="flow-step">
            <div class="flow-step-title">
              <span>Step ${step.step_index || 0}</span>
              <span class="flow-phase">${esc(step.phase || '-')}</span>
            </div>
            ${itemsHtml}
          </section>
        `}).join('');

        return `
        <div class="nested-block">
            <button class="nested-toggle" type="button" onclick="toggleNested('${taskId}')">
                ${icon} 子タスク ${taskNum}: ${esc(task.title || '')}
            </button>
            <pre class="subtle" style="padding:0 8px 8px 8px; white-space:pre-wrap;">${esc(summary)}</pre>
            <div class="nested-content ${openNestedIds.has(taskId) ? '' : isClosed}" data-nested-id="${taskId}">
                <div class="operation-output">
                    ${stepsHtml}
                </div>
            </div>
        </div>`;
      }).join('');
    }

    function withFlowScrollId(content, scrollId) {
      if (!scrollId) return content;
      return content.replace('<div class="flow-content"', `<div class="flow-content" data-flow-scroll-id="${esc(scrollId)}"`);
    }

    function renderJudgeDetails(payload, title) {
      if (!payload) return "";
      const d = payload.details || payload;
      const attempts = Array.isArray(d.attempts) ? d.attempts : [];
      const parsed = d.parsed && typeof d.parsed === "object" ? d.parsed : {};
      const summary = [];
      if (d.prompt) summary.push(`<div class="flow-k">P4 → judge LLM input</div><details><summary style="cursor:pointer; color:#9ec5ff;">judge prompt</summary><pre>${esc(d.prompt)}</pre></details>`);
      if (d.final_answer) summary.push(`<div class="flow-k">judge input final_answer</div><pre>${esc(d.final_answer)}</pre>`);
      if (d.evidence_text) summary.push(`<div class="flow-k">judge input evidence</div><pre>${esc(d.evidence_text)}</pre>`);
      summary.push(`<div class="flow-k">judge LLM → P4 output</div>`);
      if (payload.reason_code) summary.push(`<div class="flow-k">reason_code</div><pre>${esc(payload.reason_code)}</pre>`);
      if (payload.message) summary.push(`<div class="flow-k">message</div><pre>${esc(payload.message)}</pre>`);
      if (d.decision) summary.push(`<div class="flow-k">judge decision</div><pre>${esc(d.decision)}</pre>`);
      if (d.response_model || d.model) summary.push(`<div class="flow-k">judge model</div><pre>${esc(d.response_model || d.model)}</pre>`);
      if (parsed.verdict || parsed.status || d.verdict || d.status) summary.push(`<div class="flow-k">judge LLM answer</div><pre>${esc(parsed.verdict || parsed.status || d.verdict || d.status)}</pre>`);
      if (parsed.reason_code || d.reason_code) summary.push(`<div class="flow-k">judge reason</div><pre>${esc(parsed.reason_code || d.reason_code)}</pre>`);
      if (parsed.rationale || d.rationale) summary.push(`<div class="flow-k">judge rationale</div><pre>${esc(parsed.rationale || d.rationale)}</pre>`);
      if (parsed.observed_mismatch) summary.push(`<div class="flow-k">observed mismatch</div><pre>${esc(parsed.observed_mismatch)}</pre>`);
      if (Array.isArray(parsed.unsupported_claims) && parsed.unsupported_claims.length) summary.push(`<div class="flow-k">unsupported claims</div><pre>${esc(parsed.unsupported_claims.join("\\n"))}</pre>`);
      if (attempts.length) {
        const lines = attempts.map((a) => {
          const suffix = a.error ? `: ${a.error}` : (a.decision ? `: ${a.decision}` : "");
          return `attempt ${a.attempt || "-"}${suffix}`;
        }).join("\\n");
        summary.push(`<div class="flow-k">judge attempts</div><pre class="stderr">${esc(lines)}</pre>`);
      }
      const raw = JSON.stringify(payload, null, 2);
      return `<div class="judge-detail">
        <div class="flow-k">${esc(title)}</div>
        ${summary.join("")}
        <details style="margin-top:8px;"><summary style="cursor:pointer; color:#9ec5ff;">judge raw details</summary><pre>${esc(raw)}</pre></details>
      </div>`;
    }

    function renderBlockedDecisionDetails(item, details) {
      const inner = details && details.details ? details.details : {};
      const title = item.human_title || details.human_title || "ブロック";
      const desc = item.human_desc || details.human_desc || "";
      const reason = item.reason_code || details.reason_code || "";
      const code = item.code || details.decision_type || "";
      const parts = [`<div class="blocked-reason" style="margin:0 0 10px 0;"><strong>${esc(title)}</strong>`];
      if (desc) parts.push(`<pre>${esc(desc)}</pre>`);
      if (code || reason) {
        parts.push(`<div class="flow-k">runtime判定</div><pre>${esc([code, reason].filter(Boolean).join(" / "))}</pre>`);
      }
      if (inner.blocked_tool) {
        parts.push(`<div class="flow-k">拒否されたLLM提案</div><pre>${esc(inner.blocked_tool)}</pre>`);
      }
      if (Array.isArray(inner.issues) && inner.issues.length) {
        parts.push(`<div class="flow-k">具体的な不足・違反</div><pre>${esc(inner.issues.map((issue) => `- ${issue}`).join("\\n"))}</pre>`);
      }
      if (Array.isArray(inner.required_contract) && inner.required_contract.length) {
        parts.push(`<div class="flow-k">必要な契約項目</div><pre>${esc(inner.required_contract.map((value) => `- ${value}`).join("\\n"))}</pre>`);
      }
      if (inner.suggested_fix) {
        parts.push(`<div class="flow-k">次に直すべきこと</div><pre>${esc(inner.suggested_fix)}</pre>`);
      }
      if (Array.isArray(inner.allowed_next_actions) && inner.allowed_next_actions.length) {
        parts.push(`<details style="margin-top:8px;"><summary style="cursor:pointer; color:#9ec5ff;">許可される次アクション</summary><pre>${esc(JSON.stringify(inner.allowed_next_actions, null, 2))}</pre></details>`);
      }
      parts.push(`</div>`);
      return parts.join("");
    }

    function renderLlmCausalNotes(notes) {
      if (!Array.isArray(notes) || !notes.length) return "";
      const rows = notes.map((note) => {
        const bits = [];
        if (note.failure_type) bits.push(`<div class="flow-k">failure_type</div><pre>${esc(note.failure_type)}</pre>`);
        if (note.summary) bits.push(`<div class="flow-k">what happened</div><pre>${esc(note.summary)}</pre>`);
        if (note.next) bits.push(`<div class="flow-k">next</div><pre>${esc(note.next)}</pre>`);
        return `<section class="nested-block" style="margin:8px 0;"><button class="nested-toggle" type="button">${esc(note.title || "LLM retry context")}</button><div class="nested-content" style="padding:8px;">${bits.join("")}</div></section>`;
      });
      return `<div class="flow-k">P4判定と再要求の経緯</div>${rows.join("")}`;
    }

    function renderRejectedLlmAttempts(attempts) {
      if (!Array.isArray(attempts) || !attempts.length) return "";
      const rows = attempts.map((attempt) => {
        const bits = [];
        if (attempt.model) bits.push(`<div class="flow-k">model</div><pre>${esc(attempt.model)}</pre>`);
        if (attempt.content_text) bits.push(`<div class="flow-k">content</div><pre>${esc(attempt.content_text)}</pre>`);
        return `<section class="nested-block" style="margin:8px 0;"><button class="nested-toggle" type="button">${esc(attempt.title || "LLM → P4 Output (rejected)")}</button><div class="nested-content" style="padding:8px;">${bits.join("")}</div></section>`;
      });
      return rows.join("");
    }

    function renderToolExecutionResult(details, scrollId = "") {
      const toolName = String(details?.tool_name || "");
      const result = details?.tool_result || {};
      const parts = [];
      if (toolName) parts.push(`<div><span class="tool-pill">${esc(toolName)}</span>${result.ok !== undefined ? ` <span class="tool-pill">ok: ${esc(result.ok)}</span>` : ""}</div>`);
      if (toolName === "list_files" && Array.isArray(result.items)) {
        const items = result.items.length ? result.items.map((value) => `- ${value}`).join("\\n") : "(empty)";
        parts.push(`<div class="flow-k">list_files result</div><pre>${esc(items)}</pre>`);
      } else if (toolName === "run_command") {
        parts.push(renderCommandResult(result));
      } else if (toolName === "write_file" || toolName === "append_file" || toolName === "replace_text") {
        const fileBits = [];
        if (result.path) fileBits.push(`path: ${result.path}`);
        if (result.bytes_written !== undefined) fileBits.push(`bytes_written: ${result.bytes_written}`);
        if (result.bytes_appended !== undefined) fileBits.push(`bytes_appended: ${result.bytes_appended}`);
        parts.push(`<div class="flow-k">file result</div><pre>${esc(fileBits.join("\\n") || JSON.stringify(result, null, 2))}</pre>`);
      } else if (Object.keys(result).length) {
        parts.push(`<div class="flow-k">result</div><pre>${esc(JSON.stringify(result, null, 2))}</pre>`);
      }
      return `<section class="nested-block" style="margin:10px 0 0 0;"><button class="nested-toggle" type="button">TOOL実行結果</button><div class="nested-content" style="padding:8px;">${parts.join("")}</div></section>`;
    }

    function renderCommandResult(p) {
      const meta = [];
      if (p.command) meta.push(`<span class="tool-pill path-text"><strong>cmd:</strong> ${esc(p.command)}</span>`);
      if (p.returncode !== undefined) meta.push(`<span class="tool-pill"><strong>ret:</strong> ${esc(p.returncode)}</span>`);
      if (p.cwd) meta.push(`<span class="tool-pill path-text"><strong>cwd:</strong> ${esc(shortPath(p.cwd))}</span>`);
      const parts = [`<div class="tool-result-card">${meta.length ? `<div class="tool-result-meta">${meta.join("")}</div>` : ""}`];
      if (p.stdout) parts.push(`<div class="flow-k">stdout</div><pre>${esc(shortText(p.stdout))}</pre>`);
      if (p.stderr) parts.push(`<div class="flow-k">stderr</div><pre class="stderr">${esc(shortText(p.stderr))}</pre>`);
      if (p.error) parts.push(`<div class="flow-k">error</div><pre class="stderr">${esc(shortText(p.error))}</pre>`);
      parts.push(`</div>`);
      return parts.join("");
    }

    function renderFlowItem(item, scrollId = "") {
      if (item.hidden) return "";
      const labels = { observer_note: '解説者', system_note: 'システム', planning_note: '計画', task_plan: '子タスク計画', problem_profile: '問題プロファイル', planner_decision: 'プランナー判定', plan_record: '計画契約', plan_revision: '計画改訂', activity_update: 'システム状態', runtime_event: '実行イベント', assistant_message: 'LLM応答', user_message: 'ユーザー', tool_call: 'ツール呼び出し', tool_result: 'ツール結果', finish: '完了', frame_opened: 'フレーム開始', frame_returned: 'フレーム帰還', child_return: '子フレーム結果', live_stream: 'LLMライブ', llm: 'LLM', tool: 'ツール', frame: 'フレーム', decision: '判定', observation: '観測' };
      const label = esc(labels[item.label] || item.label || "");
      let content = esc(item.content || "");
      const depth = Number(item.frame_depth || 0);
      const indent = Math.min(depth, 8) * 18;
      const depthBadge = `<span class="depth-badge">D${esc(depth)}</span>`;
      const depthMeta = `<span class="depth-meta">階層深度: ${esc(depth)} / インデント: ${esc(indent)}px</span>`;

      // Highlight blocked states
      const isBlocked = (item.label === 'system_note' && content.includes('ブロックされました')) || (item.label === 'decision' && item.status === 'blocked');
      const extraClass = isBlocked ? 'blocked' : '';

      if (item.label === 'consolidated_card') {
        const d = item.details || {};
        const parts = [];
        let extraCls = '';
        let lbl = '';
        
        if (item.card_type === 'llm') {
            const inputBits = [d.p4_input_repeated ? 'same machine-control contract as previous Step' : 'machine-control action request'];
            if (d.role) inputBits.push(`role: ${d.role}`);
            if (d.transport) inputBits.push(`transport: ${d.transport}`);
            if (d.schema_required !== undefined) inputBits.push(`schema_required: ${d.schema_required}`);
            if (d.attempt_count) inputBits.push(`attempt: ${d.attempt_count}`);
            if (d.model) inputBits.push(`model: ${d.model}`);
            parts.push(`<div class="flow-k">P4 → Agent LLM Input</div><pre>${esc(inputBits.join(' / '))}</pre>`);
            if (d.model_reason || d.prompt || d.prompt_preview) {
                parts.push(`<details style="margin-top:8px;"><summary style="cursor:pointer; color:#9ec5ff;">P4 input details</summary>`);
                if (d.model_reason) parts.push(`<div class="flow-k" style="margin-top:8px;">model reason</div><pre>${esc(d.model_reason)}</pre>`);
                if (d.prompt) parts.push(`<div class="flow-k" style="margin-top:8px;">full prompt</div><pre>${esc(d.prompt)}</pre>`);
                else if (d.prompt_preview) parts.push(`<div class="flow-k" style="margin-top:8px;">prompt preview</div><pre>${esc(d.prompt_preview)}</pre>`);
                parts.push(`</details>`);
            }
            if (d.rejected_attempts) parts.push(renderRejectedLlmAttempts(d.rejected_attempts));
            if (d.causal_notes) parts.push(renderLlmCausalNotes(d.causal_notes));
            parts.push(`<section class="nested-block" style="margin:10px 0 0 0;"><button class="nested-toggle" type="button">LLM → P4 Output</button><div class="nested-content" style="padding:8px;">`);
            if (d.analysis) parts.push(`<div class="flow-k">analysis</div><pre>${esc(d.analysis)}</pre>`);
            if (d.assistant_message) parts.push(`<div class="flow-k">assistant_message</div><pre>${esc(d.assistant_message)}</pre>`);
            if (d.tool_name) parts.push(`<div class="flow-k">proposed action</div><div><span class="tool-pill">${esc(d.tool_name)}</span></div>`);
            if (d.tool_args) parts.push(`<div class="flow-k">tool_args</div><pre>${esc(JSON.stringify(d.tool_args, null, 2))}</pre>`);
            if (d.executed_tool) parts.push(renderToolExecutionResult(d.executed_tool, scrollId));
            
            parts.push(`<details style="margin-top:8px;"><summary style="cursor:pointer; color:#9ec5ff;">詳細・JSON表示</summary>`);
            if (d.thinking_text) parts.push(`<div class="flow-k" style="margin-top:8px;">thinking</div><pre>${esc(d.thinking_text)}</pre>`);
            if (d.final_text) parts.push(`<div class="flow-k" style="margin-top:8px;">raw json</div><pre>${esc(d.final_text)}</pre>`);
            parts.push(`</details>`);
            parts.push(`</div></section>`);
            
            lbl = "Agent LLM output";
            extraCls = "llm";
        } else if (item.card_type === 'tool') {
            if (d.tool_name) parts.push(`<div class="flow-k">tool</div><pre>${esc(d.tool_name)}</pre>`);
            if (d.tool_args) parts.push(`<div class="flow-k">args</div><pre>${esc(JSON.stringify(d.tool_args, null, 2))}</pre>`);
            if (d.tool_result) {
                if (d.tool_name === 'run_command') {
                    parts.push(renderCommandResult(d.tool_result));
                } else {
                    parts.push(`<div class="flow-k">result</div><pre>${esc(JSON.stringify(d.tool_result, null, 2))}</pre>`);
                }
            }
            lbl = "Tool (consolidated)";
            extraCls = "tool";
        } else if (item.card_type === 'finish') {
            const blockedJudge = d.blocked && d.blocked.details && d.blocked.details.judge ? d.blocked.details.judge : null;
            if (d.grounding_judge) {
                parts.push(renderJudgeDetails(d.grounding_judge, "grounding judge details"));
            } else if (blockedJudge) {
                parts.push(renderJudgeDetails(blockedJudge, "blocked judge details"));
            }
            if (d.blocked) {
                const b = d.blocked;
                parts.push(`<div class="flow-k">P4 completion decision</div>`);
                parts.push(`<div style="color:#ffd08a; margin-bottom:8px;">⚠ <strong>${esc(b.human_title || b.code)}</strong><br/>${esc(b.human_desc || '')}</div>`);
                if (b.reason_code) parts.push(`<div class="flow-k">blocked reason_code</div><pre>${esc(b.reason_code)}</pre>`);
                parts.push(`<pre>${esc(b.content || '')}</pre>`);
            }
            if (d.acceptance) parts.push(`<div class="flow-k">acceptance</div><pre>${esc(JSON.stringify(d.acceptance, null, 2))}</pre>`);
            if (d.controller_finish) parts.push(`<div class="flow-k">controller_finish</div><pre>${esc(JSON.stringify(d.controller_finish, null, 2))}</pre>`);
            lbl = "P4 completion check";
            extraCls = "decision";
        } else {
            parts.push(`<pre>${esc(item.content || '')}</pre>`);
            lbl = "Generic";
        }
        
        return `<div class="flow-item ${extraCls}" style="margin-left:${indent}px">
            <div class="flow-label">${depthBadge}${depthMeta}<span>${lbl}</span></div>
            ${withFlowScrollId('<div class="flow-content">' + parts.join('') + '</div>', scrollId)}
        </div>`;
      }


      if (item.label === 'tool_result' && item.tool_name === 'run_command' && item.parsed_payload) {
        const p = item.parsed_payload;
        content = `<div class="tool-result-card">
          <div class="tool-result-meta">
            ${p.command ? `<span class="tool-pill"><strong>cmd:</strong> ${esc(p.command)}</span>` : ''}
            ${p.returncode !== undefined ? `<span class="tool-pill"><strong>ret:</strong> ${esc(p.returncode)}</span>` : ''}
            ${p.cwd ? `<span class="tool-pill path-text"><strong>cwd:</strong> ${esc(shortPath(p.cwd))}</span>` : ''}
          </div>
          ${p.stdout ? `<div class="flow-label">stdout</div><pre>${esc(shortText(p.stdout))}</pre>` : ''}
          ${p.stderr ? `<div class="flow-label">stderr</div><pre class="stderr">${esc(shortText(p.stderr))}</pre>` : ''}
          ${p.error ? `<div class="flow-label">error</div><pre class="stderr">${esc(shortText(p.error))}</pre>` : ''}
        </div>`;
      } else if (item.label === 'observer_note') {
        content = renderCommentatorContent(item.content || "");
      } else if (item.label === 'system_note' && item.code === 'llm_output_issue' && item.details) {
        content = renderLlmOutputIssue(item);
      } else if (item.label === 'system_note' && item.code === 'llm_output_recovered' && item.details) {
        content = renderLlmOutputRecovered(item);
      } else if (item.label === 'task_plan') {
        content = renderTaskPlan(item);
      } else if (item.label === 'problem_profile' || item.label === 'planner_decision' || item.label === 'plan_record' || item.label === 'plan_revision') {
        content = renderPlannerEvent(item);
      } else if (item.label === 'runtime_event') {
        content = renderRuntimeEvent(item);
      } else if (item.label === 'llm' || item.label === 'tool' || item.label === 'decision' || item.label === 'observation') {
        content = renderCanonicalEvent(item);
      } else if (item.label === 'frame') {
        content = renderCanonicalFrame(item);
      } else if (item.label === 'live_stream') {
        content = `<div class="flow-content">${renderLiveOutput(item.content || "")}</div>`;
      } else if (item.label === 'frame_opened' || item.label === 'frame_returned' || item.label === 'child_return') {
        content = renderFrameFlowContent(item);
      } else {
        content = `<div class="flow-content"><pre>${esc(item.content || "")}</pre></div>`;
      }
      content = withFlowScrollId(content, scrollId);
      const itemClass = item.code === 'llm_output_issue' ? 'llm_output_issue'
        : item.code === 'llm_output_recovered' ? 'llm_output_recovered'
        : esc(item.label);
      return `<div class="flow-item ${itemClass} ${extraClass}" style="margin-left:${indent}px">
        <div class="flow-label">${depthBadge}${depthMeta}<span>${label}${isBlocked ? ' (BLOCKED)' : ''}</span></div>
        ${content}
      </div>`;
    }

    function renderCanonicalEvent(item) {
      const d = item.details || {};
      const rows = [`<div class="flow-k">status</div><pre>${esc(item.status || "")}</pre>`];
      if (item.label === 'llm') {
        if (d.event_name) rows.push(`<div class="flow-k">event</div><pre>${esc(d.event_name)}</pre>`);
        if (d.model) rows.push(`<div class="flow-k">model</div><pre>${esc(d.model)}</pre>`);
        if (d.parse_issue) rows.push(`<div class="flow-k">parse issue</div><pre>${esc(d.parse_issue)}</pre>`);
        if (d.thinking_text) rows.push(`<div class="flow-k">thinking</div><pre>${esc(d.thinking_text)}</pre>`);
        if (d.content_text) rows.push(`<div class="flow-k">content</div><pre>${esc(d.content_text)}</pre>`);
        if (!d.thinking_text && !d.content_text && item.content) rows.push(`<pre>${esc(item.content)}</pre>`);
      } else if (item.label === 'tool') {
        if (item.tool_name) rows.push(`<div class="flow-k">tool</div><pre>${esc(item.tool_name)}</pre>`);
        if (item.tool_args) rows.push(`<div class="flow-k">tool args</div><pre>${esc(JSON.stringify(item.tool_args, null, 2))}</pre>`);
        const toolResult = d.result || d.tool_result || item.parsed_payload;
        if (toolResult) rows.push(`<div class="flow-k">tool result</div><pre>${esc(JSON.stringify(toolResult, null, 2))}</pre>`);
        if (!toolResult && item.content) rows.push(`<pre>${esc(item.content)}</pre>`);
      } else if (item.label === 'decision') {
        if (item.status === 'blocked') rows.push(renderBlockedDecisionDetails(item, d));
        if (item.code) rows.push(`<div class="flow-k">decision</div><pre>${esc(item.code)}</pre>`);
        if (item.reason_code) rows.push(`<div class="flow-k">reason</div><pre>${esc(item.reason_code)}</pre>`);
        if (d.rationale) rows.push(`<div class="flow-k">rationale</div><pre>${esc(d.rationale)}</pre>`);
        if (d.tasks) rows.push(`<div class="flow-k">tasks</div><pre>${esc(JSON.stringify(d.tasks, null, 2))}</pre>`);
        if (d.strategy) rows.push(`<div class="flow-k">strategy</div><pre>${esc(d.strategy)}</pre>`);
        if (d.profile) rows.push(`<div class="flow-k">problem profile</div><pre>${esc(JSON.stringify(d.profile, null, 2))}</pre>`);
        if (d.plan) rows.push(`<div class="flow-k">plan record</div><pre>${esc(JSON.stringify(d.plan, null, 2))}</pre>`);
        if (d.work_units) rows.push(`<div class="flow-k">work units</div><pre>${esc(JSON.stringify(d.work_units, null, 2))}</pre>`);
        if (d.verification_contract) rows.push(`<div class="flow-k">verification contract</div><pre>${esc(JSON.stringify(d.verification_contract, null, 2))}</pre>`);
        rows.push(`<div class="flow-k">message</div><pre>${esc(item.content || "")}</pre>`);
      } else if (item.label === 'observation') {
        if (item.code) rows.push(`<div class="flow-k">source</div><pre>${esc(item.code)}</pre>`);
        if (d.details) rows.push(`<div class="flow-k">details</div><pre>${esc(JSON.stringify(d.details, null, 2))}</pre>`);
        rows.push(`<div class="flow-k">summary</div><pre>${esc(item.content || "")}</pre>`);
      }
      return `<div class="flow-content">${rows.join("")}</div>`;
    }

    function renderCanonicalFrame(item) {
      const d = item.details || {};
      if (item.status === 'opened') {
        return `<div class="flow-content"><pre>open child frame
parent: ${esc(d.parent_frame_id || "root")}
child: ${esc(d.frame_id || "-")}
depth: ${esc(d.depth || 0)}
goal: ${esc(shortText(d.goal || "-", 320))}</pre></div>`;
      }
      const payload = d.return_payload || {};
      const findings = Array.isArray(payload.findings) ? payload.findings.join(" / ") : "";
      return `<div class="flow-content"><pre>return to parent
child: ${esc(d.frame_id || "-")}
parent: ${esc(d.parent_frame_id || "root")}
depth: ${esc(d.depth || 0)}
summary: ${esc(shortText(payload.summary || item.content || "", 320))}
findings: ${esc(shortText(findings || "-", 320))}</pre></div>`;
    }

    function renderLlmOutputIssue(item) {
      const d = item.details || {};
      const thinking = d.thinking_text || "";
      const raw = d.raw_text || "";
      const combined = d.combined_text || "";
      const meta = d.stream_metadata ? JSON.stringify(d.stream_metadata, null, 2) : "";
      return `<div class="flow-content">
        <pre>${esc(item.content || "")}</pre>
        ${thinking ? `<div class="flow-k">thinking</div><pre>${esc(thinking)}</pre>` : ''}
        ${raw ? `<div class="flow-k">content</div><pre>${esc(raw)}</pre>` : ''}
        ${(!thinking && !raw && combined) ? `<div class="flow-k">combined</div><pre>${esc(combined)}</pre>` : ''}
        ${meta ? `<div class="flow-k">stream metadata</div><pre>${esc(meta)}</pre>` : ''}
      </div>`;
    }

    function renderLlmOutputRecovered(item) {
      const d = item.details || {};
      return `<div class="flow-content" style="border-left:3px solid #2d8f6f; background:#0d1a16; padding:10px;">
        <div style="color:#7fdfaa; font-weight:bold; margin-bottom:6px;">✓ recovery: 余計テキスト除去</div>
        <pre>${esc(item.content || "")}</pre>
        ${d.envelope_tool_name ? `<div class="flow-k">採用したツール</div><pre>${esc(d.envelope_tool_name)}</pre>` : ''}
        ${d.raw_length ? `<div class="flow-k">元出力サイズ</div><pre>${esc(d.raw_length)} chars</pre>` : ''}
        ${d.warning ? `<div class="flow-k">警告</div><pre>${esc(d.warning)}</pre>` : ''}
        <details style="margin-top:8px;"><summary style="cursor:pointer; color:#9ec5ff;">この recovery が必要な理由</summary>
          <pre>P4 は machine-control schema として「1ターン1 envelope」を要求します。LLM がそれに違反した場合、旧設計はターン全体を失敗扱いにしていました。
やり切る invariant の下では、最初の有効な envelope を採用してユーザー request の達成を継続します。
LLM が複数手を一括予測しようとしたとき、最初の手だけ採用して 1 ステップずつ進めます。</pre>
        </details>
      </div>`;
    }

    function renderRuntimeEvent(item) {
      const d = item.details || {};
      const eventName = item.event_name || "";
      const rows = [];
      if (eventName) rows.push(`<div class="flow-k">event</div><pre>${esc(eventName)}</pre>`);
      if (d.tool_name) rows.push(`<div class="flow-k">tool</div><pre>${esc(d.tool_name)}</pre>`);
      if (d.ok !== undefined) rows.push(`<div class="flow-k">ok</div><pre>${esc(d.ok)}</pre>`);
      if (d.model) rows.push(`<div class="flow-k">model</div><pre>${esc(d.model)}</pre>`);
      if (d.attempt_count !== undefined) rows.push(`<div class="flow-k">attempt</div><pre>${esc(d.attempt_count)}</pre>`);
      if (d.parse_issue) rows.push(`<div class="flow-k">parse issue</div><pre>${esc(d.parse_issue)}</pre>`);
      if (d.thinking_text) rows.push(`<div class="flow-k">thinking</div><pre>${esc(d.thinking_text)}</pre>`);
      if (d.content_text) rows.push(`<div class="flow-k">content</div><pre>${esc(d.content_text)}</pre>`);
      if (d.partial) rows.push(`<div class="flow-k">partial</div><pre>${esc(JSON.stringify(d.partial, null, 2))}</pre>`);
      if (d.tool_args) rows.push(`<div class="flow-k">tool args</div><pre>${esc(JSON.stringify(d.tool_args, null, 2))}</pre>`);
      if (d.tool_result) rows.push(`<div class="flow-k">tool result</div><pre>${esc(JSON.stringify(d.tool_result, null, 2))}</pre>`);
      if (!rows.length) rows.push(`<pre>${esc(item.content || "")}</pre>`);
      return `<div class="flow-content">${rows.join("")}</div>`;
    }

    function renderTaskPlan(item) {
      const tasks = Array.isArray(item.tasks) ? item.tasks : [];
      const rows = [];
      rows.push(`<div class="flow-k">summary</div><pre>${esc(item.content || "")}</pre>`);
      if (item.rationale) rows.push(`<div class="flow-k">rationale</div><pre>${esc(item.rationale)}</pre>`);
      tasks.forEach((task, index) => {
        rows.push(`<div class="flow-k">task ${index + 1}</div><pre>${esc(JSON.stringify(task, null, 2))}</pre>`);
      });
      return `<div class="flow-content">${rows.join("")}</div>`;
    }

    function renderPlannerEvent(item) {
      const rows = [];
      rows.push(`<div class="flow-k">summary</div><pre>${esc(item.content || "")}</pre>`);
      if (item.strategy) rows.push(`<div class="flow-k">strategy</div><pre>${esc(item.strategy)}</pre>`);
      if (item.profile && Object.keys(item.profile).length) rows.push(`<div class="flow-k">problem profile</div><pre>${esc(JSON.stringify(item.profile, null, 2))}</pre>`);
      if (item.plan && Object.keys(item.plan).length) rows.push(`<div class="flow-k">plan record</div><pre>${esc(JSON.stringify(item.plan, null, 2))}</pre>`);
      const workUnits = Array.isArray(item.work_units) ? item.work_units : [];
      if (workUnits.length) rows.push(`<div class="flow-k">work units</div><pre>${esc(JSON.stringify(workUnits, null, 2))}</pre>`);
      const vc = item.verification_contract || {};
      if (Object.keys(vc).length) rows.push(`<div class="flow-k">verification contract</div><pre>${esc(JSON.stringify(vc, null, 2))}</pre>`);
      if (item.details && Object.keys(item.details).length) rows.push(`<div class="flow-k">details</div><pre>${esc(JSON.stringify(item.details, null, 2))}</pre>`);
      return `<div class="flow-content">${rows.join("")}</div>`;
    }

    function renderLiveOutput(text) {
      const clean = String(text || "");
      if (!clean) return '<div class="flow-empty">まだ出力はありません。</div>';
      if (clean.startsWith("Waiting for model response")) {
        return `<div class="live-state waiting"><div class="flow-k">status</div><pre>Waiting for model response...</pre></div>`;
      }
      if (clean.startsWith("Running command via ")) {
        return `<div class="live-state running"><div class="flow-k">status</div><pre>${esc(clean)}</pre></div>`;
      }
      if (clean.startsWith("[thinking]")) {
        const body = clean.slice("[thinking]".length).replace(/^\\n/, "");
        return `<div class="flow-content llm-thinking"><div class="flow-k">thinking stream</div><pre>${esc(body)}</pre></div>`;
      }
      if (clean.includes("[content]")) {
        const [thinkingPart, contentPart] = clean.split("[content]");
        const thinking = thinkingPart.replace("[thinking]", "").trim();
        const content = String(contentPart || "").trim();
        return `<div class="flow-content llm-thinking">
          ${thinking ? `<div class="flow-k">thinking stream</div><pre>${esc(thinking)}</pre>` : ''}
          <div class="flow-k">content stream</div><pre>${esc(content)}</pre>
        </div>`;
      }
      return `<pre>${esc(clean)}</pre>`;
    }

    function renderFrameFlowContent(item) {
      const payload = item.return_payload || {};
      const summary = payload.summary || item.content || "";
      const findings = Array.isArray(payload.findings) ? payload.findings.join(" / ") : "";
      if (item.label === "frame_opened") {
        return `<div class="flow-content"><pre>open child frame
parent: ${esc(item.parent_frame_id || "root")}
child: ${esc(item.frame_id || "-")}
goal: ${esc(shortText(item.goal || item.content || "-", 320))}</pre></div>`;
      }
      if (item.label === "frame_returned") {
        return `<div class="flow-content"><pre>return to parent
child: ${esc(item.frame_id || "-")}
parent: ${esc(item.parent_frame_id || "root")}
summary: ${esc(shortText(summary, 320))}
findings: ${esc(shortText(findings || "-", 320))}</pre></div>`;
      }
      return `<div class="flow-content"><pre>child result received
child: ${esc(item.child_frame_id || "-")}
summary: ${esc(shortText(summary, 320))}
findings: ${esc(shortText(findings || "-", 320))}</pre></div>`;
    }

    function commentatorRows(text) {
      const rawLines = String(text || "").split(/\\n+/).map(v => v.trim()).filter(Boolean);
      const rows = [];
      for (const line of rawLines) {
        const m = line.match(/^(?:\\d+[\\.．、:]\\s*)?([^:：]{2,22})[:：]\\s*(.*)$/);
        if (m) rows.push({ label: m[1], body: m[2] });
        else rows.push({ label: "解説", body: line });
      }
      return rows.length ? rows : [{ label: "解説", body: "まだ解説本文はありません。" }];
    }

    function renderCommentatorContent(text) {
      return `<div class="commentator-body">${
        commentatorRows(text).map(row => `
          <div class="commentator-line">
            <strong>${esc(row.label)}</strong>
            <span>${esc(row.body)}</span>
          </div>
        `).join("")
      }</div>`;
    }

    function scrollSnapshot(el) {
      const bottom = Math.max(0, el.scrollHeight - el.clientHeight - el.scrollTop);
      return { top: el.scrollTop, left: el.scrollLeft, nearBottom: bottom < 8 };
    }

    function restoreElementScroll(el, saved) {
      if (!saved) return;
      if (saved.nearBottom) el.scrollTop = el.scrollHeight;
      else el.scrollTop = saved.top;
      el.scrollLeft = saved.left;
    }

    function setHtmlPreservingScroll(el, html, key = "") {
      if (!el) return;
      const nextKey = key || html;
      if (el.dataset.renderKey === nextKey) return;
      const saved = scrollSnapshot(el);
      el.innerHTML = html;
      el.dataset.renderKey = nextKey;
      restoreElementScroll(el, saved);
    }

    function setTextIfChanged(el, text) {
      if (el && el.textContent !== String(text ?? "")) el.textContent = String(text ?? "");
    }

    function operationOpenByDefault(op, index) {
      const opId = op.operation_id || "";
      return openOperationIds.has(opId) || (!closedOperationIds.has(opId) && (op.status === "running" || index === 0));
    }

    function renderBlockedReason(op) {
      return op.blocked_reason ? `<strong>ブロック理由</strong><pre>${esc(op.blocked_reason)}</pre>` : "";
    }

    function renderOperationCard(op, index) {
      const opId = op.operation_id || "";
      const open = operationOpenByDefault(op, index);
      return `<section class="operation-card ${esc(op.status)}" data-operation-id="${esc(opId)}">
        <button class="operation-head" type="button" onclick="toggleOperation('${esc(opId)}')">
          <strong data-role="operation-title"></strong>
          <span data-role="operation-status" class="operation-status"></span>
        </button>
        <div class="bubble-meta" data-role="operation-started"></div>
        <div class="operation-body ${open ? '' : 'closed'}">
          <pre class="operation-detail" data-role="operation-detail" style="padding: 12px;"></pre>
          <div class="blocked-reason" data-role="blocked-reason" style="display:none;"></div>
          <div class="flow-container" style="padding: 0 12px 12px 12px;">
            <div class="flow-label"><span class="depth-badge">FLOW</span><span>階層フロー</span></div>
            <div data-role="flow-body"></div>
          </div>
          <div class="nested-block">
            <button class="nested-toggle" type="button" onclick="toggleNested('${esc(opId)}:live')">ライブ出力</button>
            <div class="nested-content ${openNestedIds.has(`${opId}:live`) ? '' : 'closed'}" data-nested-id="${esc(opId)}:live">
              <div class="operation-output" data-role="live-output"></div>
            </div>
          </div>
        </div>
      </section>`;
    }

    function updateOperationCard(card, op, index) {
      const opId = op.operation_id || "";
      card.className = `operation-card ${op.status || ""}`;
      card.dataset.operationId = opId;
      setTextIfChanged(card.querySelector('[data-role="operation-title"]'), op.title || "Operation");
      const statusEl = card.querySelector('[data-role="operation-status"]');
      setTextIfChanged(statusEl, op.status || "");
      if (statusEl) statusEl.className = `operation-status ${op.status || ""}`;
      setTextIfChanged(card.querySelector('[data-role="operation-started"]'), op.started_at || "");
      setTextIfChanged(card.querySelector('[data-role="operation-detail"]'), op.detail || "");
      const body = card.querySelector(".operation-body");
      if (body) body.classList.toggle("closed", !operationOpenByDefault(op, index));
      const blocked = card.querySelector('[data-role="blocked-reason"]');
      if (blocked) {
        blocked.style.display = op.blocked_reason ? "" : "none";
        setHtmlPreservingScroll(blocked, renderBlockedReason(op), `blocked:${op.blocked_reason || ""}`);
      }
      const flowBody = card.querySelector('[data-role="flow-body"]');
      const flowKey = JSON.stringify(op.flow_steps || []);
      setHtmlPreservingScroll(flowBody, renderFlowSteps(op.flow_steps || [], opId), `flow:${flowKey}`);
      const live = card.querySelector('[data-role="live-output"]');
      setHtmlPreservingScroll(live, renderLiveOutput(op.output_preview || ""), `live:${op.output_preview || ""}`);
    }

    function syncOperations(panel, ops) {
      const existing = new Map(Array.from(panel.querySelectorAll(".operation-card[data-operation-id]")).map(card => [card.dataset.operationId, card]));
      const seen = new Set();
      if (ops.length && existing.size === 0) panel.innerHTML = "";
      ops.forEach((op, index) => {
        const opId = op.operation_id || "";
        if (!opId) return;
        let card = existing.get(opId);
        if (card && !card.querySelector('[data-role="operation-title"]')) {
          card.remove();
          card = null;
        }
        if (!card) {
          const wrapper = document.createElement("div");
          wrapper.innerHTML = renderOperationCard(op, index).trim();
          card = wrapper.firstElementChild;
        }
        updateOperationCard(card, op, index);
        panel.appendChild(card);
        seen.add(opId);
      });
      for (const [opId, card] of existing.entries()) {
        if (!seen.has(opId)) card.remove();
      }
      if (!ops.length) panel.innerHTML = "<div>まだ実行操作はありません。</div>";
    }

    function captureScrollState() {
      const boxes = {};
      document.querySelectorAll("[data-nested-id] .operation-output").forEach(el => {
        const owner = el.closest("[data-nested-id]");
        const id = owner ? owner.getAttribute("data-nested-id") : "";
        if (id) {
          const bottom = Math.max(0, el.scrollHeight - el.clientHeight - el.scrollTop);
          boxes[id] = { top: el.scrollTop, left: el.scrollLeft, bottom, nearBottom: bottom < 8 };
        }
      });
      document.querySelectorAll("[data-flow-scroll-id]").forEach(el => {
        const id = el.getAttribute("data-flow-scroll-id") || "";
        if (id) {
          const bottom = Math.max(0, el.scrollHeight - el.clientHeight - el.scrollTop);
          boxes[`flow:${id}`] = { top: el.scrollTop, left: el.scrollLeft, bottom, nearBottom: bottom < 8 };
        }
      });
      return { x: window.scrollX, y: window.scrollY, boxes };
    }

    function restoreScrollStateNow(state) {
      document.querySelectorAll("[data-nested-id] .operation-output").forEach(el => {
        const owner = el.closest("[data-nested-id]");
        const id = owner ? owner.getAttribute("data-nested-id") : "";
        const saved = id ? state.boxes[id] : null;
        if (saved) {
          if (saved.nearBottom) el.scrollTop = el.scrollHeight;
          else el.scrollTop = saved.top;
          el.scrollLeft = saved.left;
        }
      });
      document.querySelectorAll("[data-flow-scroll-id]").forEach(el => {
        const id = el.getAttribute("data-flow-scroll-id") || "";
        const saved = state.boxes[`flow:${id}`];
        if (saved) {
          if (saved.nearBottom) el.scrollTop = el.scrollHeight;
          else el.scrollTop = saved.top;
          el.scrollLeft = saved.left;
        }
      });
      window.scrollTo(state.x, state.y);
    }

    function restoreScrollState(state) {
      restoreScrollStateNow(state);
      requestAnimationFrame(() => restoreScrollStateNow(state));
      setTimeout(() => restoreScrollStateNow(state), 50);
    }

    function renderSnapshot(snapshot) {
      const scrollState = captureScrollState();
      console.log("Snapshot received", snapshot);
      latestSnapshot = snapshot;
      const rt = snapshot.runtime || {};
      const judge = snapshot.judge_metrics || {};
      const ops = snapshot.recent_operations || [];

      document.getElementById("statusPill").textContent = `状態: ${rt.status || "idle"}`;
      document.getElementById("modelPill").textContent = `モデル: ${rt.current_model || snapshot.model}`;
      syncModelSelect(snapshot.available_models || [], rt.current_model || snapshot.model);
      const parseIssue = rt.last_llm_parse_issue ? ` / 失敗分類: ${rt.last_llm_parse_issue}` : "";
      const doneReason = rt.last_llm_stream_metadata && rt.last_llm_stream_metadata.done_reason ? ` / done: ${rt.last_llm_stream_metadata.done_reason}` : "";
      document.getElementById("lastLlmPill").textContent = `直近LLM: ${rt.last_llm_duration_ms || "-"}ms${parseIssue}${doneReason}`;
      document.getElementById("workspacePill").textContent = `作業場: ${rt.current_llm_workspace || rt.last_llm_workspace || "-"}`;
      document.getElementById("judgePill").textContent = `judge: blocks=${judge.consecutive_finish_blocks || 0} / last=${judge.last_judge_decision || "-"} / retries=${judge.judge_retry_count || 0} / fallback=${judge.fallback_used ? "yes" : "no"}`;
      document.getElementById("operationsSummary").textContent = `実行操作 (${ops.length})`;

      const resultBody = document.getElementById("latestResultBody");
      const resultMeta = document.getElementById("latestResultMeta");
      if (resultBody && resultMeta) {
        const summary = latestResultSummary(snapshot);
        const detail = latestResultText(snapshot);
        resultMeta.textContent = summary ? `直近: ${summary}` : "直近結果はありません。";
        resultBody.textContent = shortText(detail, 4000);
      }

      const contract = snapshot.contract_progress || {};
      const contractBody = document.getElementById("contractProgressBody");
      if (contractBody) {
        contractBody.innerHTML = `
          <div class="commentator-line"><strong>契約状態</strong> <span>${esc(contract.contract_state || "unknown")}</span></div>
          <div class="commentator-line"><strong>必要契約</strong> <span>${esc(contractRequiredText(contract))}</span></div>
          <div class="commentator-line"><strong>ファイル作成</strong> <span>${esc(contractMetricText(contract, "artifact_written"))}</span></div>
          <div class="commentator-line"><strong>コマンド実行</strong> <span>${esc(contractMetricText(contract, "command_executed"))}</span></div>
          <div class="commentator-line"><strong>表示出力</strong> <span>${esc(contractMetricText(contract, "stdout_displayed"))}</span></div>
          <div class="commentator-line"><strong>ユーザ応答</strong> <span>${esc(contract.result_selected_for_user || "no")}</span></div>
        `;
      }

      if (Date.now() > suspendOperationsRenderUntil) {
         const panel = document.getElementById("operationsPanel");
         syncOperations(panel, ops);
      }
      requestAnimationFrame(() => restoreScrollState(scrollState));
    }

    async function refresh() {
      try {
        const r = await fetch("/api/snapshot");
        if (r.ok) renderSnapshot(await r.json());
      } catch(e) {}
    }

    async function sendMessage() {
      const msg = document.getElementById("messageText").value.trim();
      if (!msg) return;
      const resultEl = document.getElementById("chatResult");
      const sendButton = document.getElementById("sendButton");
      resultEl.textContent = "送信中...";
      sendButton.disabled = true;
      try {
        const r = await fetch("/api/message", {
          method: "POST",
          headers: {"Content-Type": "application/json"},
          body: JSON.stringify({
            content: msg,
            model: document.getElementById("modelSelect").value,
            mode: document.getElementById("modeSelect").value,
            shell: document.getElementById("shellSelect").value
          })
        });
        if (r.ok) {
          resultEl.textContent = "送信しました。";
          document.getElementById("messageText").value = "";
          setTimeout(refresh, 100);
        } else {
          const body = await r.text();
          resultEl.textContent = `送信に失敗しました。HTTP ${r.status} ${body.slice(0, 160)}`;
        }
      } catch(e) {
        resultEl.textContent = `ネットワークエラーです: ${e}`;
      } finally {
        sendButton.disabled = false;
      }
    }

    const events = new EventSource("/api/events");
    events.addEventListener("snapshot", (e) => renderSnapshot(JSON.parse(e.data)));
    setInterval(refresh, 2000);
  </script>
</body>
</html>"""

def _phase_for_flow_step(step: dict[str, Any]) -> str:
    step_index = int(step.get("step_index") or 0)
    if step_index == 0:
        return "DISCOVER_REQUIRED_COMMANDS"
    items = step.get("items") or []
    labels = {str(item.get("label") or "") for item in items if isinstance(item, dict)}
    tool_names = {str(item.get("tool_name") or "") for item in items if isinstance(item, dict)}
    if "decision" in labels:
        decision_items = [item for item in items if isinstance(item, dict) and str(item.get("label") or "") == "decision"]
        if any(str(item.get("code") or "") == "finish" and str(item.get("status") or "") == "accepted" for item in decision_items):
            return "FINISH"
        return "DECISION"
    if "frame" in labels:
        return "FRAME"
    if "tool" in labels and "run_command" in tool_names:
        return "EXECUTE_MISSING_COMMANDS"
    if "tool" in labels:
        return "TOOL"
    if "llm" in labels:
        return "LLM"
    if "observation" in labels:
        return "OBSERVATION"
    if "finish" in labels:
        return "FINISH"
    if labels & {"frame_opened", "frame_returned", "child_return"}:
        return "FRAME"
    if labels & {"problem_profile", "planner_decision", "plan_record", "plan_revision"}:
        return "PLANNING"
    if "tool_call" in labels and "run_command" in tool_names:
        return "EXECUTE_MISSING_COMMANDS"
    if "tool_result" in labels and "run_command" in tool_names:
        return "SYNTHESIZE_FROM_EVIDENCE"
    if "assistant_message" in labels:
        return "SYNTHESIZE_FROM_EVIDENCE"
    return "DISCOVER_REQUIRED_COMMANDS"

def _render_flow_steps_html(child_tasks: list[dict[str, Any]], op_id: str = "") -> str:
    if not child_tasks:
        return "<div class=\"flow-empty\">まだ flow はありません。</div>"

    def _task_summary(task: dict[str, Any]) -> str:
        lines: list[str] = []
        for step in task.get("steps") or []:
            for item in step.get("items") or []:
                if not isinstance(item, dict):
                    continue
                if item.get("hidden"):
                    continue
                details = item.get("details") if isinstance(item.get("details"), dict) else {}
                if item.get("card_type") == "llm" and details.get("tool_name"):
                    causal_notes = details.get("causal_notes") if isinstance(details.get("causal_notes"), list) else []
                    if causal_notes:
                        note = causal_notes[-1] if isinstance(causal_notes[-1], dict) else {}
                        lines.append(
                            f"P4判定: {note.get('failure_type') or note.get('title') or 'repaired'}"
                            + (f" / {_short_text(str(note.get('summary') or ''), 100)}" if note.get("summary") else "")
                        )
                    msg = "" if causal_notes else _short_text(str(details.get("assistant_message") or ""), 120)
                    lines.append(f"LLM提案: {details.get('tool_name')}" + (f" / {msg}" if msg else ""))
                elif item.get("card_type") == "finish" and details.get("acceptance"):
                    acceptance = details.get("acceptance") if isinstance(details.get("acceptance"), dict) else {}
                    lines.append(f"完了判定: {acceptance.get('message') or acceptance.get('reason_code') or 'accepted'}")
                elif item.get("label") == "decision" and item.get("content"):
                    lines.append(f"最終応答: {_short_text(str(item.get('content') or ''), 140)}")
                elif item.get("label") == "observation" and item.get("content") and not lines:
                    lines.append(_short_text(str(item.get("content") or ""), 160))
        priority = [line for line in lines if line.startswith("P4判定:")]
        merged: list[str] = []
        for line in [*priority, *lines[-3:]]:
            if line and line not in merged:
                merged.append(line)
        return "\n".join(merged[:4]) or str(task.get("status") or "unknown")
    
    parts = []
    for task_index, task in enumerate(child_tasks, start=1):
        status = str(task.get("status") or "")
        icon = "✓" if status == "finished" else "⚠" if status in ("finished_with_warnings", "blocked", "failed") else "●"
        is_closed = "closed" if status in ("finished", "finished_with_warnings") else ""
        summary = _task_summary(task)
        
        task_id = f"{op_id}:task_{task_index}"
        
        parts.append(f'''
        <div class="nested-block">
            <button class="nested-toggle" onclick="toggleNested('{task_id}')">
                {icon} 子タスク {task_index}: {html.escape(task.get('title') or '')}
            </button>
            <pre class="subtle" style="padding:0 8px 8px 8px; white-space:pre-wrap;">{html.escape(summary)}</pre>
            <div class="nested-content {is_closed}" data-nested-id="{task_id}">
                <div class="operation-output">
        ''')
        
        last_llm_input_signature = ""
        for step in task.get("steps") or []:
            step_index = int(step.get("step_index") or 0)
            rendered_items: list[str] = []
            for item_index, item in enumerate(step.get("items") or []):
                if not isinstance(item, dict) or item.get("hidden"):
                    continue
                render_item = item
                details = item.get("details") if isinstance(item.get("details"), dict) else {}
                if item.get("card_type") == "llm" and details:
                    signature = json.dumps(
                        {
                            "role": details.get("role") or "",
                            "transport": details.get("transport") or "",
                            "schema_required": details.get("schema_required", ""),
                            "model": details.get("model") or "",
                        },
                        sort_keys=True,
                    )
                    render_item = dict(item)
                    render_details = dict(details)
                    render_details["p4_input_repeated"] = bool(last_llm_input_signature and last_llm_input_signature == signature)
                    render_item["details"] = render_details
                    last_llm_input_signature = signature
                rendered_items.append(_render_flow_item_html(render_item, scroll_id=f"initial:{step_index}:{item_index}"))
            items_html = "".join(rendered_items)
            parts.append(
                f"<section class=\"flow-step\">"
                f"<div class=\"flow-step-title\">Step {step_index}"
                f"<span class=\"flow-phase\">{html.escape(str(step.get('phase') or '-'))}</span></div>"
                f"{items_html}"
                "</section>"
            )
            
        parts.append('''
                </div>
            </div>
        </div>
        ''')
        
    return "".join(parts)

def _flow_content_scroll_attr(scroll_id: str) -> str:
    return f" data-flow-scroll-id=\"{html.escape(scroll_id)}\"" if scroll_id else ""

def _attach_flow_scroll_id(content: str, scroll_id: str) -> str:
    if not scroll_id:
        return content
    return content.replace("<div class=\"flow-content\"", f"<div class=\"flow-content\"{_flow_content_scroll_attr(scroll_id)}", 1)

def _render_judge_details_html(payload: dict[str, Any], title: str) -> str:
    details = payload.get("details") if isinstance(payload.get("details"), dict) else payload
    attempts = details.get("attempts") if isinstance(details.get("attempts"), list) else []
    parts: list[str] = [f"<div class=\"flow-k\">{html.escape(title)}</div>"]
    prompt = str(details.get("prompt") or "")
    if prompt:
        parts.append(
            "<div class=\"flow-k\">P4 → judge LLM input</div>"
            "<details><summary style=\"cursor:pointer; color:#9ec5ff;\">judge prompt</summary>"
            f"<pre>{html.escape(prompt)}</pre></details>"
        )
    final_answer = str(details.get("final_answer") or "")
    if final_answer:
        parts.append(f"<div class=\"flow-k\">judge input final_answer</div><pre>{html.escape(final_answer)}</pre>")
    evidence_text = str(details.get("evidence_text") or "")
    if evidence_text:
        parts.append(f"<div class=\"flow-k\">judge input evidence</div><pre>{html.escape(evidence_text)}</pre>")
    parts.append("<div class=\"flow-k\">judge LLM → P4 output</div>")
    parsed = details.get("parsed") if isinstance(details.get("parsed"), dict) else {}
    reason_code = str(payload.get("reason_code") or "")
    if reason_code:
        parts.append(f"<div class=\"flow-k\">reason_code</div><pre>{html.escape(reason_code)}</pre>")
    message = str(payload.get("message") or "")
    if message:
        parts.append(f"<div class=\"flow-k\">message</div><pre>{html.escape(message)}</pre>")
    decision = str(details.get("decision") or "")
    if decision:
        parts.append(f"<div class=\"flow-k\">judge decision</div><pre>{html.escape(decision)}</pre>")
    model = str(details.get("response_model") or details.get("model") or "")
    if model:
        parts.append(f"<div class=\"flow-k\">judge model</div><pre>{html.escape(model)}</pre>")
    answer = str(parsed.get("verdict") or parsed.get("status") or details.get("verdict") or details.get("status") or "")
    if answer:
        parts.append(f"<div class=\"flow-k\">judge LLM answer</div><pre>{html.escape(answer)}</pre>")
    judge_reason = str(parsed.get("reason_code") or details.get("reason_code") or "")
    if judge_reason:
        parts.append(f"<div class=\"flow-k\">judge reason</div><pre>{html.escape(judge_reason)}</pre>")
    rationale = str(parsed.get("rationale") or details.get("rationale") or "")
    if rationale:
        parts.append(f"<div class=\"flow-k\">judge rationale</div><pre>{html.escape(rationale)}</pre>")
    observed_mismatch = str(parsed.get("observed_mismatch") or "")
    if observed_mismatch:
        parts.append(f"<div class=\"flow-k\">observed mismatch</div><pre>{html.escape(observed_mismatch)}</pre>")
    unsupported = parsed.get("unsupported_claims")
    if isinstance(unsupported, list) and unsupported:
        parts.append(f"<div class=\"flow-k\">unsupported claims</div><pre>{html.escape(chr(10).join(str(item) for item in unsupported))}</pre>")
    if attempts:
        lines: list[str] = []
        for attempt in attempts:
            if not isinstance(attempt, dict):
                continue
            suffix = str(attempt.get("error") or attempt.get("decision") or "")
            attempt_no = str(attempt.get("attempt") or "-")
            lines.append(f"attempt {attempt_no}: {suffix}" if suffix else f"attempt {attempt_no}")
        if lines:
            parts.append(f"<div class=\"flow-k\">judge attempts</div><pre class=\"stderr\">{html.escape(chr(10).join(lines))}</pre>")
    raw = json.dumps(payload, ensure_ascii=False, indent=2)
    parts.append(
        "<details style=\"margin-top:8px;\">"
        "<summary style=\"cursor:pointer; color:#9ec5ff;\">judge raw details</summary>"
        f"<pre>{html.escape(raw)}</pre></details>"
    )
    return f"<div class=\"judge-detail\">{''.join(parts)}</div>"

def _render_tool_execution_result_html(details: dict[str, Any]) -> str:
    tool_name = str(details.get("tool_name") or "")
    tool_result = details.get("tool_result") if isinstance(details.get("tool_result"), dict) else {}
    parts: list[str] = []
    if tool_name:
        ok_text = f" <span class=\"tool-pill\">ok: {html.escape(str(tool_result.get('ok')))}</span>" if "ok" in tool_result else ""
        parts.append(f"<div><span class=\"tool-pill\">{html.escape(tool_name)}</span>{ok_text}</div>")
    if tool_name == "list_files" and isinstance(tool_result.get("items"), list):
        item_lines = [f"- {str(item)}" for item in tool_result.get("items") or []]
        parts.append(f"<div class=\"flow-k\">list_files result</div><pre>{html.escape(chr(10).join(item_lines) or '(empty)')}</pre>")
    elif tool_name == "run_command":
        parts.append(_render_command_result_html(tool_result))
    elif tool_name in {"write_file", "append_file", "replace_text"}:
        file_bits: list[str] = []
        if tool_result.get("path"):
            file_bits.append(f"path: {tool_result.get('path')}")
        for key in ("bytes_written", "bytes_appended"):
            if key in tool_result:
                file_bits.append(f"{key}: {tool_result.get(key)}")
        parts.append(
            f"<div class=\"flow-k\">file result</div><pre>{html.escape(chr(10).join(file_bits) or json.dumps(tool_result, ensure_ascii=False, indent=2))}</pre>"
        )
    elif tool_result:
        parts.append(f"<div class=\"flow-k\">result</div><pre>{html.escape(json.dumps(tool_result, ensure_ascii=False, indent=2))}</pre>")
    return (
        "<section class=\"nested-block\" style=\"margin:10px 0 0 0;\">"
        "<button class=\"nested-toggle\" type=\"button\">TOOL実行結果</button>"
        f"<div class=\"nested-content\" style=\"padding:8px;\">{''.join(parts)}</div></section>"
    )

def _render_llm_causal_notes_html(notes: Any) -> str:
    if not isinstance(notes, list) or not notes:
        return ""
    rows: list[str] = ["<div class=\"flow-k\">P4判定と再要求の経緯</div>"]
    for note in notes:
        if not isinstance(note, dict):
            continue
        parts: list[str] = []
        if note.get("failure_type"):
            parts.append(f"<div class=\"flow-k\">failure_type</div><pre>{html.escape(str(note.get('failure_type') or ''))}</pre>")
        if note.get("summary"):
            parts.append(f"<div class=\"flow-k\">what happened</div><pre>{html.escape(str(note.get('summary') or ''))}</pre>")
        if note.get("next"):
            parts.append(f"<div class=\"flow-k\">next</div><pre>{html.escape(str(note.get('next') or ''))}</pre>")
        rows.append(
            "<section class=\"nested-block\" style=\"margin:8px 0;\">"
            f"<button class=\"nested-toggle\" type=\"button\">{html.escape(str(note.get('title') or 'LLM retry context'))}</button>"
            f"<div class=\"nested-content\" style=\"padding:8px;\">{''.join(parts)}</div></section>"
        )
    return "".join(rows)

def _render_rejected_llm_attempts_html(attempts: Any) -> str:
    if not isinstance(attempts, list) or not attempts:
        return ""
    rows: list[str] = []
    for attempt in attempts:
        if not isinstance(attempt, dict):
            continue
        parts: list[str] = []
        if attempt.get("model"):
            parts.append(f"<div class=\"flow-k\">model</div><pre>{html.escape(str(attempt.get('model') or ''))}</pre>")
        if attempt.get("content_text"):
            parts.append(f"<div class=\"flow-k\">content</div><pre>{html.escape(str(attempt.get('content_text') or ''))}</pre>")
        rows.append(
            "<section class=\"nested-block\" style=\"margin:8px 0;\">"
            f"<button class=\"nested-toggle\" type=\"button\">{html.escape(str(attempt.get('title') or 'LLM → P4 Output (rejected)'))}</button>"
            f"<div class=\"nested-content\" style=\"padding:8px;\">{''.join(parts)}</div></section>"
        )
    return "".join(rows)

def _render_flow_item_html(item: dict[str, Any], *, scroll_id: str = "") -> str:
    if item.get("hidden"):
        return ""
    label_map = {
        "observer_note": "解説者",
        "system_note": "システム",
        "planning_note": "計画",
        "task_plan": "子タスク計画",
        "problem_profile": "問題プロファイル",
        "planner_decision": "プランナー判定",
        "plan_record": "計画契約",
        "plan_revision": "計画改訂",
        "activity_update": "システム状態",
        "runtime_event": "実行イベント",
        "assistant_message": "LLM応答",
        "user_message": "ユーザー",
        "tool_call": "ツール呼び出し",
        "tool_result": "ツール結果",
        "finish": "完了",
        "frame_opened": "フレーム開始",
        "frame_returned": "フレーム帰還",
        "child_return": "子フレーム結果",
        "live_stream": "LLMライブ",
        "llm": "LLM",
        "tool": "ツール",
        "frame": "フレーム",
        "decision": "判定",
        "observation": "観測",
    }
    label = html.escape(label_map.get(str(item.get("label") or ""), str(item.get("label") or "")))
    content = str(item.get("content") or "")
    tool_name = str(item.get("tool_name") or "")
    depth = int(item.get("frame_depth") or 0)
    indent = min(depth, 8) * 18
    depth_badge = f"<span class=\"depth-badge\">D{depth}</span>"
    depth_meta = f"<span class=\"depth-meta\">階層深度: {depth} / インデント: {indent}px</span>"

    # Blocked status detection
    extra_class = ""
    is_blocked = (str(item.get("label")) == "system_note" and "ブロックされました" in content) or (
        str(item.get("label")) == "decision" and str(item.get("status") or "") == "blocked"
    )
    if is_blocked:
        extra_class = " blocked"
        label = f"{label} (BLOCKED)"

    if str(item.get("label") or "") == "consolidated_card":
        card_type = str(item.get("card_type") or "")
        details = item.get("details", {})
        parts = []
        
        if card_type == "llm":
            role = str(details.get("role") or "")
            transport = str(details.get("transport") or "")
            schema_required = details.get("schema_required")
            request_bits = ["same machine-control contract as previous Step" if details.get("p4_input_repeated") else "machine-control action request"]
            if role:
                request_bits.append(f"role: {role}")
            if transport:
                request_bits.append(f"transport: {transport}")
            if schema_required is not None:
                request_bits.append(f"schema_required: {schema_required}")
            if details.get("attempt_count"):
                request_bits.append(f"attempt: {details.get('attempt_count')}")
            if details.get("model"):
                request_bits.append(f"model: {details.get('model')}")
            parts.append(f"<div class=\"flow-k\">P4 → Agent LLM Input</div><pre>{html.escape(' / '.join(request_bits))}</pre>")
            if details.get("model_reason") or details.get("prompt") or details.get("prompt_preview"):
                parts.append("<details style=\"margin-top:8px;\"><summary style=\"cursor:pointer; color:#9ec5ff;\">P4 input details</summary>")
                if details.get("model_reason"):
                    parts.append(f"<div class=\"flow-k\" style=\"margin-top:8px;\">model reason</div><pre>{html.escape(str(details.get('model_reason') or ''))}</pre>")
                if details.get("prompt"):
                    parts.append(f"<div class=\"flow-k\" style=\"margin-top:8px;\">full prompt</div><pre>{html.escape(str(details.get('prompt') or ''))}</pre>")
                elif details.get("prompt_preview"):
                    parts.append(f"<div class=\"flow-k\" style=\"margin-top:8px;\">prompt preview</div><pre>{html.escape(str(details.get('prompt_preview') or ''))}</pre>")
                parts.append("</details>")
            parts.append(_render_rejected_llm_attempts_html(details.get("rejected_attempts")))
            parts.append(_render_llm_causal_notes_html(details.get("causal_notes")))
            parts.append("<section class=\"nested-block\" style=\"margin:10px 0 0 0;\"><button class=\"nested-toggle\" type=\"button\">LLM → P4 Output</button><div class=\"nested-content\" style=\"padding:8px;\">")
            if details.get("analysis"):
                parts.append(f"<div class=\"flow-k\">analysis</div><pre>{html.escape(str(details.get('analysis') or ''))}</pre>")
            if "assistant_message" in details:
                parts.append(f"<div class=\"flow-k\">assistant_message</div><pre>{html.escape(str(details.get('assistant_message') or ''))}</pre>")
            if "tool_name" in details:
                parts.append(f"<div class=\"flow-k\">proposed action</div><div><span class=\"tool-pill\">{html.escape(str(details.get('tool_name')))}</span></div>")
            if "tool_args" in details:
                parts.append(f"<div class=\"flow-k\">tool_args</div><pre>{html.escape(json.dumps(details.get('tool_args'), ensure_ascii=False, indent=2))}</pre>")
            if isinstance(details.get("executed_tool"), dict):
                parts.append(_render_tool_execution_result_html(details["executed_tool"]))
            
            parts.append(f"<details style=\"margin-top:8px;\"><summary style=\"cursor:pointer; color:#9ec5ff;\">詳細・JSON表示</summary>")
            if "thinking_text" in details:
                parts.append(f"<div class=\"flow-k\" style=\"margin-top:8px;\">thinking</div><pre>{html.escape(str(details.get('thinking_text') or ''))}</pre>")
            if "final_text" in details:
                parts.append(f"<div class=\"flow-k\" style=\"margin-top:8px;\">raw json</div><pre>{html.escape(str(details.get('final_text') or ''))}</pre>")
            parts.append("</details>")
            parts.append("</div></section>")
            content = "".join(parts)
            label = "Agent LLM output"
            extra_class = " llm"
            
        elif card_type == "tool":
            parts.append(f"<div class=\"flow-k\">tool</div><pre>{html.escape(str(details.get('tool_name') or ''))}</pre>")
            if "tool_args" in details:
                parts.append(f"<div class=\"flow-k\">args</div><pre>{html.escape(json.dumps(details.get('tool_args'), ensure_ascii=False, indent=2))}</pre>")
            if "tool_result" in details:
                if details.get("tool_name") == "run_command":
                    parts.append(_render_command_result_html(details.get("tool_result")))
                else:
                    parts.append(f"<div class=\"flow-k\">result</div><pre>{html.escape(json.dumps(details.get('tool_result'), ensure_ascii=False, indent=2))}</pre>")
            content = "".join(parts)
            label = "Tool (consolidated)"
            extra_class = " tool"
            
        elif card_type == "finish":
            blocked = details.get("blocked") if isinstance(details.get("blocked"), dict) else {}
            blocked_details = blocked.get("details") if isinstance(blocked.get("details"), dict) else {}
            blocked_judge = blocked_details.get("judge") if isinstance(blocked_details.get("judge"), dict) else {}
            if "grounding_judge" in details and isinstance(details.get("grounding_judge"), dict):
                parts.append(_render_judge_details_html(details["grounding_judge"], "grounding judge details"))
            elif blocked_judge:
                parts.append(_render_judge_details_html(blocked_judge, "blocked judge details"))
            if blocked:
                b = blocked
                parts.append("<div class=\"flow-k\">P4 completion decision</div>")
                parts.append(f"<div>⚠ {html.escape(str(b.get('human_title') or b.get('code') or ''))}</div>")
                parts.append(f"<div>{html.escape(str(b.get('human_desc') or ''))}</div>")
                if b.get("reason_code"):
                    parts.append(f"<div class=\"flow-k\">blocked reason_code</div><pre>{html.escape(str(b.get('reason_code') or ''))}</pre>")
                parts.append(f"<pre>{html.escape(str(b.get('content') or ''))}</pre>")
            if "acceptance" in details:
                parts.append(f"<div class=\"flow-k\">acceptance</div><pre>{html.escape(json.dumps(details['acceptance'], ensure_ascii=False, indent=2))}</pre>")
            if "controller_finish" in details:
                parts.append(f"<div class=\"flow-k\">controller_finish</div><pre>{html.escape(json.dumps(details['controller_finish'], ensure_ascii=False, indent=2))}</pre>")
            content = "".join(parts)
            label = "P4 completion check"
            extra_class = " decision"
        else:
            content = f"<pre>{html.escape(str(item.get('content') or ''))}</pre>"
            label = "Generic"
            extra_class = ""
            
        return (
            f"<div class=\"flow-item{extra_class}\" style=\"margin-left:{indent}px\">"
            f"<div class=\"flow-label\">{depth_badge}{depth_meta}<span>{label}</span></div>"
            f"{_attach_flow_scroll_id('<div class=\"flow-content\">' + content + '</div>', scroll_id)}"
            "</div>"
        )

    if str(item.get("label") or "") == "tool_result":
        payload = item.get("parsed_payload")
        if isinstance(payload, dict) and tool_name == "run_command":
            return (
                f"<div class=\"flow-item\" style=\"margin-left:{indent}px\"><div class=\"flow-label\">{depth_badge}{depth_meta}<span>{label}</span></div>"
                f"{_render_command_result_html(payload)}"
                "</div>"
            )
    if str(item.get("label") or "") == "observer_note":
        return (
            f"<div class=\"flow-item observer_note\" style=\"margin-left:{indent}px\">"
            f"<div class=\"flow-label\">{depth_badge}{depth_meta}<span>{label}</span></div>"
            f"{_attach_flow_scroll_id(_render_commentator_content_html(content), scroll_id)}"
            "</div>"
        )
    if str(item.get("label") or "") == "system_note" and str(item.get("code") or "") == "llm_output_issue":
        return (
            f"<div class=\"flow-item llm_output_issue\" style=\"margin-left:{indent}px\">"
            f"<div class=\"flow-label\">{depth_badge}{depth_meta}<span>{label}</span></div>"
            f"{_attach_flow_scroll_id(_render_llm_output_issue_html(item), scroll_id)}"
            "</div>"
        )
    if str(item.get("label") or "") == "task_plan":
        return (
            f"<div class=\"flow-item task_plan\" style=\"margin-left:{indent}px\">"
            f"<div class=\"flow-label\">{depth_badge}{depth_meta}<span>{label}</span></div>"
            f"{_attach_flow_scroll_id(_render_task_plan_html(item), scroll_id)}"
            "</div>"
        )
    if str(item.get("label") or "") in {"problem_profile", "planner_decision", "plan_record", "plan_revision"}:
        return (
            f"<div class=\"flow-item {html.escape(str(item.get('label') or ''))}\" style=\"margin-left:{indent}px\">"
            f"<div class=\"flow-label\">{depth_badge}{depth_meta}<span>{label}</span></div>"
            f"{_attach_flow_scroll_id(_render_planner_event_html(item), scroll_id)}"
            "</div>"
        )
    if str(item.get("label") or "") == "runtime_event":
        return (
            f"<div class=\"flow-item runtime_event\" style=\"margin-left:{indent}px\">"
            f"<div class=\"flow-label\">{depth_badge}{depth_meta}<span>{label}</span></div>"
            f"{_attach_flow_scroll_id(_render_runtime_event_html(item), scroll_id)}"
            "</div>"
        )
    if str(item.get("label") or "") in {"llm", "tool", "decision", "observation"}:
        return (
            f"<div class=\"flow-item {html.escape(str(item.get('label') or ''))}{extra_class}\" style=\"margin-left:{indent}px\">"
            f"<div class=\"flow-label\">{depth_badge}{depth_meta}<span>{label}</span></div>"
            f"{_attach_flow_scroll_id(_render_canonical_event_html(item), scroll_id)}"
            "</div>"
        )
    if str(item.get("label") or "") == "frame":
        return (
            f"<div class=\"flow-item frame\" style=\"margin-left:{indent}px\">"
            f"<div class=\"flow-label\">{depth_badge}{depth_meta}<span>{label}</span></div>"
            f"{_attach_flow_scroll_id(_render_canonical_frame_html(item), scroll_id)}"
            "</div>"
        )
    if str(item.get("label") or "") == "live_stream":
        return (
            f"<div class=\"flow-item live_stream\" style=\"margin-left:{indent}px\">"
            f"<div class=\"flow-label\">{depth_badge}{depth_meta}<span>{label}</span></div>"
            f"{_attach_flow_scroll_id('<div class=\"flow-content\">' + _render_live_output_html(content) + '</div>', scroll_id)}"
            "</div>"
        )
    if str(item.get("label") or "") in {"frame_opened", "frame_returned", "child_return"}:
        return (
            f"<div class=\"flow-item {html.escape(str(item.get('label') or ''))}\" style=\"margin-left:{indent}px\">"
            f"<div class=\"flow-label\">{depth_badge}{depth_meta}<span>{label}</span></div>"
            f"{_attach_flow_scroll_id(_render_frame_flow_item_html(item), scroll_id)}"
            "</div>"
        )
    return (
        f"<div class=\"flow-item {html.escape(str(item.get('label') or ''))}{extra_class}\" style=\"margin-left:{indent}px\">"
        f"<div class=\"flow-label\">{depth_badge}{depth_meta}<span>{label}</span></div>"
        f"<div class=\"flow-content\"{_flow_content_scroll_attr(scroll_id)}><pre>{html.escape(content)}</pre></div>"
        "</div>"
    )

def _render_llm_output_issue_html(item: dict[str, Any]) -> str:
    details = item.get("details") if isinstance(item.get("details"), dict) else {}
    thinking = str((details or {}).get("thinking_text") or "")
    raw = str((details or {}).get("raw_text") or "")
    combined = str((details or {}).get("combined_text") or "")
    metadata = (details or {}).get("stream_metadata") or {}
    meta_text = json.dumps(metadata, ensure_ascii=False, indent=2) if metadata else ""
    parts = [f"<pre>{html.escape(str(item.get('content') or ''))}</pre>"]
    if thinking:
        parts.append(f"<div class=\"flow-k\">thinking</div><pre>{html.escape(thinking)}</pre>")
    if raw:
        parts.append(f"<div class=\"flow-k\">content</div><pre>{html.escape(raw)}</pre>")
    if combined and not thinking and not raw:
        parts.append(f"<div class=\"flow-k\">combined</div><pre>{html.escape(combined)}</pre>")
    if meta_text:
        parts.append(f"<div class=\"flow-k\">stream metadata</div><pre>{html.escape(meta_text)}</pre>")
    return f"<div class=\"flow-content\">{''.join(parts)}</div>"

def _render_runtime_event_html(item: dict[str, Any]) -> str:
    details = item.get("details") if isinstance(item.get("details"), dict) else {}
    parts: list[str] = []
    event_name = str(item.get("event_name") or "")
    if event_name:
        parts.append(f"<div class=\"flow-k\">event</div><pre>{html.escape(event_name)}</pre>")
    tool_name = str((details or {}).get("tool_name") or "")
    if tool_name:
        parts.append(f"<div class=\"flow-k\">tool</div><pre>{html.escape(tool_name)}</pre>")
    if "ok" in details:
        parts.append(f"<div class=\"flow-k\">ok</div><pre>{html.escape(str(details.get('ok')))}</pre>")
    model = str((details or {}).get("model") or "")
    if model:
        parts.append(f"<div class=\"flow-k\">model</div><pre>{html.escape(model)}</pre>")
    if "attempt_count" in details:
        parts.append(f"<div class=\"flow-k\">attempt</div><pre>{html.escape(str(details.get('attempt_count')))}</pre>")
    parse_issue = str((details or {}).get("parse_issue") or "")
    if parse_issue:
        parts.append(f"<div class=\"flow-k\">parse issue</div><pre>{html.escape(parse_issue)}</pre>")
    thinking = str((details or {}).get("thinking_text") or "")
    if thinking:
        parts.append(f"<div class=\"flow-k\">thinking</div><pre>{html.escape(thinking)}</pre>")
    content = str((details or {}).get("content_text") or "")
    if content:
        parts.append(f"<div class=\"flow-k\">content</div><pre>{html.escape(content)}</pre>")
    partial = (details or {}).get("partial")
    if isinstance(partial, dict):
        parts.append(f"<div class=\"flow-k\">partial</div><pre>{html.escape(json.dumps(partial, ensure_ascii=False, indent=2))}</pre>")
    tool_args = (details or {}).get("tool_args")
    if isinstance(tool_args, dict):
        parts.append(f"<div class=\"flow-k\">tool args</div><pre>{html.escape(json.dumps(tool_args, ensure_ascii=False, indent=2))}</pre>")
    tool_result = (details or {}).get("tool_result")
    if isinstance(tool_result, dict):
        parts.append(f"<div class=\"flow-k\">tool result</div><pre>{html.escape(json.dumps(tool_result, ensure_ascii=False, indent=2))}</pre>")
    if not parts:
        parts.append(f"<pre>{html.escape(str(item.get('content') or ''))}</pre>")
    return f"<div class=\"flow-content\">{''.join(parts)}</div>"

def _render_canonical_event_html(item: dict[str, Any]) -> str:
    details = item.get("details") if isinstance(item.get("details"), dict) else {}
    parts: list[str] = [f"<div class=\"flow-k\">status</div><pre>{html.escape(str(item.get('status') or ''))}</pre>"]
    label = str(item.get("label") or "")
    if label == "llm":
        event_name = str((details or {}).get("event_name") or "")
        if event_name:
            parts.append(f"<div class=\"flow-k\">event</div><pre>{html.escape(event_name)}</pre>")
        model = str((details or {}).get("model") or "")
        if model:
            parts.append(f"<div class=\"flow-k\">model</div><pre>{html.escape(model)}</pre>")
        parse_issue = str((details or {}).get("parse_issue") or "")
        if parse_issue:
            parts.append(f"<div class=\"flow-k\">parse issue</div><pre>{html.escape(parse_issue)}</pre>")
        thinking = str((details or {}).get("thinking_text") or "")
        if thinking:
            parts.append(f"<div class=\"flow-k\">thinking</div><pre>{html.escape(thinking)}</pre>")
        content_text = str((details or {}).get("content_text") or "")
        if content_text:
            parts.append(f"<div class=\"flow-k\">content</div><pre>{html.escape(content_text)}</pre>")
        if not thinking and not content_text:
            parts.append(f"<pre>{html.escape(str(item.get('content') or ''))}</pre>")
    elif label == "tool":
        tool_name = str(item.get("tool_name") or "")
        if tool_name:
            parts.append(f"<div class=\"flow-k\">tool</div><pre>{html.escape(tool_name)}</pre>")
        tool_args = item.get("tool_args") if isinstance(item.get("tool_args"), dict) else {}
        if tool_args:
            parts.append(f"<div class=\"flow-k\">tool args</div><pre>{html.escape(json.dumps(tool_args, ensure_ascii=False, indent=2))}</pre>")
        tool_result = (details or {}).get("result") or (details or {}).get("tool_result") or item.get("parsed_payload")
        if isinstance(tool_result, dict):
            parts.append(f"<div class=\"flow-k\">tool result</div><pre>{html.escape(json.dumps(tool_result, ensure_ascii=False, indent=2))}</pre>")
        elif str(item.get("content") or ""):
            parts.append(f"<pre>{html.escape(str(item.get('content') or ''))}</pre>")
    elif label == "decision":
        if str(item.get("status") or "") == "blocked":
            parts.append(_render_blocked_decision_details_html(item, details))
        code = str(item.get("code") or "")
        if code:
            parts.append(f"<div class=\"flow-k\">decision</div><pre>{html.escape(code)}</pre>")
        reason = str(item.get("reason_code") or "")
        if reason:
            parts.append(f"<div class=\"flow-k\">reason</div><pre>{html.escape(reason)}</pre>")
        rationale = str((details or {}).get("rationale") or "")
        if rationale:
            parts.append(f"<div class=\"flow-k\">rationale</div><pre>{html.escape(rationale)}</pre>")
        tasks = (details or {}).get("tasks")
        if isinstance(tasks, list):
            parts.append(f"<div class=\"flow-k\">tasks</div><pre>{html.escape(json.dumps(tasks, ensure_ascii=False, indent=2))}</pre>")
        strategy = str((details or {}).get("strategy") or "")
        if strategy:
            parts.append(f"<div class=\"flow-k\">strategy</div><pre>{html.escape(strategy)}</pre>")
        profile = (details or {}).get("profile")
        if isinstance(profile, dict) and profile:
            parts.append(f"<div class=\"flow-k\">problem profile</div><pre>{html.escape(json.dumps(profile, ensure_ascii=False, indent=2))}</pre>")
        plan = (details or {}).get("plan")
        if isinstance(plan, dict) and plan:
            parts.append(f"<div class=\"flow-k\">plan record</div><pre>{html.escape(json.dumps(plan, ensure_ascii=False, indent=2))}</pre>")
        work_units = (details or {}).get("work_units")
        if isinstance(work_units, list) and work_units:
            parts.append(f"<div class=\"flow-k\">work units</div><pre>{html.escape(json.dumps(work_units, ensure_ascii=False, indent=2))}</pre>")
        verification_contract = (details or {}).get("verification_contract")
        if verification_contract:
            parts.append(f"<div class=\"flow-k\">verification contract</div><pre>{html.escape(json.dumps(verification_contract, ensure_ascii=False, indent=2))}</pre>")
        parts.append(f"<div class=\"flow-k\">message</div><pre>{html.escape(str(item.get('content') or ''))}</pre>")
    elif label == "observation":
        source = str(item.get("code") or "")
        if source:
            parts.append(f"<div class=\"flow-k\">source</div><pre>{html.escape(source)}</pre>")
        inner = (details or {}).get("details")
        if isinstance(inner, dict):
            parts.append(f"<div class=\"flow-k\">details</div><pre>{html.escape(json.dumps(inner, ensure_ascii=False, indent=2))}</pre>")
        parts.append(f"<div class=\"flow-k\">summary</div><pre>{html.escape(str(item.get('content') or ''))}</pre>")
    return f"<div class=\"flow-content\">{''.join(parts)}</div>"

def _render_blocked_decision_details_html(item: dict[str, Any], details: dict[str, Any]) -> str:
    inner = details.get("details") if isinstance(details.get("details"), dict) else {}
    title = str(item.get("human_title") or details.get("human_title") or "ブロック")
    desc = str(item.get("human_desc") or details.get("human_desc") or "")
    reason = str(item.get("reason_code") or details.get("reason_code") or "")
    code = str(item.get("code") or details.get("decision_type") or "")
    parts = [
        "<div class=\"blocked-reason\" style=\"margin:0 0 10px 0;\">",
        f"<strong>{html.escape(title)}</strong>",
    ]
    if desc:
        parts.append(f"<pre>{html.escape(desc)}</pre>")
    if code or reason:
        parts.append("<div class=\"flow-k\">runtime判定</div>")
        parts.append(f"<pre>{html.escape(' / '.join(part for part in [code, reason] if part))}</pre>")
    blocked_tool = str(inner.get("blocked_tool") or "")
    if blocked_tool:
        parts.append("<div class=\"flow-k\">拒否されたLLM提案</div>")
        parts.append(f"<pre>{html.escape(blocked_tool)}</pre>")
    issues = inner.get("issues")
    if isinstance(issues, list) and issues:
        issue_text = "\n".join(f"- {str(issue)}" for issue in issues)
        parts.append("<div class=\"flow-k\">具体的な不足・違反</div>")
        parts.append(f"<pre>{html.escape(issue_text)}</pre>")
    required = inner.get("required_contract")
    if isinstance(required, list) and required:
        required_text = "\n".join(f"- {str(value)}" for value in required)
        parts.append("<div class=\"flow-k\">必要な契約項目</div>")
        parts.append(f"<pre>{html.escape(required_text)}</pre>")
    suggested = str(inner.get("suggested_fix") or "")
    if suggested:
        parts.append("<div class=\"flow-k\">次に直すべきこと</div>")
        parts.append(f"<pre>{html.escape(suggested)}</pre>")
    allowed = inner.get("allowed_next_actions")
    if isinstance(allowed, list) and allowed:
        parts.append("<details style=\"margin-top:8px;\"><summary style=\"cursor:pointer; color:#9ec5ff;\">許可される次アクション</summary>")
        parts.append(f"<pre>{html.escape(json.dumps(allowed, ensure_ascii=False, indent=2))}</pre></details>")
    parts.append("</div>")
    return "".join(parts)

def _render_canonical_frame_html(item: dict[str, Any]) -> str:
    details = item.get("details") if isinstance(item.get("details"), dict) else {}
    status = str(item.get("status") or "")
    if status == "opened":
        body = (
            "open child frame\n"
            f"parent: {details.get('parent_frame_id') or 'root'}\n"
            f"child: {details.get('frame_id') or '-'}\n"
            f"depth: {details.get('depth') or 0}\n"
            f"goal: {_short_text(str(details.get('goal') or '-'), 320)}"
        )
    else:
        payload = details.get("return_payload") if isinstance(details.get("return_payload"), dict) else {}
        findings_raw = (payload or {}).get("findings") or []
        findings = " / ".join(str(value) for value in findings_raw) if isinstance(findings_raw, list) else str(findings_raw)
        body = (
            "return to parent\n"
            f"child: {details.get('frame_id') or '-'}\n"
            f"parent: {details.get('parent_frame_id') or 'root'}\n"
            f"depth: {details.get('depth') or 0}\n"
            f"summary: {_short_text(str((payload or {}).get('summary') or item.get('content') or ''), 320)}\n"
            f"findings: {_short_text(findings or '-', 320)}"
        )
    return f"<div class=\"flow-content\"><pre>{html.escape(body)}</pre></div>"

def _render_task_plan_html(item: dict[str, Any]) -> str:
    parts = [f"<div class=\"flow-k\">summary</div><pre>{html.escape(str(item.get('content') or ''))}</pre>"]
    rationale = str(item.get("rationale") or "")
    if rationale:
        parts.append(f"<div class=\"flow-k\">rationale</div><pre>{html.escape(rationale)}</pre>")
    tasks = item.get("tasks") if isinstance(item.get("tasks"), list) else []
    for index, task in enumerate(tasks, start=1):
        parts.append(f"<div class=\"flow-k\">task {index}</div><pre>{html.escape(json.dumps(task, ensure_ascii=False, indent=2))}</pre>")
    return f"<div class=\"flow-content\">{''.join(parts)}</div>"

def _render_planner_event_html(item: dict[str, Any]) -> str:
    parts = [f"<div class=\"flow-k\">summary</div><pre>{html.escape(str(item.get('content') or ''))}</pre>"]
    strategy = str(item.get("strategy") or "")
    if strategy:
        parts.append(f"<div class=\"flow-k\">strategy</div><pre>{html.escape(strategy)}</pre>")
    profile = item.get("profile") if isinstance(item.get("profile"), dict) else {}
    if profile:
        parts.append(f"<div class=\"flow-k\">problem profile</div><pre>{html.escape(json.dumps(profile, ensure_ascii=False, indent=2))}</pre>")
    plan = item.get("plan") if isinstance(item.get("plan"), dict) else {}
    if plan:
        parts.append(f"<div class=\"flow-k\">plan record</div><pre>{html.escape(json.dumps(plan, ensure_ascii=False, indent=2))}</pre>")
    work_units = item.get("work_units") if isinstance(item.get("work_units"), list) else []
    if work_units:
        parts.append(f"<div class=\"flow-k\">work units</div><pre>{html.escape(json.dumps(work_units, ensure_ascii=False, indent=2))}</pre>")
    verification_contract = item.get("verification_contract")
    if verification_contract:
        parts.append(f"<div class=\"flow-k\">verification contract</div><pre>{html.escape(json.dumps(verification_contract, ensure_ascii=False, indent=2))}</pre>")
    details = item.get("details") if isinstance(item.get("details"), dict) else {}
    if details:
        parts.append(f"<div class=\"flow-k\">details</div><pre>{html.escape(json.dumps(details, ensure_ascii=False, indent=2))}</pre>")
    return f"<div class=\"flow-content\">{''.join(parts)}</div>"

def _render_frame_flow_item_html(item: dict[str, Any]) -> str:
    payload = item.get("return_payload") if isinstance(item.get("return_payload"), dict) else {}
    summary = str((payload or {}).get("summary") or item.get("content") or "")
    findings_raw = (payload or {}).get("findings") or []
    findings = " / ".join(str(value) for value in findings_raw) if isinstance(findings_raw, list) else str(findings_raw)
    label = str(item.get("label") or "")
    if label == "frame_opened":
        body = (
            "open child frame\n"
            f"parent: {item.get('parent_frame_id') or 'root'}\n"
            f"child: {item.get('frame_id') or '-'}\n"
            f"goal: {_short_text(str(item.get('goal') or item.get('content') or '-'), 320)}"
        )
    elif label == "frame_returned":
        body = (
            "return to parent\n"
            f"child: {item.get('frame_id') or '-'}\n"
            f"parent: {item.get('parent_frame_id') or 'root'}\n"
            f"summary: {_short_text(summary, 320)}\n"
            f"findings: {_short_text(findings or '-', 320)}"
        )
    else:
        body = (
            "child result received\n"
            f"child: {item.get('child_frame_id') or '-'}\n"
            f"summary: {_short_text(summary, 320)}\n"
            f"findings: {_short_text(findings or '-', 320)}"
        )
    return f"<div class=\"flow-content\"><pre>{html.escape(body)}</pre></div>"

def _extract_json_object(text: str) -> str | None:
    start = text.find("{")
    if start < 0:
        return None
    depth = 0
    for index in range(start, len(text)):
        char = text[index]
        if char == "{":
            depth += 1
        elif char == "}":
            depth -= 1
            if depth == 0:
                return text[start : index + 1]
    return None

def _render_live_output_html(text: str) -> str:
    clean = str(text or "")
    if not clean:
        return "<div class=\"flow-empty\">まだ出力はありません。</div>"
    if clean.startswith("Waiting for model response"):
        return "<div class=\"live-state waiting\"><div class=\"flow-k\">status</div><pre>Waiting for model response...</pre></div>"
    if clean.startswith("Running command via "):
        return f"<div class=\"live-state running\"><div class=\"flow-k\">status</div><pre>{html.escape(clean)}</pre></div>"
    if clean.startswith("[thinking]"):
        body = clean[len("[thinking]") :].lstrip("\n")
        return f"<div class=\"flow-content llm-thinking\"><div class=\"flow-k\">thinking stream</div><pre>{html.escape(body)}</pre></div>"
    if "[content]" in clean:
        thinking, content = clean.split("[content]", 1)
        thinking = thinking.replace("[thinking]", "").strip()
        content = content.strip()
        parts = []
        if thinking:
            parts.append(f"<div class=\"flow-k\">thinking stream</div><pre>{html.escape(thinking)}</pre>")
        parts.append(f"<div class=\"flow-k\">content stream</div><pre>{html.escape(content)}</pre>")
        return f"<div class=\"flow-content llm-thinking\">{''.join(parts)}</div>"
    candidate = _extract_json_object(clean)
    if candidate is not None:
        try:
            payload = json.loads(candidate)
        except json.JSONDecodeError:
            payload = None
        if isinstance(payload, dict) and str(payload.get("tool") or "") == "run_command":
            return _render_command_result_html(payload)
    return f"<pre>{html.escape(clean)}</pre>"

def _render_command_result_html(payload: dict[str, Any]) -> str:
    meta_rows = [
        ("command", payload.get("command")),
        ("shell", payload.get("shell")),
        ("cwd", payload.get("cwd")),
        ("returncode", payload.get("returncode")),
        ("duration_ms", payload.get("duration_ms")),
        ("active_stream", payload.get("active_stream")),
    ]
    meta = "".join(
        f"<span class=\"tool-pill path-text\"><strong>{html.escape(str(key))}:</strong> {html.escape(_short_text(_short_path(str(value)), 180))}</span>"
        for key, value in meta_rows
        if value not in {None, ""}
    )
    parts = [f"<div class=\"tool-result-meta\">{meta}</div>" if meta else ""]
    stdout = payload.get("stdout")
    stderr = payload.get("stderr")
    error = payload.get("error")
    if stdout not in {None, ""}:
        parts.append(
            "<div class=\"tool-stream-block\">"
            "<div class=\"flow-k\">stdout</div>"
            f"<pre>{html.escape(_short_text(str(stdout), 1200))}</pre>"
            "</div>"
        )
    if stderr not in {None, ""}:
        parts.append(
            "<div class=\"tool-stream-block stderr\">"
            "<div class=\"flow-k\">stderr</div>"
            f"<pre>{html.escape(_short_text(str(stderr), 1200))}</pre>"
            "</div>"
        )
    if error not in {None, ""}:
        parts.append(
            "<div class=\"tool-stream-block stderr\">"
            "<div class=\"flow-k\">error</div>"
            f"<pre>{html.escape(_short_text(str(error), 1200))}</pre>"
            "</div>"
        )
    return f"<div class=\"tool-result-card\">{''.join(parts)}</div>"

def _short_text(value: str, limit: int = 900) -> str:
    text = str(value or "")
    if len(text) <= limit:
        return text
    return f"{text[:limit]}\n... ({len(text) - limit} chars hidden)"

def _short_path(value: str) -> str:
    return str(value or "").replace("/private/tmp/", "/tmp/")

def _latest_result_text(snapshot: dict[str, Any]) -> str:
    latest = snapshot.get("latest_result") if isinstance(snapshot, dict) else {}
    if isinstance(latest, dict):
        body = str(latest.get("body") or "")
        if body:
            if "LLM output did not satisfy machine-control schema: json_extraneous_text" in body:
                return "⚠ machine-control JSON形式違反\nJSONの外側にMarkdownや前置きがあります。\n\n[詳細]\n" + body
            return body
        summary = str(latest.get("summary") or "")
        if summary:
            return summary
    return "直近結果はありません。"


def _latest_result_summary(snapshot: dict[str, Any]) -> str:
    latest = snapshot.get("latest_result") if isinstance(snapshot, dict) else {}
    session = snapshot.get("session") if isinstance(snapshot, dict) else {}
    if isinstance(latest, dict) and latest.get("summary"):
        return str(latest.get("summary"))
    if isinstance(session, dict):
        return str(session.get("last_assistant_message") or "").strip()
    return ""

def _commentator_rows(text: str) -> list[tuple[str, str]]:
    rows: list[tuple[str, str]] = []
    for raw_line in str(text or "").splitlines():
        line = raw_line.strip()
        if not line:
            continue
        cleaned = re.sub(r"^\d+[\.．、:]\s*", "", line)
        match = re.match(r"^([^:：]{2,22})[:：]\s*(.*)$", cleaned)
        if match:
            rows.append((match.group(1), match.group(2)))
        else:
            rows.append(("解説", cleaned))
    return rows or [("解説", "まだ解説本文はありません。")]

def _render_commentator_content_html(text: str) -> str:
    rows = "".join(
        "<div class=\"commentator-line\">"
        f"<strong>{html.escape(label)}</strong>"
        f"<span>{html.escape(body)}</span>"
        "</div>"
        for label, body in _commentator_rows(text)
    )
    return f"<div class=\"commentator-body\">{rows}</div>"

def _contract_required_text(progress: dict[str, Any]) -> str:
    required = progress.get("required_contract") if isinstance(progress, dict) else []
    if isinstance(required, list) and required:
        return ", ".join(str(item) for item in required)
    return "未確定"

def _contract_metric_text(progress: dict[str, Any], key: str) -> str:
    required = progress.get("required_contract") if isinstance(progress, dict) else []
    raw = str(progress.get(key) or "no") if isinstance(progress, dict) else "no"
    if isinstance(required, list) and required:
        required_set = {str(item) for item in required}
        if key not in required_set:
            return "not required"
        return "satisfied" if raw == "yes" else "missing"
    return raw

def render_dashboard_html(snapshot: dict[str, Any]) -> str:
    runtime = snapshot.get("runtime") or {}
    model = snapshot.get("model") or "gemma4:26b"
    available_models = snapshot.get("available_models") or [model]
    operations = snapshot.get("recent_operations") or []

    esc = html.escape
    def _op_row(op: dict[str, Any], index: int) -> str:
        op_id = str(op.get("operation_id") or "")
        status = str(op.get("status") or "idle")
        title = str(op.get("title") or "Operation")
        detail = str(op.get("detail") or "...")
        started_at = str(op.get("started_at") or "")
        output_preview = str(op.get("output_preview") or "")
        blocked_reason = str(op.get("blocked_reason") or "")
        flow_html = _render_flow_steps_html(op.get("flow_steps") or [], op_id=op_id)
        blocked_html = (
            f"<div class=\"blocked-reason\"><strong>ブロック理由</strong><pre>{esc(blocked_reason)}</pre></div>"
            if blocked_reason
            else ""
        )

        return (
            f"<section class=\"operation-card {esc(status)}\" data-operation-id=\"{esc(op_id)}\">"
            f"<button class=\"operation-head\" onclick=\"toggleOperation('{esc(op_id)}')\">"
            f"<strong>{esc(title)}</strong>"
            f"<span class=\"operation-status {esc(status)}\">{esc(status)}</span>"
            f"</button>"
            f"<div class=\"bubble-meta\">{esc(started_at)}</div>"
            f"<div class=\"operation-body {'closed' if index != 0 else ''}\">"
            f"<pre class=\"operation-detail\" style=\"padding: 12px;\">{esc(_short_text(detail, 700))}</pre>"
            f"{blocked_html}"
            f"<div class=\"flow-container\" id=\"flow-{esc(op_id)}\" style=\"padding: 0 12px 12px 12px;\">"
            f"<div class=\"flow-label\"><span class=\"depth-badge\">FLOW</span><span>階層フロー</span></div>"
            f"{flow_html}</div>"
            f"<div class=\"nested-block\">"
            f"<button class=\"nested-toggle\" onclick=\"toggleNested('{esc(op_id)}:live')\">ライブ出力</button>"
            f"<div class=\"nested-content closed\" data-nested-id=\"{esc(op_id)}:live\">"
            f"<div class=\"operation-output\">{_render_live_output_html(output_preview)}</div>"
            f"</div>"
            f"</div>"
            f"</div>"
            f"</section>"
        )

    operation_rows = "".join(_op_row(op, index) for index, op in enumerate(operations)) or "<div>まだ実行操作はありません。</div>"

    selected_model = str(runtime.get("current_model") or model)
    model_options = "".join(f"<option value=\"{esc(m)}\"{' selected' if m == selected_model else ''}>{esc(m)}</option>" for m in available_models)

    res = _DASHBOARD_TPL.replace("__STATUS__", esc(str(runtime.get("status") or "idle")))
    res = res.replace("__MODEL__", esc(str(runtime.get("current_model") or model)))
    last_llm_parts = [f"{runtime.get('last_llm_duration_ms') or '-'}ms"]
    if runtime.get("last_llm_parse_issue"):
        last_llm_parts.append(f"失敗分類: {runtime.get('last_llm_parse_issue')}")
    metadata = runtime.get("last_llm_stream_metadata") or {}
    if isinstance(metadata, dict) and metadata.get("done_reason"):
        last_llm_parts.append(f"done: {metadata.get('done_reason')}")
    res = res.replace("__LAST_LLM__", esc(" / ".join(str(part) for part in last_llm_parts)))
    res = res.replace("__LLM_WORKSPACE__", esc(str(runtime.get("current_llm_workspace") or runtime.get("last_llm_workspace") or "-")))
    judge = snapshot.get("judge_metrics") if isinstance(snapshot.get("judge_metrics"), dict) else {}
    judge_text = (
        f"blocks={judge.get('consecutive_finish_blocks') or 0} / "
        f"last={judge.get('last_judge_decision') or '-'} / "
        f"retries={judge.get('judge_retry_count') or 0} / "
        f"fallback={'yes' if judge.get('fallback_used') else 'no'}"
    )
    res = res.replace("__JUDGE_METRICS__", esc(judge_text))
    res = res.replace("__MODEL_OPTIONS__", model_options)
    res = res.replace("__OP_COUNT__", str(len(operations)))
    res = res.replace("__OP_ROWS__", operation_rows)
    latest_summary = _latest_result_summary(snapshot)
    res = res.replace(
        "__LATEST_RESULT_META__",
        esc(f"直近: {latest_summary}" if latest_summary else "直近結果はありません。"),
    )
    res = res.replace("__LATEST_RESULT__", esc(_short_text(_latest_result_text(snapshot), 4000)))
    
    contract_progress = snapshot.get("contract_progress", {}) if isinstance(snapshot.get("contract_progress"), dict) else {}
    res = res.replace("__CONTRACT_STATE__", esc(str(contract_progress.get("contract_state", "unknown"))))
    res = res.replace("__CONTRACT_REQUIRED__", esc(_contract_required_text(contract_progress)))
    res = res.replace("__CONTRACT_ARTIFACT__", esc(_contract_metric_text(contract_progress, "artifact_written")))
    res = res.replace("__CONTRACT_COMMAND__", esc(_contract_metric_text(contract_progress, "command_executed")))
    res = res.replace("__CONTRACT_STDOUT__", esc(_contract_metric_text(contract_progress, "stdout_displayed")))
    res = res.replace("__CONTRACT_RESULT__", esc(str(contract_progress.get("result_selected_for_user", "no"))))
    res = res.replace("__SNAPSHOT_JSON__", "null")

    return res
