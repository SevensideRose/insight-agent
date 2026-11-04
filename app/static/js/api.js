const JSON_HEADERS = { "Content-Type": "application/json" };

export class ApiError extends Error {
  constructor(message, status) {
    super(message);
    this.name = "ApiError";
    this.status = status;
  }
}

async function parseError(response) {
  let message = `${response.status} ${response.statusText}`;
  try {
    const data = await response.json();
    if (typeof data.detail === "string") message = data.detail;
    else if (Array.isArray(data.detail)) message = data.detail.map((d) => d.msg || JSON.stringify(d)).join("；");
    else if (data.message) message = data.message;
  } catch (_) {
    /* ignore */
  }
  return new ApiError(message, response.status);
}

async function request(path, { method = "GET", body, headers = {}, signal } = {}) {
  const options = { method, headers: { ...headers }, signal };
  if (body !== undefined) {
    options.headers = { ...JSON_HEADERS, ...headers };
    options.body = JSON.stringify(body);
  }
  const response = await fetch(path, options);
  if (!response.ok) throw await parseError(response);
  if (response.status === 204) return null;
  const contentType = response.headers.get("content-type") || "";
  if (contentType.includes("application/json")) return response.json();
  return response.text();
}

export const api = {
  get: (path, options) => request(path, options),
  post: (path, body, options) => request(path, { ...options, method: "POST", body }),
  patch: (path, body, options) => request(path, { ...options, method: "PATCH", body }),
  del: (path, options) => request(path, { ...options, method: "DELETE" }),

  async uploadFiles(kbId, files, config) {
    const form = new FormData();
    for (const file of files) form.append("files", file, file.name);
    form.append("config", JSON.stringify(config));
    const response = await fetch(`/api/kbs/${kbId}/documents`, { method: "POST", body: form });
    if (!response.ok) throw await parseError(response);
    return response.json();
  },

  async streamChat(payload, onEvent, signal) {
    const response = await fetch("/api/chat/stream", {
      method: "POST",
      headers: JSON_HEADERS,
      body: JSON.stringify(payload),
      signal,
    });
    if (!response.ok) throw await parseError(response);
    if (!response.body) throw new ApiError("浏览器不支持流式响应", 0);

    const reader = response.body.getReader();
    const decoder = new TextDecoder("utf-8");
    let buffer = "";

    const dispatch = (block) => {
      const lines = block.split("\n");
      let event = "message";
      const dataLines = [];
      for (const rawLine of lines) {
        const line = rawLine.replace(/\r$/, "");
        if (line.startsWith("event:")) event = line.slice(6).trim();
        else if (line.startsWith("data:")) dataLines.push(line.slice(5).trimStart());
      }
      if (!dataLines.length) return;
      const text = dataLines.join("\n");
      let data = text;
      try {
        data = JSON.parse(text);
      } catch (_) {
        /* keep string */
      }
      onEvent(event, data);
    };

    while (true) {
      const { value, done } = await reader.read();
      if (done) break;
      buffer += decoder.decode(value, { stream: true });
      buffer = buffer.replace(/\r\n/g, "\n");
      let index;
      while ((index = buffer.indexOf("\n\n")) >= 0) {
        const block = buffer.slice(0, index);
        buffer = buffer.slice(index + 2);
        if (block.trim()) dispatch(block);
      }
    }
    if (buffer.trim()) dispatch(buffer);
  },
};
