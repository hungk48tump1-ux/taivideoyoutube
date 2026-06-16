// content.js - Cầu nối siêu tốc Z115 DHVIPPRO và Gemini
(function() {
  console.log("🚀 Z115 Bridge Extension is active on Gemini!");

  // Tạo tab_id duy nhất cho tab này (tự xóa khi đóng tab)
  let tabId = sessionStorage.getItem("z115_tab_id");
  if (!tabId) {
    tabId = (typeof crypto !== "undefined" && crypto.randomUUID)
      ? crypto.randomUUID()
      : Math.random().toString(36).slice(2) + Date.now().toString(36);
    sessionStorage.setItem("z115_tab_id", tabId);
  }
  const tabShort = tabId.slice(0, 4) + "..." + tabId.slice(-4);

  // Tự động khôi phục luồng đã gán từ sessionStorage (nếu có) để duy trì khi F5
  let activeJob = sessionStorage.getItem("z115_active_job") || null;
  let serverPort = 8779;
  let statusMessage = "Đang kết nối tới Python...";
  let isConnected = false;
  let currentActionText = "";

  // Tự động quét tham số URL ?z115_job=file1 hoặc ?z115_job=file2 khi tải trang
  const urlParams = new URLSearchParams(window.location.search);
  const urlJob = urlParams.get("z115_job");
  if (urlJob === "file1" || urlJob === "file2") {
    activeJob = "auto";
    sessionStorage.setItem("z115_active_job", "auto");
    // Tự động kết nối và chiếm toàn quyền (Lock tab) để tab này trở thành Worker duy nhất!
    fetchBridge(`http://localhost:${serverPort}/connect?tab_id=${tabId}`).catch(()=>{});
  }

  // Inject style rule to push body margin-right to accommodate the sidebar
  function injectSidebarStyles() {
    const savedWidth = parseInt(localStorage.getItem("z115_sidebar_width") || "280", 10);
    
    let style = document.getElementById("z115-sidebar-styles");
    if (!style) {
      style = document.createElement("style");
      style.id = "z115-sidebar-styles";
      document.head.appendChild(style);
    }
    
    style.innerHTML = `
      body {
        margin-right: ${savedWidth}px !important;
        width: calc(100% - ${savedWidth}px) !important;
        transition: margin-right 0.1s ease, width 0.1s ease !important;
      }
      header, .gb_Sd, .gb_Td {
        margin-right: ${savedWidth}px !important;
        width: calc(100% - ${savedWidth}px) !important;
        transition: margin-right 0.1s ease, width 0.1s ease !important;
      }
      /* Tự chế cuộn Terminal siêu mỏng */
      #z115-logs::-webkit-scrollbar {
        width: 4px;
      }
      #z115-logs::-webkit-scrollbar-track {
        background: rgba(0, 0, 0, 0.2);
      }
      #z115-logs::-webkit-scrollbar-thumb {
        background: rgba(255, 255, 255, 0.1);
        border-radius: 2px;
      }
    `;
  }

  // Lưu log vào localStorage history
  function saveLogToHistory(time, msg, type) {
    try {
      let history = JSON.parse(localStorage.getItem("z115_log_history") || "[]");
      history.push({ time, msg, type });
      if (history.length > 1000) {
        history.shift();
      }
      localStorage.setItem("z115_log_history", JSON.stringify(history));
    } catch (e) {
      console.error("Lỗi lưu log history:", e);
    }
  }

  // Khôi phục log lịch sử khi tải trang
  function loadAndRenderLogHistory() {
    const logsEl = document.getElementById("z115-logs");
    if (!logsEl) return;
    logsEl.innerHTML = "";
    try {
      const history = JSON.parse(localStorage.getItem("z115_log_history") || "[]");
      history.forEach(item => {
        let color = "#cbd5e1";
        if (item.type === "error") color = "#fca5a5";
        if (item.type === "success") color = "#86efac";
        if (item.type === "warning") color = "#fde047";
        if (item.type === "action") color = "#a5b4fc";

        const logDiv = document.createElement("div");
        logDiv.style.cssText = `color: ${color}; margin-bottom: 5px; font-size: 11px; font-family: 'Consolas', 'Courier New', monospace;`;
        logDiv.innerText = `[${item.time}] ${item.msg}`;
        logsEl.appendChild(logDiv);
      });
      logsEl.scrollTop = logsEl.scrollHeight;
    } catch (e) {
      console.error("Lỗi nạp log history:", e);
    }
  }

  // Ghi nhật ký vào terminal nội bộ của Sidebar
  function addSidebarLog(msg, type = "info", saveToHistory = true) {
    const logsEl = document.getElementById("z115-logs");
    if (!logsEl) return;
    const time = new Date().toLocaleTimeString([], { hour: '2-digit', minute: '2-digit', second: '2-digit' });
    
    if (saveToHistory) {
      saveLogToHistory(time, msg, type);
    }
    
    // Kiểm tra xem người dùng có đang cuộn sát đáy không trước khi chèn log mới
    const isAtBottom = (logsEl.scrollHeight - logsEl.clientHeight - logsEl.scrollTop) < 50;
    
    let color = "#cbd5e1"; // Mặc định: slate-300
    if (type === "error") color = "#fca5a5"; // Đỏ nhạt
    if (type === "success") color = "#86efac"; // Xanh lá nhạt
    if (type === "warning") color = "#fde047"; // Vàng nhạt
    if (type === "action") color = "#a5b4fc"; // Tím nhạt

    const logDiv = document.createElement("div");
    logDiv.style.cssText = `color: ${color}; margin-bottom: 5px; font-size: 11px; font-family: 'Consolas', 'Courier New', monospace;`;
    logDiv.innerText = `[${time}] ${msg}`;
    
    logsEl.appendChild(logDiv);
    
    // Chỉ tự động cuộn xuống đáy nếu người dùng đang ở sát đáy
    if (isAtBottom) {
      logsEl.scrollTop = logsEl.scrollHeight;
    }
  }

  // Tạo UI Thanh Bên Cạnh (Sidebar) cố định và ổn định 100% trên trang
  function injectControlPanel() {
    if (document.getElementById("z115-panel")) return;

    // Kích hoạt style căn lề trang Gemini
    injectSidebarStyles();

    const savedWidth = parseInt(localStorage.getItem("z115_sidebar_width") || "280", 10);

    const panel = document.createElement("div");
    panel.id = "z115-panel";
    panel.style.cssText = `
      position: fixed;
      top: 0;
      right: 0;
      width: ${savedWidth}px;
      height: 100vh;
      background: #0d0e16;
      border-left: 1px solid rgba(255, 255, 255, 0.08);
      padding: 20px 16px;
      color: #fff;
      font-family: 'Inter', -apple-system, BlinkMacSystemFont, system-ui, sans-serif;
      font-size: 13px;
      z-index: 100000;
      box-shadow: -8px 0 32px rgba(0, 0, 0, 0.5);
      display: flex;
      flex-direction: column;
      gap: 16px;
      box-sizing: border-box;
    `;

    // Thanh kéo resize cạnh trái
    const resizeHandle = document.createElement("div");
    resizeHandle.id = "z115-resize-handle";
    resizeHandle.style.cssText = `
      position: absolute;
      left: 0;
      top: 0;
      width: 4px;
      height: 100%;
      cursor: ew-resize;
      z-index: 100001;
      background: transparent;
      transition: background 0.2s;
    `;
    
    // Sáng xanh nhẹ khi di chuột vào
    resizeHandle.onmouseenter = () => { resizeHandle.style.background = "rgba(77, 136, 255, 0.3)"; };
    resizeHandle.onmouseleave = () => { if (!isResizing) resizeHandle.style.background = "transparent"; };
    panel.appendChild(resizeHandle);

    // Tiêu đề Sidebar
    const header = document.createElement("div");
    header.style.cssText = `
      display: flex;
      justify-content: space-between;
      align-items: center;
      font-weight: 700;
      border-bottom: 1px solid rgba(255, 255, 255, 0.08);
      padding-bottom: 10px;
      font-size: 15px;
      color: #4d88ff;
    `;
    header.innerHTML = `
      <span>🤖 Z115 Assistant</span>
      <span style="font-size:10px; color:#8286a6; font-family:monospace;">Tab: ${tabShort}</span>
      <span id="z115-dot" style="width: 10px; height: 10px; border-radius: 50%; background: #ff4d4d; display: inline-block; transition: all 0.3s ease;"></span>
    `;
    panel.appendChild(header);

    // Khu vực Trạng thái
    const statusSection = document.createElement("div");
    statusSection.style.cssText = `
      background: rgba(255, 255, 255, 0.03);
      border: 1px solid rgba(255, 255, 255, 0.05);
      border-radius: 8px;
      padding: 12px;
      display: flex;
      flex-direction: column;
      gap: 6px;
    `;
    statusSection.innerHTML = `
      <div style="font-size: 10px; text-transform: uppercase; letter-spacing: 0.8px; color: #8286a6; font-weight: 600;">Trạng thái</div>
      <div id="z115-desc" style="font-weight: 500; font-size: 12px; line-height: 1.4; color: #e2e8f0; white-space: pre-wrap;">${statusMessage}</div>
    `;
    panel.appendChild(statusSection);

    // Khu vực Gán Luồng Chạy
    const jobSection = document.createElement("div");
    jobSection.style.cssText = `
      display: flex;
      flex-direction: column;
      gap: 8px;
    `;
    
    const jobLabel = document.createElement("div");
    jobLabel.style.cssText = "font-size: 10px; text-transform: uppercase; letter-spacing: 0.8px; color: #8286a6; font-weight: 600;";
    jobLabel.innerText = "Gán luồng chạy";
    jobSection.appendChild(jobLabel);

    const btnContainer = document.createElement("div");
    btnContainer.style.cssText = "display: flex; gap: 8px;";

    const btnConnect = document.createElement("button");
    btnConnect.id = "z115-btn-connect";
    btnConnect.innerText = "🌐 Nối Trình Duyệt Với Tool";
    applyBtnStyle(btnConnect, activeJob !== null);
    btnConnect.onclick = async () => {
      if (activeJob) {
        selectJob(null);
      } else {
        // Gọi /connect để chiếm quyền, đá hết tab cũ ra
        try {
          addSidebarLog("🔌 Đang chiếm quyền điều khiển...", "action");
          await fetchBridge(`http://localhost:${serverPort}/connect?tab_id=${tabId}`);
          addSidebarLog("✅ Đã chiếm quyền thành công! Tab này là duy nhất được nhận lệnh.", "success");
        } catch(e) {
          addSidebarLog("⚠️ Không kết nối được với Python Server: " + e.message, "error");
        }
        selectJob("auto");
      }
    };

    btnContainer.appendChild(btnConnect);
    jobSection.appendChild(btnContainer);

    const footer = document.createElement("div");
    footer.id = "z115-footer";
    footer.style.cssText = "font-size: 11px; text-align: center; color: #8286a6; padding-top: 4px; font-style: italic;";
    
    if (activeJob) {
      footer.innerText = `Đang lắng nghe: ${activeJob === "file1" ? "LUỒNG FILE 1" : "LUỒNG FILE 2"}`;
      footer.style.color = "#4d88ff";
      footer.style.fontWeight = "bold";
    } else {
      footer.innerText = "Chưa gán Tab cho luồng chạy";
    }
    jobSection.appendChild(footer);
    panel.appendChild(jobSection);

    // Khu vực LIVE LOGS TERMINAL (Độc quyền VIP)
    const logsSection = document.createElement("div");
    logsSection.style.cssText = `
      display: flex;
      flex-direction: column;
      gap: 8px;
      flex: 1;
      min-height: 0;
    `;
    
    // Header log có nút xóa log
    const logsHeader = document.createElement("div");
    logsHeader.style.cssText = `
      display: flex;
      justify-content: space-between;
      align-items: center;
      font-size: 10px;
      text-transform: uppercase;
      letter-spacing: 0.8px;
      color: #8286a6;
      font-weight: 600;
    `;
    logsHeader.innerHTML = `
      <span>Nhật ký hoạt động</span>
      <span id="z115-clear-logs" style="cursor: pointer; color: #ff4d4d; font-size: 9px; font-weight: bold; text-transform: none; transition: color 0.2s;">Xóa log</span>
    `;
    logsSection.appendChild(logsHeader);
    
    const logsEl = document.createElement("div");
    logsEl.id = "z115-logs";
    logsEl.style.cssText = `
      flex: 1;
      background: #06070a;
      border: 1px solid rgba(255, 255, 255, 0.05);
      border-radius: 8px;
      padding: 10px;
      overflow-y: auto;
      display: flex;
      flex-direction: column;
      gap: 4px;
      box-sizing: border-box;
    `;
    logsSection.appendChild(logsEl);
    panel.appendChild(logsSection);

    document.body.appendChild(panel);

    // Click xóa log thủ công
    const clearLogsBtn = logsHeader.querySelector("#z115-clear-logs");
    if (clearLogsBtn) {
      clearLogsBtn.onmouseenter = () => { clearLogsBtn.style.color = "#ff3333"; };
      clearLogsBtn.onmouseleave = () => { clearLogsBtn.style.color = "#ff4d4d"; };
      clearLogsBtn.onclick = () => {
        if (confirm("Bạn có chắc chắn muốn xóa toàn bộ lịch sử log?")) {
          localStorage.removeItem("z115_log_history");
          loadAndRenderLogHistory();
          addSidebarLog("Đã xóa toàn bộ lịch sử log.", "success");
        }
      };
    }

    // Nạp lịch sử log cũ trước
    loadAndRenderLogHistory();

    // In lời chào khởi động vào logs terminal (không lưu vào history để tránh spam khi F5)
    addSidebarLog("Khởi động Z115 Assistant Sidebar...", "success", false);
    if (activeJob) {
      addSidebarLog(`Tự động liên kết: Luồng ${activeJob.toUpperCase()}`, "success", false);
    }

    // Logic Resize Sidebar kéo giãn
    let isResizing = false;
    resizeHandle.onmousedown = (e) => {
      isResizing = true;
      document.body.style.cursor = "ew-resize";
      document.body.style.userSelect = "none";
      resizeHandle.style.background = "rgba(77, 136, 255, 0.5)";
    };

    document.addEventListener("mousemove", (e) => {
      if (!isResizing) return;
      const newWidth = window.innerWidth - e.clientX;
      if (newWidth >= 200 && newWidth <= 800) {
        panel.style.width = `${newWidth}px`;
        localStorage.setItem("z115_sidebar_width", newWidth);
        injectSidebarStyles(); // Đồng bộ ngay lập tức trang Gemini
      }
    });

    document.addEventListener("mouseup", () => {
      if (isResizing) {
        isResizing = false;
        document.body.style.cursor = "";
        document.body.style.userSelect = "";
        resizeHandle.style.background = "transparent";
      }
    });
  }

  function applyBtnStyle(btn, isActive) {
    btn.style.cssText = `
      flex: 1;
      padding: 8px 10px;
      border: 1px solid ${isActive ? '#4d88ff' : 'rgba(255, 255, 255, 0.12)'};
      background: ${isActive ? 'rgba(77, 136, 255, 0.2)' : 'rgba(255, 255, 255, 0.03)'};
      color: ${isActive ? '#fff' : '#8286a6'};
      border-radius: 6px;
      cursor: pointer;
      font-size: 11px;
      font-weight: 700;
      transition: all 0.2s ease;
      outline: none;
      outline: none;
    `;
  }

  // ── Audio Keep-Alive (Chống Chrome đóng băng tab) ──
  let audioCtx = null;
  let oscillator = null;

  function startKeepAlive() {
    if (audioCtx) return;
    try {
      audioCtx = new (window.AudioContext || window.webkitAudioContext)();
      oscillator = audioCtx.createOscillator();
      const gainNode = audioCtx.createGain();
      
      oscillator.type = 'sine';
      oscillator.frequency.setValueAtTime(440, audioCtx.currentTime);
      gainNode.gain.setValueAtTime(0.00001, audioCtx.currentTime); // Âm lượng cực nhỏ (không nghe thấy)
      
      oscillator.connect(gainNode);
      gainNode.connect(audioCtx.destination);
      
      oscillator.start();
      addSidebarLog("🔊 Đã bật chống đóng băng Tab (Audio Keep-Alive)", "info");
    } catch (e) {
      addSidebarLog("Không thể bật Audio Keep-Alive: " + e.message, "warning");
    }
  }

  function stopKeepAlive() {
    if (audioCtx) {
      audioCtx.close();
      audioCtx = null;
      oscillator = null;
      addSidebarLog("🔇 Đã tắt Audio Keep-Alive", "info");
    }
  }

  function selectJob(job) {
    const oldJob = activeJob;
    activeJob = job;
    sessionStorage.setItem("z115_active_job", job || "");
    
    const btnConnect = document.getElementById("z115-btn-connect");
    const footer = document.getElementById("z115-footer");

    if (btnConnect && footer) {
      applyBtnStyle(btnConnect, job !== null);
      footer.innerText = job ? "ĐANG LẮNG NGHE LỆNH..." : "CHƯA KẾT NỐI";
      footer.style.color = job ? "#4d88ff" : "#8286a6";
      footer.style.fontWeight = "bold";
      
      if (job) {
        btnConnect.innerText = "🔌 Đã Kết Nối (Bấm để Ngắt)";
      } else {
        btnConnect.innerText = "🌐 Nối Trình Duyệt Với Tool";
      }
    }

    if (oldJob !== job && job) {
      addSidebarLog("Đã bật chế độ tự động nhận lệnh từ phần mềm!", "warning");
      startKeepAlive();
      startRandomInteraction();
    } else if (!job) {
      addSidebarLog("Đã ngắt kết nối.", "error");
      stopKeepAlive();
      stopRandomInteraction();
    }
  }

  // Lưu trữ trạng thái kết nối trước đó để chống spam log
  let lastConnectionState = null;

  function updateStatus(text, success = true, actionText = "") {
    const desc = document.getElementById("z115-desc");
    const dot = document.getElementById("z115-dot");
    if (desc) {
      desc.innerText = text + (actionText ? `\n\n👉 ${actionText}` : "");
      desc.style.color = success ? "#fff" : "#ff4d4d";
    }
    if (dot) {
      dot.style.background = isConnected ? "#2ec4b6" : "#ff4d4d";
      dot.style.boxShadow = isConnected ? "0 0 10px #2ec4b6" : "none";
    }

    // Ghi log kết nối lên terminal
    if (lastConnectionState !== isConnected) {
      if (isConnected) {
        addSidebarLog("Đã kết nối thông suốt với Python Server!", "success");
      } else {
        addSidebarLog("Mất kết nối với Python Server!", "error");
      }
      lastConnectionState = isConnected;
    }
  }

  // --- Core Gemini Helpers ---
  const GeminiSelectors = {
    chat_input: [
      "rich-textarea p",
      "div.ql-editor",
      "div[contenteditable='true']"
    ],
    send_button: [
      "button[aria-label='Send message']",
      "button[aria-label='Gửi tin nhắn']",
      ".send-button"
    ],
    model_selector_button: [
      "button[aria-label*='Model']",
      "button[aria-label*='Mô hình']",
      "button[aria-haspopup='true']",
      "button:has(mat-icon[fonticon='expand_more'])",
      ".model-selector-button"
    ],
    model_option: [
      "li[role='option']",
      "mat-option",
      "[role='menuitem']",
      ".mat-mdc-menu-item",
      ".model-option"
    ],
    stop_button: [
      "button[aria-label='Stop generating']",
      "button[aria-label='Dừng tạo']"
    ],
    loading_spinner: [
      "mat-progress-spinner",
      ".generating-indicator",
      ".skeleton-loader"
    ],
    response_container: [
      "model-message message-content",
      "model-message .message-content",
      ".model-response-text",
      "model-message"
    ],
    regenerate_button: [
      "button[aria-label='Show drafts']",
      "button[aria-label='Xem bản nháp']",
      "button[aria-label='Regenerate draft']",
      "button[aria-label='Thử lại']",
      "button[aria-label='Modify response']",
      "button[aria-label='Điều chỉnh phản hồi']"
    ],
    regenerate_option: [
      "li[role='menuitem']",
      "button"
    ],
    popup_close_button: [
      "button[aria-label='Close']",
      "button[aria-label='Đóng']",
      ".close-button"
    ]
  };

  function sleep(ms) {
    return new Promise(resolve => setTimeout(resolve, ms));
  }

  function getSelectorString(selectorKey) {
    const selectors = GeminiSelectors[selectorKey] || window.GeminiSelectors?.[selectorKey];
    if (!selectors) return "";
    return selectors.join(",");
  }

  function findElement(selectorKey) {
    const selStr = getSelectorString(selectorKey);
    if (!selStr) return null;
    return document.querySelector(selStr);
  }

  function findAllElements(selectorKey) {
    const selStr = getSelectorString(selectorKey);
    if (!selStr) return [];
    return Array.from(document.querySelectorAll(selStr));
  }

  async function waitForElement(selectorKey, timeout = 15000) {
    const start = Date.now();
    const selStr = getSelectorString(selectorKey);
    if (!selStr) return null;
    while (Date.now() - start < timeout) {
      const el = document.querySelector(selStr);
      if (el && (el.offsetHeight > 0 || el.offsetWidth > 0 || el.getAttribute("contenteditable") === "true")) {
        return el;
      }
      await sleep(500);
    }
    return null;
  }

  async function tryClosePopups() {
    const closeBtns = findAllElements("popup_close_button");
    for (const btn of closeBtns) {
      if (btn.offsetParent !== null) {
        btn.click();
        await sleep(500);
      }
    }
  }

  function findModelSelectorButton() {
    let btn = findElement("model_selector_button");
    if (btn && btn.offsetParent !== null) return btn;
    
    const allBtns = Array.from(document.querySelectorAll('button'));
    return allBtns.find(b => {
      const txt = (b.innerText || b.textContent || "").toLowerCase();
      return (txt.includes("pro") || txt.includes("flash") || txt.includes("mở rộng") || txt.includes("nhanh") || txt.includes("model") || txt.includes("mô hình")) && b.offsetParent !== null;
    });
  }

  function findMenuItemByText(textList) {
    const candidates = Array.from(document.querySelectorAll("[role='option'], [role='menuitem'], mat-option, .mat-mdc-menu-item, li, button, div, span"));
    return candidates.find(el => {
      if (el.offsetParent === null) return false;
      const txt = (el.innerText || el.textContent || "").trim().toLowerCase();
      return textList.some(target => {
        const cleanTarget = target.toLowerCase();
        if (cleanTarget === "3.1 pro") {
          return txt === cleanTarget || txt.includes("3.1 pro") || (txt.includes("pro") && !txt.includes("mở rộng") && !txt.includes("tiêu chuẩn") && !txt.includes("tư duy"));
        }
        return txt === cleanTarget || txt.includes(cleanTarget);
      });
    });
  }

  // ── Chrome Debugger Helpers: Tạo event chuột thật (isTrusted=true) ──
  function _sendDebugCmd(cmd, x, y) {
    return new Promise((resolve, reject) => {
      chrome.runtime.sendMessage({ cmd, x: Math.round(x), y: Math.round(y) }, response => {
        if (chrome.runtime.lastError) {
          reject(new Error(chrome.runtime.lastError.message));
        } else if (response && response.success) {
          resolve();
        } else {
          reject(new Error(response ? response.error : "Unknown debugger error"));
        }
      });
    });
  }

  function _sendDebugScroll(x, y, deltaX, deltaY) {
    return new Promise((resolve, reject) => {
      chrome.runtime.sendMessage({ cmd: "debug_scroll", x, y, deltaX, deltaY }, response => {
        if (chrome.runtime.lastError) {
          reject(new Error(chrome.runtime.lastError.message));
        } else if (response && response.success) {
          resolve();
        } else {
          reject(new Error(response ? response.error : "Unknown debugger error"));
        }
      });
    });
  }

  // ── Random Wakeup Interaction ──
  let randomInteractionInterval = null;

  function startRandomInteraction() {
    if (randomInteractionInterval) return;
    randomInteractionInterval = setInterval(async () => {
      if (!activeJob) return; // Chỉ chạy khi đang nối tool
      // Bỏ qua nếu đang xử lý chunk
      if (typeof currentlyProcessingChunk !== 'undefined' && currentlyProcessingChunk) return;

      try {
        const safeX = 10;
        const safeY = Math.floor(window.innerHeight / 2);
        const deltaY = (Math.random() > 0.5 ? 1 : -1) * (Math.floor(Math.random() * 100) + 50);
        
        await _sendDebugScroll(safeX, safeY, 0, deltaY);
        setTimeout(async () => {
          await _sendDebugScroll(safeX, safeY, 0, -deltaY);
        }, 1000);

        if (Math.random() > 0.5) {
          setTimeout(async () => {
            await _sendDebugCmd("debug_click", safeX, safeY);
          }, 500);
        }
      } catch (e) {
        console.warn("[Z115] Random interaction failed", e);
      }
    }, 45000); // 45 giây/lần
  }

  function stopRandomInteraction() {
    if (randomInteractionInterval) {
      clearInterval(randomInteractionInterval);
      randomInteractionInterval = null;
    }
  }

  async function debugHover(el) {
    const rect = el.getBoundingClientRect();
    const x = rect.left + rect.width / 2;
    const y = rect.top + rect.height / 2;
    await _sendDebugCmd("debug_mouse_move", x, y);
  }

  async function debugClick(el) {
    const rect = el.getBoundingClientRect();
    const x = rect.left + rect.width / 2;
    const y = rect.top + rect.height / 2;
    await _sendDebugCmd("debug_click", x, y);
  }

  async function ensureModelAndThinking() {
    addSidebarLog("Đang thiết lập Model 'Pro' & Tư duy 'Mở rộng'...", "info");
    
    for (let attempt = 1; attempt <= 3; attempt++) {
      try {
        // Kiểm tra nhanh: nút model ở thanh dưới đã hiển thị đúng chưa?
        let btn = findModelSelectorButton();
        if (!btn) {
          if (attempt < 3) {
            addSidebarLog(`Chưa thấy nút model, thử lại lần ${attempt + 1}/3...`, "warning");
          }
          if (attempt === 3) return false;
          await sleep(2000);
          continue;
        }

      const currentLabel = (btn.innerText || btn.textContent || "").toLowerCase();
      if (currentLabel.includes("pro") && (currentLabel.includes("mở rộng") || currentLabel.includes("extended"))) {
        addSidebarLog("Model đã đúng 'Pro & Mở rộng'. Bỏ qua.", "success");
        return true;
      }

      // ── BƯỚC A: Click nút model ở thanh dưới để mở menu ──
      await debugClick(btn);
      await sleep(1200);

      // ── BƯỚC B: Kiểm tra Pro đã chọn chưa ──
      const proAlreadySelected = currentLabel.includes("pro");
      if (proAlreadySelected) {
      } else {
        const optPro = _findProMenuItem();
        if (optPro) {
          await debugClick(optPro);
          await sleep(1500);
          
          // Sau khi click Pro, menu có thể đóng → mở lại
          btn = findModelSelectorButton();
          if (btn) {
            await debugClick(btn);
            await sleep(1200);
          }
        }
      }
      // ── BƯỚC C: Hover vào "Cấp độ tư duy" để mở submenu bên cạnh ──
      let capDoItem = _findCapDoTuDuyItem();
      if (!capDoItem) {
        // Menu có thể đã đóng → mở lại
        btn = findModelSelectorButton();
        if (btn) {
          await debugClick(btn);
          await sleep(1200);
          capDoItem = _findCapDoTuDuyItem();
        }
      }

      if (!capDoItem) {
        document.body.dispatchEvent(new KeyboardEvent('keydown', { key: 'Escape', bubbles: true }));
        if (attempt < 3) {
          addSidebarLog(`Chưa mở được menu tư duy, thử lại lần ${attempt + 1}/3...`, "warning");
          await sleep(1000);
          continue;
        }
        return false;
      }

      // Di chuột thật vào "Cấp độ tư duy" qua Chrome Debugger
      const rect = capDoItem.getBoundingClientRect();
      // Hover vào MÉP PHẢI của hàng (gần mũi tên ►) để kích hoạt submenu
      const hoverX = rect.right - 15;
      const hoverY = rect.top + rect.height / 2;

      // Di chuột từ xa đến gần, giống người thật
      await _sendDebugCmd("debug_mouse_move", rect.left - 30, hoverY);
      await sleep(150);
      await _sendDebugCmd("debug_mouse_move", rect.left + rect.width * 0.3, hoverY);
      await sleep(150);
      await _sendDebugCmd("debug_mouse_move", hoverX, hoverY);
      await sleep(2000);

      // ── BƯỚC D: Click "Mở rộng" trong submenu bên cạnh ──
      let moRongItem = _findMoRongSubmenuItem();
      
      if (!moRongItem) {
        // Di chuột ra ngoài rồi vào lại
        await _sendDebugCmd("debug_mouse_move", rect.left - 50, hoverY - 20);
        await sleep(400);
        await _sendDebugCmd("debug_mouse_move", hoverX, hoverY);
        await sleep(2000);
        moRongItem = _findMoRongSubmenuItem();
      }

      if (moRongItem) {
        await debugClick(moRongItem);
        await sleep(1000);
      } else {
        document.body.dispatchEvent(new KeyboardEvent('keydown', { key: 'Escape', bubbles: true }));
        if (attempt < 3) {
          addSidebarLog(`Chưa chọn được 'Mở rộng', thử lại lần ${attempt + 1}/3...`, "warning");
          await sleep(1000);
          continue;
        }
        addSidebarLog("Không chọn được 'Mở rộng' trong submenu.", "error");
      }

      // ── Kiểm tra kết quả cuối ──
      await sleep(800);
      btn = findModelSelectorButton();
      if (btn) {
        const finalCheck = (btn.innerText || btn.textContent || "").toLowerCase();
        if (finalCheck.includes("pro") && (finalCheck.includes("mở rộng") || finalCheck.includes("extended"))) {
          addSidebarLog("✅ Đã chọn thành công 'Pro Mở rộng'!", "success");
          return true;
        } else {
          if (attempt < 3) {
            addSidebarLog(`Thiết lập chưa đúng, thử lại lần ${attempt + 1}/3...`, "warning");
            document.body.dispatchEvent(new KeyboardEvent('keydown', { key: 'Escape', bubbles: true }));
            await sleep(1000);
          }
        }
      }
    } catch (e) {
      if (attempt < 3) {
        addSidebarLog(`Lỗi thiết lập Pro/Mở rộng, thử lại lần ${attempt + 1}/3...`, "warning");
      } else {
        addSidebarLog(`Lỗi thiết lập Model & Tư duy: ${e.message}`, "error");
      }
      await sleep(1000);
    }
    } // end for loop
    
    addSidebarLog("Không thiết lập được 'Pro Mở rộng' sau 3 lần thử.", "error");
    return false;
  }

  /** Tìm option Pro đang hiển thị trong menu (3.1 Pro, 3.2 Pro, ...) */
  function _findProMenuItem() {
    const all = Array.from(document.querySelectorAll(
      "[role='option'], [role='menuitem'], mat-option, li"
    ));
    const found = all.find(el => {
      if (el.offsetParent === null) return false;
      // Chỉ kiểm tra DÒNG ĐẦU TIÊN để tránh lẫn với text con "Cấp độ tư duy"
      const txt = (el.innerText || el.textContent || "").trim();
      const firstLine = txt.split("\n")[0].toLowerCase();
      return firstLine.includes("pro") && !firstLine.includes("tư duy") && !firstLine.includes("thinking");
    });
    return found || null;
  }

  /** Tìm hàng menu chứa "Cấp độ tư duy" (trả về thẻ hàng lớn, không phải thẻ text nhỏ) */
  function _findCapDoTuDuyItem() {
    // Bước 1: Tìm tất cả element có chứa text "cấp độ tư duy"
    const allVisible = Array.from(document.querySelectorAll("*")).filter(el => {
      if (el.offsetParent === null) return false;
      const txt = (el.innerText || el.textContent || "").toLowerCase();
      return txt.includes("cấp độ tư duy") || txt.includes("thinking mode") || txt.includes("thinking budget");
    });
    if (!allVisible.length) return null;
    
    // Bước 2: Tìm element sâu nhất (nhỏ nhất)
    const deepest = allVisible.reduce((a, b) => (a.contains(b) ? b : a));
    
    // Bước 3: Leo lên DOM tìm thẻ CHA là hàng menu (có role hoặc là li/button)
    let cur = deepest;
    for (let i = 0; i < 6 && cur && cur !== document.body; i++) {
      const role = cur.getAttribute && cur.getAttribute('role');
      const tag = cur.tagName.toLowerCase();
      // Tìm thẻ có role menu-related hoặc là li/button với chiều cao hợp lý (> 30px)
      if ((role === 'option' || role === 'menuitem' || role === 'menuitemradio' || tag === 'li' || tag === 'mat-option') 
          && cur.getBoundingClientRect().height >= 30) {
          return cur;
      }
      cur = cur.parentElement;
    }
    
    // Fallback: trả về cha trực tiếp của text
    if (deepest.parentElement && deepest.parentElement.getBoundingClientRect().height >= 20) {
      addSidebarLog(`[CapDo] Fallback: dùng cha trực tiếp <${deepest.parentElement.tagName}>`, "warning");
      return deepest.parentElement;
    }
    return deepest;
  }

  /** Click/Hover đa tầng (Brute-force): Gửi event lên cả dòng họ (từ thẻ con lên thẻ cha) */
  function _bruteForceInteract(deepestEl, logName, actionType = "click") {
    if (!deepestEl) return;
    let els = [];
    let cur = deepestEl;
    while (cur && cur !== document.body && els.length < 4) {
      els.push(cur);
      cur = cur.parentElement;
    }
    
    // Gửi Hover
    els.forEach(el => {
      ["pointerover", "pointerenter", "mouseover", "mouseenter", "mousemove"].forEach(evt => {
        try { el.dispatchEvent(new MouseEvent(evt, { bubbles: true, cancelable: true, view: window })); } catch(e){}
      });
    });
    
    if (actionType === "hover") {
      return; // Chỉ hover, không click
    }
    
    // Gửi Click
    els.forEach(el => {
      ["pointerdown", "mousedown", "pointerup", "mouseup"].forEach(evt => {
        try { el.dispatchEvent(new MouseEvent(evt, { bubbles: true, cancelable: true, view: window })); } catch(e){}
      });
      try { el.click(); } catch(e){}
      try { el.dispatchEvent(new MouseEvent("click", { bubbles: true, cancelable: true, view: window })); } catch(e){}
    });
  }

  /** Click mạnh: gửi đầy đủ pointer+mouse events + native click */
  function _strongClick(el) {
    try { el.focus(); } catch(e) {}
    ["pointerdown", "mousedown", "pointerup", "mouseup"].forEach(evtName => {
      el.dispatchEvent(new MouseEvent(evtName, { bubbles: true, cancelable: true, view: window }));
    });
    el.click();
    el.dispatchEvent(new MouseEvent("click", { bubbles: true, cancelable: true, view: window }));
  }



  function _findMoRongSubmenuItem() {
    const keywords = ["mở rộng", "extended", "giải quyết vấn đề phức tạp", "solve complex"];
    const excludeParent = ["cấp độ tư duy", "thinking mode", "thinking budget"];

    const allVisible = Array.from(document.querySelectorAll("*")).filter(el => {
      if (el.offsetParent === null) return false;
      const txt = (el.innerText || el.textContent || "").trim().toLowerCase();
      // Phải chứa ít nhất 1 keyword Mở rộng, và KHÔNG chứa chữ Cấp độ tư duy
      return keywords.some(k => txt.includes(k)) && !excludeParent.some(p => txt.includes(p));
    });

    if (allVisible.length > 0) {
      // 1. Tìm thẻ có text ngắn nhất (sâu nhất)
      const deepest = allVisible.reduce((a, b) => 
        (a.innerText || a.textContent || "").length <= (b.innerText || b.textContent || "").length ? a : b
      );
      
      // 2. Leo lên DOM tìm thẻ cha là hàng menu (role=menuitem, option, li)
      let cur = deepest;
      for (let i = 0; i < 6 && cur && cur !== document.body; i++) {
        const role = cur.getAttribute && cur.getAttribute('role');
        const tag = cur.tagName.toLowerCase();
        if ((role === 'menuitem' || role === 'menuitemradio' || role === 'option' || tag === 'li' || tag === 'button') 
            && cur.getBoundingClientRect().height >= 25) {
          return cur;
        }
        cur = cur.parentElement;
      }
      
      return deepest;
    }

    return null;
  }

  async function ensureModel(targetModel) {
    if (!targetModel) return true;

    // Đặc biệt: nếu Python yêu cầu model "Pro" (mặc định), chúng ta sẽ chọn "3.1 Pro" và "Mở rộng" như anh mong muốn!
    if (targetModel && targetModel.toLowerCase() === "pro") {
      return await ensureModelAndThinking();
    }

    addSidebarLog(`Đang kiểm tra model ưu tiên: '${targetModel}'...`, "info");
    try {
      let btn = findElement("model_selector_button");
      
      if (!btn) {
        const allBtns = Array.from(document.querySelectorAll('button'));
        btn = allBtns.find(b => {
          const txt = (b.innerText || b.textContent || "").toLowerCase();
          return (txt.includes("pro") || txt.includes("nhanh") || txt.includes("model") || txt.includes("mô hình")) && b.offsetParent !== null;
        });
      }

      if (btn) {
        const current = btn.innerText || btn.textContent || "";
        if (current.toLowerCase().includes(targetModel.toLowerCase()) && !current.toLowerCase().includes("nhanh")) {
          addSidebarLog("Model đã đúng.", "success");
          return true;
        }
        btn.click();
        await sleep(800);
      } else {
        addSidebarLog("Không tìm thấy nút chọn model. Thử tìm trực tiếp option...", "warning");
      }

      const options = findAllElements("model_option");
      for (const opt of options) {
        const optText = opt.innerText || opt.textContent || "";
        if (optText.toLowerCase().includes(targetModel.toLowerCase())) {
          opt.click();
          await sleep(500);
          addSidebarLog(`Đã chọn model ${targetModel}.`, "success");
          return true;
        }
      }
      
      const textNodes = Array.from(document.querySelectorAll("*")).filter(el => {
        return el.children.length === 0 && el.textContent.toLowerCase().includes(targetModel.toLowerCase()) && el.offsetParent !== null;
      });
      if (textNodes.length > 0) {
        textNodes[textNodes.length - 1].click();
        await sleep(500);
        addSidebarLog(`Đã chọn model ${targetModel} (qua text node).`, "success");
        return true;
      }

    } catch (e) {
      addSidebarLog(`Lỗi chọn model: ${e.message}`, "error");
    }
    return false;
  }

  async function insertText(content) {
    const input = findElement("chat_input");
    if (!input) throw new Error("Không tìm thấy ô nhập liệu (chat_input).");
    
    input.focus();
    
    // Tách các dòng sạch sẽ, loại bỏ khoảng trắng thừa
    const lines = content.split(/\r?\n/).map(l => l.trim()).filter(l => l.length > 0);
    const plainText = lines.join("\r\n\r\n");
    const htmlText = lines.map(line => `<div>${line}</div>`).join('<div><br></div>');
    
    // Giả lập sao chép text vào clipboard hệ thống thật giống hệt người dùng vừa nhấn Ctrl+C
    try {
      if (typeof ClipboardItem !== 'undefined') {
        const blobText = new Blob([plainText], { type: "text/plain" });
        const blobHtml = new Blob([htmlText], { type: "text/html" });
        const item = new ClipboardItem({
          "text/plain": blobText,
          "text/html": blobHtml
        });
        await navigator.clipboard.write([item]);
        addSidebarLog("📋 [Người thật] Đã sao chép PlainText & HTML vào Clipboard thật...", "success");
      } else {
        await navigator.clipboard.writeText(plainText);
        addSidebarLog("📋 [Người thật] Đã sao chép PlainText vào Clipboard thật...", "success");
      }
    } catch (e) {
      addSidebarLog("⚠️ Lỗi sao chép clipboard hệ thống, dùng clipboard giả lập...", "warning");
    }
    
    // Clear content
    document.execCommand('selectAll', false, null);
    document.execCommand('delete', false, null);
    await sleep(200);
    
    const dataTransfer = new DataTransfer();
    dataTransfer.setData('text/plain', plainText);
    dataTransfer.setData('text/html', htmlText);
    
    const pasteEvent = new ClipboardEvent('paste', {
      clipboardData: dataTransfer,
      bubbles: true,
      cancelable: true
    });
    
    input.dispatchEvent(pasteEvent);
    
    await sleep(300);
    
    if (!input.innerText.trim() && !input.textContent.trim()) {
      const success = document.execCommand('insertText', false, plainText);
      if (!success) {
        input.textContent = plainText;
        input.dispatchEvent(new Event('input', { bubbles: true }));
      }
    }
    await sleep(500);
  }

  function getResponseContainers() {
    return findAllElements("response_container");
  }

  function readResponseText(container) {
    if (!container) return "";
    return container.innerText || container.textContent || "";
  }

  function getLatestResponseSnapshot() {
    const containers = getResponseContainers();
    const latest = containers.length > 0 ? containers[containers.length - 1] : null;
    return {
      count: containers.length,
      text: readResponseText(latest)
    };
  }

  function getResponseCandidateAfterBaseline(baseline) {
    const base = baseline || { count: 0, text: "" };
    const containers = getResponseContainers();
    let container = null;
    let index = -1;

    if (containers.length > base.count) {
      index = base.count;
      container = containers[index];
    } else if (containers.length > 0) {
      const latest = containers[containers.length - 1];
      const latestText = readResponseText(latest);
      if (latestText && latestText !== base.text) {
        index = containers.length - 1;
        container = latest;
      }
    }

    if (!container) {
      return null;
    }

    return {
      container,
      index,
      count: containers.length,
      text: readResponseText(container)
    };
  }

  function getLatestResponseText() {
    const containers = getResponseContainers();
    if (containers.length === 0) return "";
    return readResponseText(containers[containers.length - 1]);
  }

  function getLatestCodeBlocks(container = null) {
    if (!container) {
      const containers = getResponseContainers();
      if (containers.length === 0) return [];
      container = containers[containers.length - 1];
    }

    let blocks = Array.from(container.querySelectorAll('pre'));
    if (blocks.length === 0) {
      blocks = Array.from(container.querySelectorAll('code-block'));
    }

    let validBlocks = blocks.filter(b1 => {
      for (let b2 of blocks) {
        if (b1 !== b2 && b1.contains(b2)) return false; 
      }
      return true;
    });

    return validBlocks.map(b => {
      let txt = b.innerText || b.textContent || "";
      txt = txt.replace(/^(Plaintext|Python|Javascript|HTML|C\+\+|Java|Copy code|Sao chép mã|\n)+/i, "");
      return txt.trim();
    });
  }

  function getCodeLinesCount(blocks, blockIndex = 0) {
    if (blockIndex < blocks.length) {
      const text = blocks[blockIndex];
      const lines = text.split('\n').filter(l => l.trim().length > 0);
      return lines.length;
    }
    return 0;
  }

  // Hàm fetch trung chuyển qua Background Script để tránh CORS/Mixed Content của Chrome
  function fetchBridge(url, options = {}) {
    return new Promise((resolve, reject) => {
      let resolved = false;
      const timeoutId = setTimeout(() => {
        if (!resolved) {
          resolved = true;
          reject(new Error("Bridge request timed out after 8s"));
        }
      }, 8000);

      chrome.runtime.sendMessage({ cmd: "fetch_bridge", url, options }, response => {
        if (resolved) return;
        resolved = true;
        clearTimeout(timeoutId);

        if (chrome.runtime.lastError) {
          reject(new Error(chrome.runtime.lastError.message));
          return;
        }
        if (response && response.success) {
          resolve(response.data);
        } else {
          reject(new Error(response ? response.error : "Unknown bridge error"));
        }
      });
    });
  }

  // --- HTTP Bridge Polling Loop ---
  let currentlyProcessingChunk = false;
  let _currentJobId = ""; // Luư luồng hiện tại (file1/file2) do server trả về

  async function pollLoop() {
    injectControlPanel();

    // Không poll nếu chưa bấm kết nối
    if (!activeJob) {
      updateStatus("Chưa kết nối. Bấm nút '🌐 Nối Trình Duyệt Với Tool' để bắt đầu.", true, "");
      setTimeout(pollLoop, 2000);
      return;
    }

    if (currentlyProcessingChunk) {
      // Vẫn gửi poll ngầm để Python biết Tab vẫn còn sống (Heartbeat), tránh bị timeout 10s
      fetchBridge(`http://localhost:${serverPort}/poll?job=${activeJob || ''}&tab_id=${tabId}`).catch(()=>{});
      setTimeout(pollLoop, 1000);
      return;
    }

    try {
      const pollUrl = `http://localhost:${serverPort}/poll?job=${activeJob || ''}&tab_id=${tabId}`;
      const data = await fetchBridge(pollUrl);
      isConnected = true;

      if (data.cmd === "send_chunk") {
        // Lưu lại luồng gốc (file1/file2) để gửi result đúng chỗ
        _currentJobId = data._from_job || activeJob || "";
        currentlyProcessingChunk = true;
        updateStatus("Đang xử lý dữ liệu...", true, `Gửi chunk ${data.chunk_index}`);
        addSidebarLog(`📩 Nhận lệnh từ luồng: ${_currentJobId.toUpperCase()} | Chunk ${data.chunk_index}`, "action");
        try {
          await handleSendChunk(data);
        } catch (e) {
          addSidebarLog("Lỗi khi xử lý chunk: " + e.message, "error");
        } finally {
          currentlyProcessingChunk = false;
        }
      } else if (data.cmd === "redirect") {
        updateStatus("Đang chuyển trang sang File 2...", true, "Chuyển hướng...");
        addSidebarLog(`🔄 Nhận lệnh từ Python: Đổi trang sang: ${data.url}`, "warning");
        setTimeout(() => {
          window.location.href = data.url;
        }, 500);
      } else if (data.designated === false) {
        // Tab khác đang được chỉ định → chờ im lặng, không làm gì
        updateStatus("⏸ Tab khác đang chạy. Tab này đang chờ...", true, "");
      } else {
        // idle + được chỉ định hoặc chưa có job
        updateStatus(activeJob ? "Chờ lệnh từ Python..." : "Vui lòng gán Tab cho luồng chạy", true, currentActionText);
      }
    } catch (err) {
      isConnected = false;
      updateStatus("Không kết nối được với Server Python. Hãy chạy app Z115 trước!", false);
    }

    setTimeout(pollLoop, 1000);
  }

  async function waitForResponse(timeoutSec = 900, baseline = null) {
    const deadline = Date.now() + timeoutSec * 1000;
    const base = baseline || getLatestResponseSnapshot();
    addSidebarLog(`Mốc response trước gửi: count=${base.count}, text=${base.text.length} ký tự`, "info");
    
    const startWaitDeadline = Date.now() + 15000;
    
    while (Date.now() < startWaitDeadline) {
      const stopBtn = findElement("stop_button");
      const spinner = findElement("loading_spinner");
      if ((stopBtn && stopBtn.offsetParent !== null) || (spinner && spinner.offsetParent !== null)) {
        updateStatus("Gemini đang khởi tạo phản hồi...", true);
        addSidebarLog("⏳ Đang tạo phản hồi...", "warning");
        break;
      }
      if (getResponseCandidateAfterBaseline(base)) {
        break;
      }
      await sleep(500);
    }
    
    let lastText = "";
    let stableSince = null;
    const stableWaitMs = 15000;
    
    while (Date.now() < deadline) {
      const stopBtn = findElement("stop_button");
      const spinner = findElement("loading_spinner");
      const sendBtn = findElement("send_button");
      
      const isGenerating = (stopBtn && stopBtn.offsetParent !== null) || (spinner && spinner.offsetParent !== null);
      const isSendBtnReady = sendBtn && !sendBtn.disabled && sendBtn.getAttribute("aria-disabled") !== "true" && sendBtn.offsetParent !== null;
      
      const isBusy = isGenerating || !isSendBtnReady;
      
      const candidate = getResponseCandidateAfterBaseline(base);
      const currentText = candidate ? candidate.text : "";
      
      if (!candidate) {
        updateStatus("Đang chờ response mới...", true, `Response: ${getResponseContainers().length}/${base.count}`);
        stableSince = null;
        lastText = "";
      } else if (!isBusy) {
        updateStatus("Đang chờ ổn định (15 giây)...", true, `Ổn định: ${currentText.length} ký tự`);
        if (currentText && currentText === lastText) {
          if (!stableSince) {
            addSidebarLog(`Phát hiện response mới #${candidate.index + 1}/${candidate.count} dừng sinh (${currentText.length} ký tự). Đang đợi 15 giây ổn định...`, "info");
            stableSince = Date.now();
          } else if (Date.now() - stableSince >= stableWaitMs) {
            addSidebarLog("Chữ chạy đã ổn định hoàn toàn!", "success");
            return candidate;
          }
        } else {
          stableSince = null;
          lastText = currentText;
        }
      } else {
        updateStatus("Gemini đang soạn thảo...", true, `Đang sinh chữ (${currentText.length} ký tự)`);
        stableSince = null;
        lastText = currentText;
      }
      await sleep(500);
    }
    throw new Error(`Không thấy response mới sau khi gửi chunk (baseline count=${base.count}, current count=${getResponseContainers().length}).`);
  }

  async function handleSendChunk(data) {
    if (data.chunk_index === 1) {
      localStorage.removeItem("z115_log_history");
      loadAndRenderLogHistory();
      addSidebarLog("🔄 Bắt đầu phiên làm việc mới - Đã xóa sạch log lịch sử cũ.", "success");
    }

    updateStatus("Đang chờ tải trang...", true, "Dọn dẹp popups...");
    addSidebarLog(`Nhận lệnh gửi chunk ${data.chunk_index} từ Python`, "action");

    // Yêu cầu: "chờ 5s ổn định gửi chunk mới"
    addSidebarLog("⏳ Đang chờ 5 giây để trang Gemini ổn định hoàn toàn...", "warning");
    await sleep(5000);

    await tryClosePopups();

    addSidebarLog("Bước 1: Tìm ô nhập văn bản (Chat Input) của Gemini...", "info");
    const inputEl = await waitForElement("chat_input", 15000);
    if (!inputEl) {
      addSidebarLog("LỖI: Không tìm thấy ô nhập chat Gemini sau 15s!", "error");
      sendResult({ status: "error", error: "Không tìm thấy ô nhập chat Gemini sau 15 giây!" });
      return;
    }

    // Yêu cầu Focus Tab trước khi dán để Clipboard API hoạt động
    await new Promise(r => chrome.runtime.sendMessage({ cmd: "focus_tab" }, r));
    await sleep(200);

    // 1. Dán văn bản TRƯỚC (vì nút chọn model chỉ hoạt động khi ô chat có nội dung)
    currentActionText = `Dán text cho chunk ${data.chunk_index}`;
    updateStatus("Đang dán văn bản...", true, currentActionText);
    addSidebarLog(`Bước 1: Bắt đầu dán (Paste) nội dung dài vào ô chat...`, "action");
    
    try {
      await insertText(data.content);
    } catch (e) {
      addSidebarLog("LỖI dán chữ: " + e.message, "error");
      sendResult({ status: "error", error: "Lỗi dán chữ: " + e.message });
      return;
    }

    // 2. SAU KHI dán xong → Luôn chọn lại model Pro + Mở rộng trước mỗi lần gửi
    updateStatus("Đang kiểm tra Model...", true, "Chọn Pro/Advanced...");
    addSidebarLog(`Bước 2: Chọn lại Model Pro và Cấp độ tư duy Mở rộng trước khi gửi chunk ${data.chunk_index}...`, "action");
    await ensureModel("Pro");

    const responseBaseline = getLatestResponseSnapshot();

    // 3. Click nút gửi
    updateStatus("Đang bấm gửi...", true, `Gửi chunk ${data.chunk_index}`);
    addSidebarLog(`Bước 3: Tìm nút Gửi (Send) và bấm click...`, "action");
    const sendBtn = findElement("send_button");
    if (sendBtn && !sendBtn.disabled && sendBtn.offsetParent !== null) {
      sendBtn.click();
    } else {
      addSidebarLog("Sử dụng phím Enter thay thế nút Gửi", "warning");
      const input = findElement("chat_input");
      input.dispatchEvent(new KeyboardEvent('keydown', { key: 'Enter', bubbles: true, cancelable: true }));
    }

    // Trả lại tab cũ sau khi đã dán và gửi xong
    chrome.runtime.sendMessage({ cmd: "restore_tab" });

    await sleep(2000);

    // 4. Đợi phản hồi ổn định
    currentActionText = `Đang đợi phản hồi từ Gemini...`;
    addSidebarLog("Bước 4: Đã gửi xong! Bắt đầu chờ Gemini sinh chữ...", "warning");
    let responseText = "";
    let responseInfo = null;
    let responseContainer = null;
    try {
      responseInfo = await waitForResponse(900, responseBaseline);
      responseText = responseInfo.text;
      responseContainer = responseInfo.container;
    } catch (e) {
      addSidebarLog("LỖI khi chờ phản hồi: " + e.message, "error");
      await sendResult({
        status: "failed_retry",
        error: e.message,
        text: "",
        blocks: []
      });
      addSidebarLog("Đang F5 reload trang để làm sạch UI sau lỗi chờ response...", "warning");
      setTimeout(async () => {
        if (data.chunk_index === 1) {
          const targetUrl = data.initial_url || "https://gemini.google.com/app";
          addSidebarLog(`🔄 Trở lại trang bắt đầu: ${targetUrl}...`, "warning");
          await forceSaveAndReload(targetUrl);
        } else {
          addSidebarLog("🔄 Tải lại đúng trang đang chat...", "warning");
          await forceSaveAndReload();
        }
      }, 2000);
      return;
    }

    let finalBlocks = getLatestCodeBlocks(responseContainer);
    
    // Kiểm tra xem đã đủ số lượng block tối thiểu chưa (Chunk 1 cần 2 blocks, các chunk sau cần 1 block)
    const minRequiredBlocks = (data.chunk_index === 1) ? 2 : 1;
    
    if (finalBlocks.length < minRequiredBlocks && data.expected_lines !== null) {
      addSidebarLog(`⏳ Chưa đủ ${minRequiredBlocks} code blocks (hiện có: ${finalBlocks.length}), chờ tối đa 450s...`, "warning");
      const cbDeadline = Date.now() + 450 * 1000;
      let elapsed = 0;
      let found = false;
      
      while (Date.now() < cbDeadline) {
        await sleep(30000);
        elapsed += 30;
        finalBlocks = getLatestCodeBlocks(responseContainer);
        
        // Đặc thù Chunk 1: đã có 1 block ổn định -> chờ tiếp block thứ 2 thêm 450s theo cấu hình hiện tại
        if (data.chunk_index === 1 && finalBlocks.length === 1) {
          addSidebarLog("✅ Đã nhận được code block 1. Chờ ổn định code block 1 trước...", "success");
          try {
            await waitForResponse(60, responseBaseline); // Chờ ổn định code block 1
            finalBlocks = getLatestCodeBlocks(responseContainer);
          } catch (e) {}
          
          if (finalBlocks.length >= 2) {
            addSidebarLog("✅ Code block 2 đã xuất hiện cùng lúc! Chờ ổn định toàn bộ...", "success");
            try {
              await waitForResponse(60, responseBaseline);
              finalBlocks = getLatestCodeBlocks(responseContainer);
            } catch (e) {}
            found = true;
            break;
          }
          
          addSidebarLog("⏳ Code block 1 đã ổn định. Bắt đầu chờ tiếp code block 2 trong tối đa 450s tiếp theo...", "warning");
          const block2Deadline = Date.now() + 450 * 1000;
          let elapsed2 = 0;
          let found2 = false;
          
          while (Date.now() < block2Deadline) {
            await sleep(30000);
            elapsed2 += 30;
            finalBlocks = getLatestCodeBlocks(responseContainer);
            
            if (finalBlocks.length >= 2) {
              addSidebarLog(`✅ Code block 2 đã xuất hiện sau ${elapsed2}s chờ thêm! Chờ ổn định toàn bộ...`, "success");
              try {
                await waitForResponse(60, responseBaseline);
                finalBlocks = getLatestCodeBlocks(responseContainer);
              } catch (e) {}
              found2 = true;
              break;
            }
            addSidebarLog(`⏳ [${elapsed2}/450s] Đang chờ tiếp code block 2...`, "warning");
          }
          
          if (found2) {
            found = true;
            break;
          } else {
            // Không tìm thấy block 2 sau 450s chờ thêm -> dừng và báo lỗi
            break;
          }
        }
        
        if (finalBlocks.length >= minRequiredBlocks) {
          addSidebarLog(`✅ Đã nhận đủ ${finalBlocks.length} code blocks sau ${elapsed}s. Chờ ổn định thêm 15s...`, "success");
          try {
            await waitForResponse(60, responseBaseline);
            finalBlocks = getLatestCodeBlocks(responseContainer);
          } catch (e) {
            // Bỏ qua lỗi chờ phụ
          }
          found = true;
          break;
        }
        addSidebarLog(`⏳ [${elapsed}/450s] Chưa đủ ${minRequiredBlocks} code blocks (hiện có: ${finalBlocks.length}), tiếp tục chờ...`, "warning");
      }
      
      if (!found) {
        addSidebarLog(`❌ Hết thời gian vẫn không đủ ${minRequiredBlocks} code blocks. Reload & gửi lại.`, "error");
        await sendResult({
          status: "failed_retry",
          error: `Hết thời gian chờ vẫn không xuất hiện đủ ${minRequiredBlocks} code blocks!`,
          text: responseText,
          blocks: []
        });
        
        setTimeout(async () => {
          if (data.chunk_index === 1) {
            const targetUrl = data.initial_url || "https://gemini.google.com/app";
            addSidebarLog(`🔄 Trở lại trang bắt đầu: ${targetUrl}...`, "warning");
            await forceSaveAndReload(targetUrl);
          } else {
            addSidebarLog("🔄 Tải lại đúng trang đang chat...", "warning");
            await forceSaveAndReload();
          }
        }, 2000);
        return;
      }
    }

    // Những chunk sau thì chỉ lấy kết quả block đầu tiên
    if (data.chunk_index > 1 && finalBlocks.length > 0) {
      addSidebarLog("👉 Chunk sau 1: Chỉ lấy kết quả block đầu tiên.", "info");
      finalBlocks = [finalBlocks[0]];
    }

    const linesBlock1 = getCodeLinesCount(finalBlocks, 0);
    addSidebarLog(`Số code blocks nhận được: ${finalBlocks.length}. Block 1: ${linesBlock1} dòng`, "info");

    // 5. Kiểm tra dòng (nếu cần)
    if (data.expected_lines !== null) {
      // Chunk 1: kiểm tra CẢ 2 block, mỗi block phải đủ expected_lines
      if (data.chunk_index === 1) {
        const linesBlock2 = getCodeLinesCount(finalBlocks, 1);
        addSidebarLog(`[Chunk 1] Kiểm tra 2 block: Block1=${linesBlock1} dòng, Block2=${linesBlock2} dòng. Yêu cầu mỗi block: ${data.expected_lines} dòng`, "info");
        
        let failMsg = "";
        if (linesBlock1 !== data.expected_lines) {
          failMsg += `Block 1 sai: ${linesBlock1}/${data.expected_lines} dòng. `;
        }
        if (linesBlock2 !== data.expected_lines) {
          failMsg += `Block 2 sai: ${linesBlock2}/${data.expected_lines} dòng. `;
        }
        
        if (failMsg) {
          currentActionText = `Lỗi số dòng! Báo cáo Python và tự động tải lại trang...`;
          addSidebarLog(`LỖI: ${failMsg}`, "error");
          await sendResult({
            status: "failed_retry",
            error: failMsg.trim(),
            text: responseText,
            blocks: finalBlocks
          });
          
          addSidebarLog("Đang F5 reload trang để làm sạch UI...", "warning");
          setTimeout(async () => {
            const targetUrl = data.initial_url || "https://gemini.google.com/app";
            addSidebarLog(`🔄 Trở lại trang bắt đầu: ${targetUrl}...`, "warning");
            await forceSaveAndReload(targetUrl);
          }, 2000);
          return;
        }
        addSidebarLog(`✅ [Chunk 1] Cả 2 block đều đủ ${data.expected_lines} dòng!`, "success");
      } else {
        // Chunk 2 trở đi: chỉ kiểm tra block đầu tiên
        currentActionText = `Xác thực số dòng code block...`;
        addSidebarLog(`Yêu cầu số dòng: ${data.expected_lines}. Thực tế: ${linesBlock1}`, "info");
        if (linesBlock1 !== data.expected_lines) {
          currentActionText = `Lỗi số dòng! Báo cáo Python và tự động tải lại trang...`;
          addSidebarLog(`LỖI: Sai lệch dòng! Yêu cầu: ${data.expected_lines}, Thực tế: ${linesBlock1}`, "error");
          await sendResult({
            status: "failed_retry",
            error: `Số dòng không khớp! Yêu cầu: ${data.expected_lines}, Thực tế: ${linesBlock1}`,
            text: responseText,
            blocks: finalBlocks
          });
          
          addSidebarLog("Đang F5 reload trang để làm sạch UI...", "warning");
          setTimeout(async () => {
            addSidebarLog("🔄 Tải lại đúng trang đang chat...", "warning");
            await forceSaveAndReload();
          }, 2000);
          return;
        }
        addSidebarLog(`✅ Số dòng khớp: ${linesBlock1}/${data.expected_lines}`, "success");
      }
    }

    // Gửi kết quả thành công về cho Python
    currentActionText = `Thành công! Báo cáo Python và tự động tải lại trang...`;
    addSidebarLog("GỬI CHUNK THÀNH CÔNG! Đang truyền kết quả về Python...", "success");
    await sendResult({
      status: "success",
      text: responseText,
      blocks: finalBlocks
    });

    // Yêu cầu: Sau mỗi chunk thành công chọn vào thanh địa chỉ rồi tải lại trang
    addSidebarLog("Đang ép lưu nháp và tải lại trang (F5) để dọn dẹp bộ nhớ...", "info");
    await forceSaveAndReload();
  }

  async function sendResult(payload) {
    const jobForResult = _currentJobId || activeJob || '';
    try {
      await fetchBridge(`http://localhost:${serverPort}/result?job=${jobForResult}`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(payload)
      });
      currentActionText = "Đã lưu kết quả thành công!";
      addSidebarLog(`✅ Đã gửi kết quả về Python (luồng: ${jobForResult.toUpperCase()})`, "success");
    } catch (e) {
      addSidebarLog("Lỗi truyền dữ liệu kết quả về Python: " + e.message, "error");
      console.error("Lỗi gửi kết quả về Python:", e);
    }
  }

  async function forceSaveAndReload(url = null) {
    addSidebarLog("Thực hiện tương tác giả để ép Google lưu dữ liệu trước khi thoát...", "info");
    try {
      const safeX = 100;
      const safeY = 100;
      
      await _sendDebugScroll(safeX, safeY, 0, 100);
      await sleep(200);
      await _sendDebugScroll(safeX, safeY, 0, -100);
      await sleep(1500);
    } catch (e) {
      console.warn("Lỗi khi forceSaveAndReload", e);
    }
    
    if (url) {
      window.location.href = url;
    } else {
      window.location.reload();
    }
  }

  async function forceSaveOnly() {
    try {
      const safeX = 100;
      const safeY = 100;
      
      await _sendDebugScroll(safeX, safeY, 0, 100);
      await sleep(200);
      await _sendDebugScroll(safeX, safeY, 0, -100);
      await sleep(1500);
    } catch (e) {
      console.warn("Lỗi khi forceSaveOnly", e);
    }
  }

  // Khởi động vòng lặp
  pollLoop();
})();
