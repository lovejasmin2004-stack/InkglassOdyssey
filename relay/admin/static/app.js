/* Inkglass Admin — Library Workshop + RP Tester
 *
 * Schema-driven form generation (Invariant #24: no hardcoded Workshop forms).
 * Forms are built at runtime from the JSON Schema files served by the admin API.
 */

const ADMIN_BASE = "";                     // same origin (port 8081)
const RELAY_WS   = "ws://127.0.0.1:8000"; // main relay WebSocket

// ---------------------------------------------------------------------------
// Tab switching
// ---------------------------------------------------------------------------

document.querySelectorAll(".tab-btn").forEach(btn => {
  btn.addEventListener("click", () => {
    document.querySelectorAll(".tab-btn").forEach(b => b.classList.remove("active"));
    document.querySelectorAll(".tab-panel").forEach(p => p.classList.remove("active"));
    btn.classList.add("active");
    document.getElementById("tab-" + btn.dataset.tab).classList.add("active");
  });
});

// ---------------------------------------------------------------------------
// Toast notifications
// ---------------------------------------------------------------------------

function toast(msg, type = "success") {
  const el = document.createElement("div");
  el.className = "toast " + type;
  el.textContent = msg;
  document.body.appendChild(el);
  setTimeout(() => el.remove(), 3000);
}

// ---------------------------------------------------------------------------
// API helpers
// ---------------------------------------------------------------------------

async function api(path, opts = {}) {
  const res = await fetch(ADMIN_BASE + path, {
    headers: { "Content-Type": "application/json", ...opts.headers },
    ...opts,
  });
  if (!res.ok) {
    const body = await res.json().catch(() => ({ detail: res.statusText }));
    throw new Error(Array.isArray(body.detail) ? body.detail.join("\n") : body.detail || res.statusText);
  }
  return res.json();
}

// ═══════════════════════════════════════════════════════════════════════════
// LIBRARY WORKSHOP
// ═══════════════════════════════════════════════════════════════════════════

const wsContentType = document.getElementById("ws-content-type");
const wsWorld       = document.getElementById("ws-world");
const wsFileList    = document.getElementById("ws-file-list");
const wsEditorTitle = document.getElementById("ws-editor-title");
const wsEditorArea  = document.getElementById("ws-editor-content");
const wsSaveBtn     = document.getElementById("ws-save-btn");
const wsValidateBtn = document.getElementById("ws-validate-btn");
const wsDeleteBtn   = document.getElementById("ws-delete-btn");
const wsNewBtn      = document.getElementById("ws-new-btn");

let wsCurrentSchema = null;   // loaded JSON Schema object
let wsCurrentFileId = null;
let wsEditorMode    = "form"; // "form" | "json"
let wsJsonEditor    = null;   // textarea ref in json mode

// Load content types and worlds on init
(async function initWorkshop() {
  const [types, worlds] = await Promise.all([
    api("/api/content-types"),
    api("/api/worlds"),
  ]);
  Object.keys(types).sort().forEach(t => {
    const opt = document.createElement("option");
    opt.value = t;
    opt.textContent = t;
    wsContentType.appendChild(opt);
  });
  worlds.forEach(w => {
    const opt = document.createElement("option");
    opt.value = w;
    opt.textContent = w;
    wsWorld.appendChild(opt);
    // also populate RP tester world dropdown
    const opt2 = opt.cloneNode(true);
    document.getElementById("rp-world").appendChild(opt2);
  });
})();

// Reload file list when content type or world changes
let _reloadSeq = 0;
wsContentType.addEventListener("change", reloadFileList);
wsWorld.addEventListener("change", reloadFileList);

async function reloadFileList() {
  const seq = ++_reloadSeq;
  wsFileList.innerHTML = "";
  wsCurrentFileId = null;
  const ct = wsContentType.value;
  const w  = wsWorld.value;
  if (!ct || !w) return;

  // Load schema for this content type
  const types = await api("/api/content-types");
  if (seq !== _reloadSeq) return;
  const schemaFile = types[ct]?.schema;
  if (schemaFile) {
    const schemaName = schemaFile.replace(".json", "");
    wsCurrentSchema = await api("/api/schemas/" + schemaName);
  } else {
    wsCurrentSchema = null;
  }
  if (seq !== _reloadSeq) return;

  const files = await api(`/api/content/${ct}/${w}`);
  if (seq !== _reloadSeq) return;
  files.forEach(f => {
    const div = document.createElement("div");
    div.className = "sidebar-item";
    div.dataset.id = f.id;
    div.innerHTML = `<span>${f.name || f.id}</span><span style="font-size:0.75rem;color:var(--text2)">${f.id}</span>`;
    div.addEventListener("click", () => loadFile(f.id));
    wsFileList.appendChild(div);
  });
}

async function loadFile(fileId) {
  const ct = wsContentType.value;
  const w  = wsWorld.value;
  const data = await api(`/api/content/${ct}/${w}/${fileId}`);
  wsCurrentFileId = fileId;
  wsEditorTitle.textContent = `${ct} / ${w} / ${fileId}`;
  wsSaveBtn.disabled = false;
  wsValidateBtn.disabled = false;
  wsDeleteBtn.disabled = false;

  // Highlight active item
  wsFileList.querySelectorAll(".sidebar-item").forEach(el => {
    el.classList.toggle("active", el.dataset.id === fileId);
  });

  renderEditor(data);
}

// New file
wsNewBtn.addEventListener("click", () => {
  const ct = wsContentType.value;
  const w  = wsWorld.value;
  if (!ct || !w) { toast("Select content type and world first", "error"); return; }

  const fileId = prompt("Enter file ID (snake_case):");
  if (!fileId || !/^[a-z][a-z0-9_]*$/.test(fileId)) {
    if (fileId) toast("ID must be lowercase snake_case", "error");
    return;
  }

  wsCurrentFileId = fileId;
  wsEditorTitle.textContent = `${ct} / ${w} / ${fileId} (new)`;
  wsSaveBtn.disabled = false;
  wsValidateBtn.disabled = false;
  wsDeleteBtn.disabled = true;

  // Scaffold from schema defaults
  const scaffold = wsCurrentSchema ? scaffoldFromSchema(wsCurrentSchema, { id: fileId, world_id: w }) : { id: fileId };
  renderEditor(scaffold);
});

// Editor mode toggle (form / json)
document.querySelectorAll(".editor-mode-toggle .mode-btn").forEach(btn => {
  btn.addEventListener("click", () => {
    document.querySelectorAll(".editor-mode-toggle .mode-btn").forEach(b => b.classList.remove("active"));
    btn.classList.add("active");
    const newMode = btn.dataset.mode;
    if (newMode === wsEditorMode) return;

    const currentData = collectFormData();
    wsEditorMode = newMode;
    renderEditor(currentData);
  });
});

// Save
wsSaveBtn.addEventListener("click", async () => {
  try {
    const data = collectFormData();
    const ct = wsContentType.value;
    const w  = wsWorld.value;
    await api(`/api/content/${ct}/${w}/${wsCurrentFileId}`, {
      method: "PUT",
      body: JSON.stringify(data),
    });
    toast("Saved " + wsCurrentFileId);
    reloadFileList();
  } catch (e) {
    toast(e.message, "error");
  }
});

// Validate
wsValidateBtn.addEventListener("click", async () => {
  try {
    const data = collectFormData();
    const ct = wsContentType.value;
    const w  = wsWorld.value;
    const result = await api(`/api/content/${ct}/${w}/validate`, {
      method: "POST",
      body: JSON.stringify(data),
    });
    if (result.valid) {
      toast("Valid!");
    } else {
      showValidationErrors(result.errors);
    }
  } catch (e) {
    toast(e.message, "error");
  }
});

// Delete
wsDeleteBtn.addEventListener("click", async () => {
  if (!confirm(`Delete ${wsCurrentFileId}?`)) return;
  try {
    const ct = wsContentType.value;
    const w  = wsWorld.value;
    await api(`/api/content/${ct}/${w}/${wsCurrentFileId}`, { method: "DELETE" });
    toast("Deleted " + wsCurrentFileId);
    wsCurrentFileId = null;
    wsEditorArea.innerHTML = '<div class="empty-state">File deleted.</div>';
    wsSaveBtn.disabled = true;
    wsValidateBtn.disabled = true;
    wsDeleteBtn.disabled = true;
    reloadFileList();
  } catch (e) {
    toast(e.message, "error");
  }
});

function showValidationErrors(errors) {
  let container = wsEditorArea.querySelector(".validation-errors");
  if (!container) {
    container = document.createElement("div");
    container.className = "validation-errors";
    wsEditorArea.insertBefore(container, wsEditorArea.firstChild);
  }
  container.innerHTML = `<h4>Validation Errors</h4><ul>${errors.map(e => `<li>${esc(e)}</li>`).join("")}</ul>`;
}

// ---------------------------------------------------------------------------
// Schema-driven form rendering
// ---------------------------------------------------------------------------

function renderEditor(data) {
  wsEditorArea.innerHTML = "";
  if (wsEditorMode === "json") {
    renderJsonEditor(data);
  } else {
    renderFormEditor(data);
  }
}

function renderJsonEditor(data) {
  const ta = document.createElement("textarea");
  ta.className = "json-editor";
  ta.value = JSON.stringify(data, null, 2);
  wsEditorArea.appendChild(ta);
  wsJsonEditor = ta;
}

function renderFormEditor(data) {
  if (!wsCurrentSchema) {
    renderJsonEditor(data);
    return;
  }
  const form = document.createElement("div");
  form.className = "schema-form";
  form.id = "schema-form-root";
  buildFormFields(form, wsCurrentSchema, data, "");
  wsEditorArea.appendChild(form);
}

function collectFormData() {
  if (wsEditorMode === "json" || !wsCurrentSchema) {
    const ta = wsEditorArea.querySelector(".json-editor");
    if (ta) return JSON.parse(ta.value);
    return {};
  }
  return collectObjectFromForm(document.getElementById("schema-form-root"), wsCurrentSchema);
}

function scaffoldFromSchema(schema, overrides = {}) {
  const obj = {};
  const props = schema.properties || {};
  for (const [key, prop] of Object.entries(props)) {
    if (key in overrides) { obj[key] = overrides[key]; continue; }
    if (prop.default !== undefined) { obj[key] = prop.default; continue; }
    switch (prop.type) {
      case "string":  obj[key] = prop.enum ? prop.enum[0] : ""; break;
      case "integer": case "number": obj[key] = prop.minimum || 0; break;
      case "boolean": obj[key] = false; break;
      case "array":   obj[key] = []; break;
      case "object":  obj[key] = {}; break;
    }
  }
  return obj;
}

// Build form fields recursively from a JSON Schema
function buildFormFields(container, schema, data, pathPrefix) {
  const props = schema.properties || {};
  const required = new Set(schema.required || []);

  for (const [key, prop] of Object.entries(props)) {
    const fullPath = pathPrefix ? pathPrefix + "." + key : key;
    const value = data?.[key];
    const isReq = required.has(key);

    if (prop.type === "object" && prop.properties) {
      // Nested object → section
      const section = document.createElement("div");
      section.className = "form-section";
      section.innerHTML = `<div class="section-title">${formatLabel(key)}${isReq ? " *" : ""}</div>`;
      buildFormFields(section, prop, value || {}, fullPath);
      container.appendChild(section);
    } else if (prop.type === "array") {
      buildArrayField(container, key, prop, value || [], fullPath, isReq);
    } else if (prop.type === "object" && !prop.properties) {
      // Freeform object → JSON textarea
      const group = makeGroup(key, isReq, prop.description);
      const ta = document.createElement("textarea");
      ta.dataset.path = fullPath;
      ta.dataset.jsonObj = "true";
      ta.rows = 4;
      ta.value = value ? JSON.stringify(value, null, 2) : "{}";
      group.appendChild(ta);
      container.appendChild(group);
    } else {
      buildScalarField(container, key, prop, value, fullPath, isReq);
    }
  }
}

function buildScalarField(container, key, prop, value, path, isReq) {
  const group = makeGroup(key, isReq, prop.description);

  if (prop.enum) {
    const sel = document.createElement("select");
    sel.dataset.path = path;
    prop.enum.forEach(v => {
      const opt = document.createElement("option");
      opt.value = v;
      opt.textContent = v;
      if (v === value) opt.selected = true;
      sel.appendChild(opt);
    });
    group.appendChild(sel);
  } else if (prop.type === "boolean") {
    const sel = document.createElement("select");
    sel.dataset.path = path;
    sel.dataset.boolField = "true";
    ["true", "false"].forEach(v => {
      const opt = document.createElement("option");
      opt.value = v;
      opt.textContent = v;
      if (String(value) === v) opt.selected = true;
      sel.appendChild(opt);
    });
    group.appendChild(sel);
  } else if (prop.type === "integer" || prop.type === "number") {
    const inp = document.createElement("input");
    inp.type = "number";
    inp.dataset.path = path;
    inp.dataset.numField = "true";
    if (prop.minimum !== undefined) inp.min = prop.minimum;
    if (prop.maximum !== undefined) inp.max = prop.maximum;
    inp.value = value ?? "";
    group.appendChild(inp);
  } else {
    // string
    if (prop.maxLength && prop.maxLength > 200) {
      const ta = document.createElement("textarea");
      ta.dataset.path = path;
      ta.rows = 3;
      ta.value = value ?? "";
      group.appendChild(ta);
    } else {
      const inp = document.createElement("input");
      inp.type = "text";
      inp.dataset.path = path;
      inp.value = value ?? "";
      if (prop.pattern) inp.pattern = prop.pattern;
      group.appendChild(inp);
    }
  }
  container.appendChild(group);
}

function buildArrayField(container, key, prop, items, path, isReq) {
  const group = makeGroup(key, isReq, prop.description);
  const itemSchema = prop.items;
  const listEl = document.createElement("div");
  listEl.dataset.arrayPath = path;
  listEl.dataset.itemSchema = JSON.stringify(itemSchema || {});

  (items || []).forEach((item, i) => {
    addArrayItem(listEl, itemSchema, item, `${path}[${i}]`, i);
  });

  const addBtn = document.createElement("button");
  addBtn.className = "btn btn-outline btn-sm";
  addBtn.textContent = "+ Add";
  addBtn.type = "button";
  addBtn.addEventListener("click", () => {
    const idx = listEl.querySelectorAll(".array-item").length;
    const blank = itemSchema?.type === "object" ? scaffoldFromSchema(itemSchema) :
                  itemSchema?.type === "string" ? "" : null;
    addArrayItem(listEl, itemSchema, blank, `${path}[${idx}]`, idx);
  });

  group.appendChild(listEl);
  const controls = document.createElement("div");
  controls.className = "array-controls";
  controls.appendChild(addBtn);
  group.appendChild(controls);
  container.appendChild(group);
}

function addArrayItem(listEl, itemSchema, value, path, index) {
  const wrapper = document.createElement("div");
  wrapper.className = "array-item";

  const removeBtn = document.createElement("button");
  removeBtn.className = "remove-item";
  removeBtn.textContent = "×";
  removeBtn.type = "button";
  removeBtn.addEventListener("click", () => wrapper.remove());
  wrapper.appendChild(removeBtn);

  if (!itemSchema || itemSchema.type === "string") {
    const inp = document.createElement("input");
    inp.type = "text";
    inp.dataset.path = path;
    inp.value = value ?? "";
    wrapper.appendChild(inp);
  } else if (itemSchema.type === "object" && itemSchema.properties) {
    buildFormFields(wrapper, itemSchema, value || {}, path);
  } else {
    const ta = document.createElement("textarea");
    ta.dataset.path = path;
    ta.dataset.jsonObj = "true";
    ta.rows = 2;
    ta.value = typeof value === "object" ? JSON.stringify(value, null, 2) : String(value ?? "");
    wrapper.appendChild(ta);
  }

  listEl.appendChild(wrapper);
}

// Collect data back from form
function collectObjectFromForm(container, schema) {
  const obj = {};
  const props = schema.properties || {};

  for (const [key, prop] of Object.entries(props)) {
    if (prop.type === "object" && prop.properties) {
      const section = findSectionForKey(container, key);
      if (section) obj[key] = collectObjectFromForm(section, prop);
    } else if (prop.type === "array") {
      obj[key] = collectArrayFromForm(container, key, prop);
    } else {
      const el = findFieldByKey(container, key);
      if (!el) continue;
      obj[key] = readFieldValue(el, prop);
    }
  }
  return obj;
}

function collectArrayFromForm(container, key, prop) {
  // Find the array container
  const arrayEls = container.querySelectorAll(`[data-array-path]`);
  let arrayContainer = null;
  for (const el of arrayEls) {
    const p = el.dataset.arrayPath;
    if (p.endsWith(key) || p.split(".").pop() === key) {
      arrayContainer = el;
      break;
    }
  }
  if (!arrayContainer) return [];

  const items = arrayContainer.querySelectorAll(":scope > .array-item");
  const itemSchema = prop.items;
  const result = [];

  items.forEach(itemEl => {
    if (!itemSchema || itemSchema.type === "string") {
      const inp = itemEl.querySelector("input, textarea");
      result.push(inp ? inp.value : "");
    } else if (itemSchema.type === "object" && itemSchema.properties) {
      result.push(collectObjectFromForm(itemEl, itemSchema));
    } else {
      const ta = itemEl.querySelector("textarea");
      if (ta) {
        try { result.push(JSON.parse(ta.value)); } catch { result.push(ta.value); }
      }
    }
  });
  return result;
}

function findSectionForKey(container, key) {
  const sections = container.querySelectorAll(":scope > .form-section");
  for (const s of sections) {
    const title = s.querySelector(".section-title");
    if (title && title.textContent.replace(" *", "").trim() === formatLabel(key)) return s;
  }
  return null;
}

function findFieldByKey(container, key) {
  const allFields = container.querySelectorAll("input, select, textarea");
  for (const el of allFields) {
    const p = el.dataset.path || "";
    const lastKey = p.includes(".") ? p.split(".").pop() : p;
    if (lastKey === key && el.closest(".form-section, .schema-form, .array-item") === container.closest(".form-section, .schema-form, .array-item")) {
      return el;
    }
  }
  // Broader search: just find in direct form-group children
  const groups = container.querySelectorAll(":scope > .form-group");
  for (const g of groups) {
    const label = g.querySelector("label");
    if (label && label.textContent.replace(" *", "").trim() === formatLabel(key)) {
      return g.querySelector("input, select, textarea");
    }
  }
  return null;
}

function readFieldValue(el, prop) {
  if (el.dataset.boolField) return el.value === "true";
  if (el.dataset.numField) {
    const v = el.value;
    return v === "" ? 0 : (prop.type === "integer" ? parseInt(v, 10) : parseFloat(v));
  }
  if (el.dataset.jsonObj) {
    try { return JSON.parse(el.value); } catch { return {}; }
  }
  return el.value;
}

// Helpers
function makeGroup(key, isReq, desc) {
  const group = document.createElement("div");
  group.className = "form-group";
  const label = document.createElement("label");
  label.textContent = formatLabel(key) + (isReq ? " *" : "");
  group.appendChild(label);
  if (desc) {
    const hint = document.createElement("div");
    hint.className = "hint";
    hint.textContent = desc;
    group.appendChild(hint);
  }
  return group;
}

function formatLabel(key) {
  return key.replace(/_/g, " ").replace(/\b\w/g, c => c.toUpperCase());
}

function esc(s) {
  const d = document.createElement("div");
  d.textContent = s;
  return d.innerHTML;
}


// ═══════════════════════════════════════════════════════════════════════════
// RP TESTER
// ═══════════════════════════════════════════════════════════════════════════

const rpWorld      = document.getElementById("rp-world");
const rpNpc        = document.getElementById("rp-npc");
const rpMode       = document.getElementById("rp-mode");
const rpConnectBtn = document.getElementById("rp-connect-btn");
const rpDisconnBtn = document.getElementById("rp-disconnect-btn");
const rpMessages   = document.getElementById("rp-messages");
const rpInput      = document.getElementById("rp-input");
const rpSendBtn    = document.getElementById("rp-send-btn");
const rpStatusDot  = document.getElementById("rp-status-dot");
const rpStatusText = document.getElementById("rp-status-text");

let rpSocket       = null;
let rpSessionData  = null;
let rpCurrentText  = "";

// Load NPCs when world changes
rpWorld.addEventListener("change", async () => {
  rpNpc.innerHTML = '<option value="">Loading...</option>';
  const w = rpWorld.value;
  if (!w) { rpNpc.innerHTML = '<option value="">Select world first</option>'; return; }
  try {
    const npcs = await api(`/api/content/npcs/${w}`);
    rpNpc.innerHTML = "";
    if (npcs.length === 0) {
      rpNpc.innerHTML = '<option value="">No NPCs found</option>';
      return;
    }
    npcs.forEach(n => {
      const opt = document.createElement("option");
      opt.value = n.id;
      opt.textContent = n.name || n.id;
      rpNpc.appendChild(opt);
    });
  } catch (e) {
    rpNpc.innerHTML = '<option value="">Error loading NPCs</option>';
  }
});

// Connect
rpConnectBtn.addEventListener("click", async () => {
  const world = rpWorld.value;
  const npc = rpNpc.value;
  const mode = rpMode.value;
  if (!world || !npc) { toast("Select a world and NPC", "error"); return; }

  rpConnectBtn.disabled = true;
  rpMessages.innerHTML = "";
  addChatMsg("system", "Creating test session...");

  try {
    rpSessionData = await api("/api/test-session", {
      method: "POST",
      body: JSON.stringify({ world_id: world, npc_id: npc, mode }),
    });
    addChatMsg("system", `Session created. Connecting to relay WebSocket...`);
    connectWebSocket();
  } catch (e) {
    addChatMsg("system", "Error: " + e.message);
    rpConnectBtn.disabled = false;
  }
});

function connectWebSocket() {
  rpSocket = new WebSocket(RELAY_WS + "/dialogue");

  rpSocket.addEventListener("open", () => {
    setConnected(true);
    addChatMsg("system", "WebSocket open. Authenticating...");
    rpSocket.send(JSON.stringify({
      type: "auth",
      token: rpSessionData.session_token,
    }));
    addChatMsg("system", `Authenticated. Chatting with NPC: ${rpSessionData.npc_id}`);
  });

  rpSocket.addEventListener("message", (event) => {
    const msg = JSON.parse(event.data);
    handleRelayMessage(msg);
  });

  rpSocket.addEventListener("close", (event) => {
    setConnected(false);
    addChatMsg("system", `Disconnected (code ${event.code})`);
  });

  rpSocket.addEventListener("error", () => {
    setConnected(false);
    addChatMsg("system", "WebSocket error — is the relay running on port 8000?");
  });
}

function handleRelayMessage(msg) {
  switch (msg.type) {
    case "error":
      addChatMsg("system", `Error [${msg.code}]: ${msg.message}`);
      break;
    case "heartbeat_ack":
      break;
    case "stream_start":
      rpCurrentText = "";
      break;
    case "stream_chunk":
      rpCurrentText += msg.text;
      updateStreamingMsg(rpCurrentText);
      break;
    case "stream_end":
      finalizeStreamingMsg(msg.text || rpCurrentText);
      rpCurrentText = "";
      break;
    case "check_proposal":
      addChatMsg("check", `Check proposed: ${msg.skill} (DC ${msg.dc}) — ${msg.reason}`);
      // Auto-confirm in tester
      if (msg.turn_id) {
        rpSocket.send(JSON.stringify({ type: "check_confirm", turn_id: msg.turn_id }));
        addChatMsg("system", "Auto-confirmed check.");
      }
      break;
    case "check_result": {
      const passStr = msg.passed ? "PASSED" : "FAILED";
      const cls = msg.passed ? "passed" : "failed";
      addChatMsg("check " + cls,
        `${msg.skill} check: d20(${msg.dice?.join(",")}) ${msg.roll_mode !== "straight" ? `[${msg.roll_mode}]` : ""} → ${msg.roll} + ${msg.modifier} = ${msg.total} vs DC ${msg.dc} → ${passStr}` +
        (msg.natural_20 ? " (NAT 20!)" : "") + (msg.natural_1 ? " (NAT 1!)" : "")
      );
      break;
    }
    case "passive_check":
      addChatMsg("check", `Passive ${msg.skill}: ${msg.passive_value} vs DC ${msg.dc} — detected!`);
      break;
    case "animation_directive":
      addChatMsg("system", `Animation: ${JSON.stringify(msg.directive)}`);
      break;
    case "scene_update":
      addChatMsg("system", `Scene update: ${JSON.stringify(msg.changes)}`);
      break;
    case "turn_recovery":
      addChatMsg("system", `Recovery data: ${msg.turns?.length || 0} pending turns`);
      break;
    default:
      addChatMsg("system", `Unknown message type: ${msg.type}`);
  }
}

// Disconnect
rpDisconnBtn.addEventListener("click", () => {
  if (rpSocket) rpSocket.close();
  rpSocket = null;
  rpSessionData = null;
  setConnected(false);
});

// Send
rpSendBtn.addEventListener("click", sendMessage);
rpInput.addEventListener("keydown", (e) => {
  if (e.key === "Enter" && !e.shiftKey) { e.preventDefault(); sendMessage(); }
});

function sendMessage() {
  const text = rpInput.value.trim();
  if (!text || !rpSocket || rpSocket.readyState !== WebSocket.OPEN) return;

  addChatMsg("player", text);
  rpInput.value = "";

  const mode = rpMode.value;
  const payload = mode === "rp"
    ? {
        type: "rp_turn",
        scene_id: rpSessionData.scene_id,
        npc_id: rpSessionData.npc_id,
        text,
        character: {
          id: rpSessionData.character_id,
          name: "Admin Test Character",
          level: 5,
          ability_scores: { strength: 14, dexterity: 12, constitution: 13, intelligence: 10, wisdom: 15, charisma: 8 },
          skill_proficiencies: ["perception", "insight", "athletics"],
          conditions: [],
          exhaustion_level: 0,
        },
      }
    : {
        type: "quickchat_turn",
        scene_id: rpSessionData.scene_id,
        npc_id: rpSessionData.npc_id,
        text,
      };

  rpSocket.send(JSON.stringify(payload));
}

// Chat UI helpers
let streamingEl = null;

function addChatMsg(cls, text) {
  const div = document.createElement("div");
  div.className = "msg " + cls;
  div.textContent = text;
  rpMessages.appendChild(div);
  rpMessages.scrollTop = rpMessages.scrollHeight;
  // Clear empty state
  const empty = rpMessages.querySelector(".empty-state");
  if (empty) empty.remove();
  return div;
}

function updateStreamingMsg(text) {
  if (!streamingEl) {
    streamingEl = addChatMsg("npc", text);
  } else {
    streamingEl.textContent = text;
    rpMessages.scrollTop = rpMessages.scrollHeight;
  }
}

function finalizeStreamingMsg(text) {
  if (streamingEl) {
    streamingEl.textContent = text;
    streamingEl = null;
  } else {
    addChatMsg("npc", text);
  }
  rpMessages.scrollTop = rpMessages.scrollHeight;
}

function setConnected(connected) {
  rpStatusDot.className = "status-dot " + (connected ? "connected" : "disconnected");
  rpStatusText.textContent = connected ? "Connected" : "Disconnected";
  rpInput.disabled = !connected;
  rpSendBtn.disabled = !connected;
  rpConnectBtn.disabled = connected;
  rpDisconnBtn.disabled = !connected;
}
