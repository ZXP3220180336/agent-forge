/**
 * ============================================
 * AI 聊天助手 - 前端逻辑
 * 负责：消息发送、流式接收、UI 更新
 * 技术：Fetch API + ReadableStream
 * 依赖：无外部依赖，纯原生 JavaScript
 * ============================================
 */

// ===== DOM 元素引用 =====
const chatMessages = document.getElementById("chatMessages");
const userInput = document.getElementById("userInput");
const sendBtn = document.getElementById("sendBtn");

// ===== 应用状态 =====
let isLoading = false; // 是否正在等待 AI 响应
let abortController = null; // 用于取消请求

// ===== 初始化 =====
document.addEventListener("DOMContentLoaded", () => {
  userInput.focus();
  bindEvents();
});

// ===== 事件绑定 =====
function bindEvents() {
  // 发送按钮点击
  sendBtn.addEventListener("click", sendMessage);

  // 回车键发送
  userInput.addEventListener("keydown", (event) => {
    if (event.key === "Enter" && !event.shiftKey) {
      event.preventDefault(); // 阻止默认换行行为
      sendMessage();
    }
  });

  // 输入框自动调整高度（如果支持多行）
  userInput.addEventListener("input", autoResizeInput);
}

// ===== 发送消息 =====
async function sendMessage() {
  // 获取用户输入并验证
  const message = userInput.value.trim();
  if (!message || isLoading) return;

  // 清空输入框
  userInput.value = "";
  resetInputHeight();

  // 添加用户消息到界面
  addMessage(message, "user");

  // 开始加载状态
  setLoading(true);

  // ★ ===== 创建 AI 响应容器（包含 reasoning panel + 回答气泡） =====
  const assistantContainer = document.createElement("div");
  assistantContainer.className = "assistant-container";

  // 1. 创建 reasoning panel（初始隐藏）
  const reasoningPanel = document.createElement("div");
  reasoningPanel.className = "reasoning-panel";
  reasoningPanel.style.display = "none";
  reasoningPanel.innerHTML = `
        <details>
            <summary>
                <span class="reasoning-icon">🧠</span>
                <span class="reasoning-title">深度思考过程</span>
                <span class="reasoning-toggle-icon">▸</span>
            </summary>
            <div class="reasoning-content"></div>
        </details>
    `;
  assistantContainer.appendChild(reasoningPanel);

  // 2. 创建回答气泡
  const aiMessageDiv = document.createElement("div");
  aiMessageDiv.className = "message assistant";
  aiMessageDiv.textContent = "";
  assistantContainer.appendChild(aiMessageDiv);

  // 添加到聊天区域
  chatMessages.appendChild(assistantContainer);
  scrollToBottom();

  // 发送请求并处理流式响应
  try {
    // ★ 将 reasoningPanel 和 aiMessageDiv 传入流式处理函数
    await fetchStreamResponse(message, reasoningPanel, aiMessageDiv);
  } catch (error) {
    // 处理预期外的错误
    if (error.name !== "AbortError") {
      aiMessageDiv.textContent = `网络错误: ${error.message}`;
    }
  } finally {
    setLoading(false);
  }
}

// ===== 流式请求 =====
// ★ 参数增加 reasoningPanel
async function fetchStreamResponse(message, reasoningPanel, aiMessageDiv) {
  // 创建 AbortController 用于取消请求
  abortController = new AbortController();

  // 调用流式接口
  const response = await fetch("/chat/stream", {
    method: "POST",
    headers: {
      "Content-Type": "application/json",
      Accept: "text/event-stream",
    },
    body: JSON.stringify({ message }),
    signal: abortController.signal,
  });

  // 检查响应状态
  if (!response.ok) {
    throw new Error(`HTTP ${response.status}: ${response.statusText}`);
  }

  // 获取响应体的读取器
  const reader = response.body.getReader();
  const decoder = new TextDecoder();
  let buffer = "";

  // ★ 每个对话实例独立的 reasoning 状态
  let hasReasoningContent = false;
  // ★ 从动态创建的 panel 中获取内容容器
  const reasoningContent = reasoningPanel.querySelector(".reasoning-content");

  // 循环读取数据流
  while (true) {
    const { done, value } = await reader.read();
    if (done) break;

    // 解码并追加到缓冲区
    buffer += decoder.decode(value, { stream: true });

    // 按 SSE 格式分割消息（以 \n\n 分隔）
    const lines = buffer.split("\n\n");
    // 最后一个元素可能不完整，保留到下次处理
    buffer = lines.pop() || "";

    // 处理每条完整消息
    for (const line of lines) {
      if (line.startsWith("data: ")) {
        const rawData = line.slice(6);

        // 检查结束标记
        if (rawData === "[DONE]") continue;

        // 尝试解析 JSON
        isJson = false;
        parsedData = null;

        try {
          parsedData = JSON.parse(rawData);
          isJson = true;
        } catch (e) {
          // 解析失败，说明是普通文本
          isJson = false;
        }

        if (isJson) {
          // JSON 处理
          // ===== 新增：处理推理内容（reasoning） =====
          if (parsedData.type === "reasoning" && parsedData.content) {
            // 显示推理面板（如果尚未显示）
            if (!hasReasoningContent) {
              reasoningPanel.style.display = "block";
              // 自动展开面板
              const details = reasoningPanel.querySelector("details");
              if (details) details.open = true;
              hasReasoningContent = true;
            }
            // 追加推理内容
            reasoningContent.textContent += parsedData.content;
            // 自动滚动到底部
            reasoningContent.scrollTop = reasoningContent.scrollHeight;
          }
          // ===== 处理 AI 回答内容（message） =====
          else if (parsedData.type === "message" && parsedData.content) {
            aiMessageDiv.textContent += parsedData.content;
          }
          // 处理迭代结束标记
          else if (parsedData.type === "iteration_end") {
            console.log(`第 ${parsedData.iteration} 轮迭代结束`);
          }
          // ===== 处理错误 =====
          else if (parsedData.type === "error") {
            aiMessageDiv.textContent += `错误: ${parsedData.content}`;
            return;
          }
          // 处理终止信息
          else if (parsedData.type === "info") {
            // 可选：在界面上显示终止原因
            aiMessageDiv.textContent += `终止原因: ${parsedData.content}`;
          }
        } else {
          // ✅ 普通文本直接追加
          aiMessageDiv.textContent += rawData;
        }

        // ✅ 实时滚动
        scrollToBottom();
      }
    }
  }
}

// ===== UI 辅助函数 =====

/**
 * 添加消息到聊天界面
 */
function addMessage(content, role) {
  const messageDiv = createMessageElement(content, role);
  chatMessages.appendChild(messageDiv);
  scrollToBottom();
  return messageDiv;
}

/**
 * 创建消息 DOM 元素
 */
function createMessageElement(content, role) {
  const div = document.createElement("div");
  div.className = `message ${role}`;
  div.textContent = content;
  return div;
}

/**
 * 滚动到聊天区域底部
 */
function scrollToBottom() {
  // 使用 requestAnimationFrame 确保在 DOM 更新后滚动
  requestAnimationFrame(() => {
    chatMessages.scrollTop = chatMessages.scrollHeight;
  });
}

/**
 * 设置加载状态
 */
function setLoading(loading) {
  isLoading = loading;
  sendBtn.disabled = loading;
  sendBtn.innerHTML = loading
    ? '<span class="loading-spinner"></span> 思考中...'
    : "发送";
  userInput.disabled = loading;

  if (!loading) {
    userInput.focus();
  }
}

/**
 * 输入框自动调整高度（支持多行时使用）
 */
function autoResizeInput() {
  userInput.style.height = "auto";
  userInput.style.height = Math.min(userInput.scrollHeight, 120) + "px";
}

/**
 * 重置输入框高度
 */
function resetInputHeight() {
  userInput.style.height = "auto";
}

// ===== 键盘快捷键（可选增强） =====
document.addEventListener("keydown", (event) => {
  // Ctrl+Enter 或 Cmd+Enter 发送（多行输入时）
  if ((event.ctrlKey || event.metaKey) && event.key === "Enter") {
    if (document.activeElement === userInput) {
      event.preventDefault();
      sendMessage();
    }
  }
});

// ===== 导出（如果需要模块化） =====
// export { sendMessage, addMessage, setLoading };
