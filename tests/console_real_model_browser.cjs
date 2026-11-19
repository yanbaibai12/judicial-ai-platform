// Mutates only an explicitly isolated local acceptance instance.
const assert = require("node:assert/strict");
const { spawn } = require("node:child_process");
const crypto = require("node:crypto");
const fs = require("node:fs");
const os = require("node:os");
const path = require("node:path");
const waitJob = require("./console_acceptance_job.cjs");

const base = process.env.JAI_CONSOLE_TEST_URL;
const admin = process.env.JAI_CONSOLE_ADMIN_TOKEN;
if (process.env.JAI_CONSOLE_TEST_ISOLATED !== "1" || !admin ||
    !base || !["localhost", "127.0.0.1", "::1"].includes(new URL(base).hostname)) {
  throw new Error("An isolated loopback instance and admin token are required");
}
const source = "https://www.court.gov.cn/fabu/xiangqing/508001.html";
const expectedHash = "82220337e5235b49c11078498ec4ac8ab009e1b23607944b8330b42f5ae20345";
const objectPath = process.env.JAI_COURT_SNAPSHOT || path.join("data", "public-console-aeb40f70", "objects",
  "tenant-381022968706433024/kb-381022972003155968/document-381022978672099328/" +
  "version-381022978672099329", expectedHash);
const text = fs.readFileSync(objectPath, "utf8");
assert.equal(crypto.createHash("sha256").update(Buffer.from(text)).digest("hex"), expectedHash);
const pause = (ms) => new Promise((resolve) => setTimeout(resolve, ms));

async function main() {
  const profile = fs.mkdtempSync(path.join(os.tmpdir(), "jai-real-browser-"));
  const browser = spawn(process.env.CHROME_PATH ||
    "C:\\Program Files\\Google\\Chrome\\Application\\chrome.exe", [
    "--headless", "--disable-gpu", "--no-first-run",
    "--remote-debugging-port=0", `--user-data-dir=${profile}`, "about:blank",
  ], { stdio: "ignore", windowsHide: true });
  let socket;
  try {
    let port;
    for (let i = 0; i < 100 && !port; i++) {
      try { port = Number(fs.readFileSync(path.join(profile, "DevToolsActivePort"), "utf8").split("\n")[0]); } catch {}
      if (browser.exitCode !== null) throw new Error("Chrome exited");
      if (!port) await pause(100);
    }
    assert.ok(port, "Chrome debugging port unavailable");
    const target = await fetch(`http://127.0.0.1:${port}/json/new?${encodeURIComponent(base)}`,
      { method: "PUT" }).then((response) => response.json());
    socket = new WebSocket(target.webSocketDebuggerUrl);
    await new Promise((resolve, reject) => {
      socket.addEventListener("open", resolve, { once: true });
      socket.addEventListener("error", reject, { once: true });
    });
    let seq = 0;
    const pending = new Map();
    socket.addEventListener("message", (event) => {
      const message = JSON.parse(event.data);
      if (!message.id || !pending.has(message.id)) return;
      const { resolve, reject } = pending.get(message.id);
      pending.delete(message.id);
      message.error ? reject(new Error(message.error.message)) : resolve(message.result);
    });
    const send = (method, params = {}) => {
      const id = ++seq;
      socket.send(JSON.stringify({ id, method, params }));
      return new Promise((resolve, reject) => pending.set(id, { resolve, reject }));
    };
    async function evaluate(expression) {
      const result = await send("Runtime.evaluate", { expression, returnByValue: true, awaitPromise: true });
      if (result.exceptionDetails) throw new Error(result.exceptionDetails.exception?.description || result.exceptionDetails.text);
      return result.result.value;
    }
    async function waitFor(expression, seconds = 30) {
      for (let i = 0; i < seconds * 5; i++) {
        if (await evaluate(expression)) return;
        await pause(200);
      }
      throw new Error(`Timed out waiting for ${expression}`);
    }
    await send("Runtime.enable");
    await waitFor("document.querySelector('#connectionLabel') && !document.body.classList.contains('initializing')");
    // All setup requests originate in Chrome; large database IDs retain their exact decimal text.
    await evaluate(`window.acceptance = {
      admin: ${JSON.stringify(admin)},
      parse: (s) => JSON.parse(s, (k, v, c) =>
        typeof v === "number" && Number.isInteger(v) && !Number.isSafeInteger(v) ? c.source : v),
      async call(method, url, body, token = this.admin, expected = 200) {
        const response = await fetch(url, {
          method, headers: {Authorization: "Bearer " + token, "Content-Type": "application/json"},
          ...(body === undefined ? {} : {body: JSON.stringify(body)})
        });
        const raw = await response.text();
        if (!(Array.isArray(expected) ? expected : [expected]).includes(response.status))
          throw new Error(method + " " + url + ": " + response.status + " " + raw.slice(0, 500));
        return this.parse(raw);
      },
      async waitJob(id, token) {
        return (${waitJob.toString()})(this.call.bind(this), id, token);
      }
    }`);
    const setup = await evaluate(`(async () => {
      const a = window.acceptance, suffix = Date.now().toString();
      const tenant = (await a.call("POST", "/api/v1/admin/tenants",
        {code: "browser-real-" + suffix, name: "浏览器真实模型验收", type: "PROJECT"}, a.admin, 201)).data;
      const app = (await a.call("POST", "/api/v1/admin/apps", {
        tenant_id: tenant.id, code: "browser-real-" + suffix, name: "验收应用",
        permissions: ["app:invoke", "kb:read", "kb:write", "kb:approve", "kb:promote",
          "governance:read", "governance:write", "job:read"]
      }, a.admin, 201)).data;
      a.token = (await a.call("POST", "/api/v1/admin/apps/" + app.id + "/credentials", {}, a.admin, 201)).data.api_key;
      const reader = (await a.call("POST", "/api/v1/admin/apps", {
        tenant_id: tenant.id, code: "browser-reader-" + suffix, name: "验收只读应用",
        permissions: ["app:invoke", "kb:read"]
      }, a.admin, 201)).data;
      a.reader = (await a.call("POST", "/api/v1/admin/apps/" + reader.id + "/credentials",
        {}, a.admin, 201)).data.api_key;
      const kb = (await a.call("POST", "/api/v1/kbs",
        {code: "browser-real-" + suffix, name: "公开司法材料验收", security_level: "PUBLIC"},
        a.token, 201)).data;
      a.kb = kb.id;
      return {tenant: tenant.id, app: app.id, reader: reader.id, kb: kb.id};
    })()`);
    console.log("Isolated tenant/app/knowledge base created:", setup);
    await evaluate(`document.querySelector('[data-open-token]').click()`);
    await evaluate(`document.querySelector('#adminTokenInput').value = window.acceptance.admin`);
    await evaluate(`document.querySelector('#appTokenInput').value = window.acceptance.token`);
    await evaluate("document.querySelector('#tokenForm').requestSubmit()");
    await waitFor("document.querySelector('#connectionLabel').textContent === '已连接'", 90);
    const doc = await evaluate(`(async () => {
      const a = window.acceptance;
      return (await a.call("POST", "/api/v1/kbs/" + a.kb + "/documents", {
        title: "最高人民法院关于适用生态环境法典时间效力的若干规定",
        text: ${JSON.stringify(text)}, doc_type: "public_judicial_material",
        source: "supreme_peoples_court", source_ref: ${JSON.stringify(source)}, security_level: "PUBLIC",
        metadata: {published_at: "2026-08-06 16:15:58", fetched_at: "2026-11-17T10:09:14.226863+00:00",
          source_sha256: "717af9ff74e464c1d94b06e25900648a8af96ca8ebbd31603e3dab8360cf78ac",
          legal_effect_status: "UNVERIFIED"}
      }, a.token, 201)).data;
    })()`);
    assert.ok(doc.index_job_id, "automatic indexing was not submitted");
    const index = await evaluate(`(async () => {
      const a = window.acceptance;
      const job = await a.waitJob(${JSON.stringify(doc.index_job_id)}, a.token);
      const idx = job.result.index;
      if (idx.status !== "PENDING_APPROVAL") throw new Error("index approval was not required");
      await a.call("POST", "/api/v1/kbs/" + a.kb + "/indexes/" + idx.id + "/approve",
        {reason: "isolated technical acceptance; not legal approval"}, a.admin);
      const versions = (await a.call("GET", "/api/v1/kbs/" + a.kb + "/versions", undefined, a.token)).data;
      const version = versions.kb_versions.find((v) => v.id === idx.kb_version_id);
      await a.call("POST", "/api/v1/kbs/" + a.kb + "/aliases",
        {alias: "prod", kb_version: version.version, index_version: idx.version}, a.token);
      return {index: idx.id, version: version.version};
    })()`);
    console.log("Real embedding index approved and published:", index);
    await evaluate(`(async () => {
      const a = window.acceptance;
      await a.call("POST", "/v1/retrieve",
        {kb_ids: [a.kb], query: "生态环境法典", top_k: 3}, a.reader, 403);
      await a.call("POST", "/api/v1/kbs/" + a.kb + "/grants",
        {subject_kind: "app", subject_id: ${JSON.stringify(setup.reader)}, level: "read"}, a.token, 201);
    })()`);
    await evaluate("document.querySelector('[data-open-token]').click()");
    await evaluate("document.querySelector('#appTokenInput').value = window.acceptance.reader");
    await evaluate("document.querySelector('#tokenForm').requestSubmit()");
    await waitFor("!document.querySelector('#tokenDialog').open", 90);
    console.log("Reader denied before grant; browser uses the granted reader.");
    await evaluate("document.querySelector('[data-route=\"retrieval\"]').click()");
    await waitFor(`!!document.querySelector('[data-kb-id="${setup.kb}"]')`, 60);
    await evaluate(`document.querySelectorAll('#kbChips .is-selected').forEach((el) => {
      if (el.dataset.kbId !== ${JSON.stringify(setup.kb)}) el.click();
    }); if (!document.querySelector('[data-kb-id="${setup.kb}"]').classList.contains('is-selected'))
      document.querySelector('[data-kb-id="${setup.kb}"]').click()`);
    await evaluate("document.querySelector('#retrievalQuery').value = '生态环境法典施行后的法律事实引起的生态环境案件适用什么规定？'");
    await evaluate("document.querySelector('#retrievalForm').requestSubmit()");
    await waitFor("!document.querySelector('#runRetrieval').disabled", 180);
    assert.equal(await evaluate("document.querySelector('#retrievalResults').hidden"), false,
      await evaluate("document.querySelector('#toast').textContent"));
    const resultText = await evaluate("document.querySelector('#retrievalResults').textContent");
    assert.match(resultText, /生态环境法典/);
    assert.match(resultText, /相关性及法律适用仍需人工核验/);
    assert.match(resultText, /排序分/);
    console.log("Browser retrieval rendered a public-source result.");
    const assessment = await evaluate(`(async () => {
      const a = window.acceptance;
      const query = "生态环境法典施行后的法律事实引起的生态环境案件适用什么规定？";
      const retrieval = await a.call("POST", "/v1/retrieve",
        {kb_ids: [a.kb], query, top_k: 3, with_citation: true}, a.token);
      if (retrieval.citation.status !== "VERIFIED" || !retrieval.results.length)
        throw new Error("citation was not verified");
      const hit = retrieval.results[0];
      a.evidence = hit.text || hit.citation.snippet;
      const source = await a.call("GET", hit.citation.source_url, undefined, a.token);
      if (!source.data.chunks.some((chunk) => chunk.chunk_id === hit.chunk_id))
        throw new Error("citation chunk is absent from source snapshot");
      const feedback = (await a.call("POST", "/api/v1/feedback",
        {request_id: retrieval.request_id, rating: "NEGATIVE",
          reason: "isolated technical regression; not expert legal review"}, a.token, 201)).data;
      const dataset = (await a.call("POST", "/api/v1/datasets", {
        code: "browser-eval-" + Date.now(), name: "公开司法资料技术回归",
        type: "RETRIEVAL_EVAL"
      }, a.token, 201)).data;
      const version = (await a.call("POST", "/api/v1/datasets/" + dataset.id + "/versions", {
        version: "v1", source_snapshot: {source: "retrieval_feedback", feedback_ids: [feedback.id]},
        split: {train: 0, validation: 0, test: 1}
      }, a.token, 201)).data;
      const items = (await a.call("GET", "/api/v1/dataset-versions/" + version.id + "/items",
        undefined, a.token)).data.items;
      if (items.length !== 1 || items[0].review_status !== "NEEDS_REVIEW")
        throw new Error("feedback sample must require review");
      const task = (await a.call("POST", "/api/v1/annotation-tasks", {
        name: "技术回归标注", task_type: "RETRIEVAL",
        dataset_version_id: version.id, required_reviewers: 1
      }, a.token, 201)).data;
      await a.call("POST", "/api/v1/annotation-tasks/" + task.id + "/records", {
        item_id: items[0].id, decision: "APPROVED",
        label: {relevant_chunk_ids: [hit.chunk_id]}
      }, a.token, 201);
      await a.call("POST", "/api/v1/dataset-versions/" + version.id + "/publish", {}, a.token);
      let evaluation = (await a.call("POST", "/api/v1/evals", {
        dataset_version_id: version.id, target_type: "RETRIEVAL", target_version: "browser-real",
        config: {top_k: 3, gates: {recall_at_k: 1.0}}
      }, a.token, [201, 202])).data;
      if (evaluation.job) {
        await a.waitJob(evaluation.job.id, a.token);
        evaluation = (await a.call("GET", "/api/v1/evals/" + evaluation.id, undefined, a.token)).data;
      }
      if (evaluation.status !== "SUCCEEDED" || !evaluation.metrics.gate_passed)
        throw new Error("evaluation did not pass: " + JSON.stringify(evaluation));
      return {request_id: retrieval.request_id, citation: retrieval.citation.status,
        chunk_id: hit.chunk_id, dataset_version: version.id, metrics: evaluation.metrics};
    })()`);
    console.log("Citation snapshot, feedback and technical regression:", assessment);
    let generationError;
    try {
      const generation = await evaluate(`(async () => {
      const a = window.acceptance, code = "browser-chat-" + Date.now();
      await a.call("POST", "/api/v1/admin/capabilities", {
        code, name: "真实模型验收", type: "MODEL_CHAT", owner: "acceptance",
        version: "1.0.0", protocol: "OPENAI", model_name: "MiniMax-M2.7",
        request_schema: {type: "object", required: ["messages"], properties: {messages: {type: "array"}}},
        response_schema: {type: "object"}, tags: ["acceptance"]
      }, a.admin, 201);
      await a.call("POST", "/api/v1/admin/capabilities/" + code + "/versions/1.0.0/publish", {}, a.admin);
      await a.call("POST", "/api/v1/admin/capabilities/" + code + "/grants",
        {app_id: ${JSON.stringify(setup.app)}}, a.admin, 201);
      const answer = await a.call("POST", "/v1/chat/completions", {
        model: code, messages: [{role: "user", content:
          "仅根据以下资料简述其内容，并说明法律效力仍需核验：\\n" + a.evidence}],
        max_tokens: 400
      }, a.token);
      const content = answer.choices?.[0]?.message?.content;
      if (typeof content !== "string" || !content.trim()) throw new Error("empty generation response");
      return {model: answer.model, characters: content.length};
    })()`);
      console.log("Real chat completion received:", generation);
    } catch (error) {
      generationError = error;
      console.error("Real chat completion failed:", error.message);
    }
    const probes = await evaluate(`(async () => {
      const a = window.acceptance;
      const cases = [
        {query: "生态环境法典施行前的法律事实应适用什么规定？", expected: true},
        {query: "火星基地的外星居民如何办理星际护照？", expected: false},
        {query: "夫妻离婚时房屋财产如何分割？", expected: false},
        {query: "刑事案件中犯罪嫌疑人何时可以聘请辩护人？", expected: false}
      ];
      const outcomes = [];
      for (const item of cases) {
        const result = await a.call("POST", "/v1/retrieve", {
          kb_ids: [a.kb], query: item.query, top_k: 3, with_citation: true
        }, a.token);
        outcomes.push({query: item.query, expected: item.expected,
          count: result.results.length, citation: result.citation.status});
      }
      return outcomes;
    })()`);
    console.log("Holdout probes (not an expert-reviewed test):", probes);
    assert.ok(probes.every((item) => (item.count > 0) === item.expected),
      "at least one relevance probe failed");
    await evaluate("document.querySelector('#retrievalQuery').value = '火星基地的外星居民如何办理星际护照？'");
    await evaluate("document.querySelector('#retrievalForm').requestSubmit()");
    await waitFor("!document.querySelector('#runRetrieval').disabled", 180);
    assert.match(await evaluate("document.querySelector('#retrievalResults').textContent"), /没有召回结果/);
    console.log("Browser displayed the no-evidence state.");
    if (generationError) throw generationError;
  } finally {
    if (socket) socket.close();
    browser.kill();
  }
}

main().catch((error) => { console.error(error); process.exitCode = 1; });
