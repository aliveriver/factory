const bridge = window.AstrBotPluginPage;
const sshForm = document.getElementById("ssh-form");
const harnessForm = document.getElementById("harness-form");
const sshResult = document.getElementById("ssh-result");
const harnessResult = document.getElementById("harness-result");
const modelList = document.getElementById("model-list");
const saveHarness = document.getElementById("save-harness");
const sshProfile = document.getElementById("ssh-profile");
let discoveredModels = [];
let discoveryId = "";

function formObject(form) {
  return Object.fromEntries(new FormData(form).entries());
}

function showToast(message) {
  document.getElementById("toast").textContent = message;
}

sshProfile.addEventListener("change", () => {
  const selected = sshProfile.selectedOptions[0];
  if (!selected?.dataset.profile) return;
  const profile = JSON.parse(selected.dataset.profile);
  for (const field of ["host", "port", "user", "identity_file"]) {
    if (profile[field] !== undefined) sshForm.elements[field].value = profile[field];
  }
});

function renderModels(models) {
  discoveredModels = models;
  modelList.replaceChildren();
  if (!models.length) {
    modelList.innerHTML = '<span class="muted">Harness 没有返回可选模型</span>';
    saveHarness.disabled = true;
    return;
  }
  models.forEach((model, index) => {
    const value = model.value || model.model || "";
    const label = model.name || model.displayName || value;
    const item = document.createElement("label");
    item.className = "model-option";
    const input = document.createElement("input");
    input.type = "radio";
    input.name = "selected-model";
    input.value = index;
    input.checked = index === 0;
    const text = document.createElement("span");
    const title = document.createElement("strong");
    title.textContent = label;
    const detail = document.createElement("small");
    detail.textContent = value;
    text.append(title, detail);
    item.append(input, text);
    modelList.append(item);
  });
  saveHarness.disabled = false;
}

sshForm.addEventListener("submit", async (event) => {
  event.preventDefault();
  sshResult.textContent = "正在读取...";
  try {
    const result = await bridge.apiPost("discover/ssh", {
      ...formObject(sshForm),
      port: Number(sshForm.elements.port.value),
    });
    sshResult.textContent = JSON.stringify(result.environment, null, 2);
    if (result.capabilities) {
      sshForm.dataset.capabilities = result.capabilities;
    }
    showToast("SSH 环境读取完成");
  } catch (error) {
    sshResult.textContent = error.message;
    showToast("SSH 环境读取失败");
  }
});

document.getElementById("save-ssh").addEventListener("click", async () => {
  try {
    const values = {
      ...formObject(sshForm),
      port: Number(sshForm.elements.port.value),
    };
    const discovered = await bridge.apiPost("discover/ssh", values);
    sshForm.dataset.capabilities = discovered.capabilities || "";
    sshResult.textContent = JSON.stringify(discovered.environment, null, 2);
    await bridge.apiPost("configure/ssh", {
      ...values,
      capabilities: discovered.capabilities || "",
    });
    showToast("SSH 设置已写入，请重新加载插件");
  } catch (error) {
    showToast(error.message);
  }
});

harnessForm.addEventListener("submit", async (event) => {
  event.preventDefault();
  harnessResult.textContent = "正在启动本机 Harness 并读取模型...";
  try {
    const values = formObject(harnessForm);
    let argumentsValue = [];
    if (values.arguments.trim()) {
      argumentsValue = JSON.parse(values.arguments);
      if (!Array.isArray(argumentsValue)) throw new Error("启动参数必须是 JSON 数组");
    }
    const result = await bridge.apiPost("discover/harness", {
      ...values,
      arguments: argumentsValue,
    });
    discoveryId = result.discovery_id || "";
    renderModels(result.models || []);
    harnessResult.textContent = JSON.stringify(result.models || [], null, 2);
    showToast("模型读取完成");
  } catch (error) {
    harnessResult.textContent = error.message;
    showToast("Harness 模型读取失败");
  }
});

saveHarness.addEventListener("click", async () => {
  const selected = document.querySelector('input[name="selected-model"]:checked');
  if (!selected) return;
  const values = formObject(harnessForm);
  const model = discoveredModels[Number(selected.value)]?.value;
  try {
    await bridge.apiPost("configure/harness", {
      ...values,
      model,
      discovery_id: discoveryId,
      arguments: values.arguments.trim() ? JSON.parse(values.arguments) : [],
    });
    showToast("Harness 成员已写入，请重新加载插件");
  } catch (error) {
    showToast(error.message);
  }
});

await bridge.ready();
try {
  const profiles = await bridge.apiGet("discover/ssh-config");
  for (const profile of profiles.hosts || []) {
    const option = document.createElement("option");
    option.value = profile.alias;
    option.textContent = `${profile.alias} -> ${profile.host}`;
    option.dataset.profile = JSON.stringify(profile);
    sshProfile.append(option);
  }
  const current = await bridge.apiGet("config");
  const ssh = current.ssh || {};
  for (const field of ["host", "port", "user", "identity_file", "root", "python"]) {
    if (ssh[field] !== undefined && sshForm.elements[field]) {
      sshForm.elements[field].value = ssh[field];
    }
  }
} catch (error) {
  showToast(`读取当前配置失败：${error.message}`);
}
