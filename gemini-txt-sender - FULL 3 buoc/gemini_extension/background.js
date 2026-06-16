// background.js - Service Worker: bypass CORS + Chrome Debugger Protocol cho hover/click thật
const debuggerAttached = {};
let previousTabId = null;

chrome.runtime.onMessage.addListener((request, sender, sendResponse) => {
  // ── Fetch Bridge: bypass CORS khi gọi localhost từ HTTPS ──
  if (request.cmd === "fetch_bridge") {
    const { url, options } = request;
    
    fetch(url, options)
      .then(response => {
        if (!response.ok) {
          throw new Error(`HTTP error! status: ${response.status}`);
        }
        return response.text();
      })
      .then(text => {
        try {
          const json = JSON.parse(text);
          sendResponse({ success: true, data: json });
        } catch (e) {
          sendResponse({ success: true, data: text });
        }
      })
      .catch(error => {
        sendResponse({ success: false, error: error.message });
      });
      
    return true; // Giữ cổng kết nối async hoạt động
  }

  // ── Debug Mouse Move (Hover thật qua CDP) ──
  if (request.cmd === "debug_mouse_move") {
    const tabId = sender.tab.id;
    handleDebugMouseMove(tabId, request.x, request.y)
      .then(() => sendResponse({ success: true }))
      .catch(err => sendResponse({ success: false, error: err.message }));
    return true;
  }

  // ── Debug Click (Click thật qua CDP) ──
  if (request.cmd === "debug_click") {
    const tabId = sender.tab.id;
    handleDebugClick(tabId, request.x, request.y)
      .then(() => sendResponse({ success: true }))
      .catch(err => sendResponse({ success: false, error: err.message }));
    return true;
  }

  // ── Debug Scroll (Cuộn trang thật qua CDP) ──
  if (request.cmd === "debug_scroll") {
    const tabId = sender.tab.id;
    handleDebugScroll(tabId, request.x, request.y, request.deltaX, request.deltaY)
      .then(() => sendResponse({ success: true }))
      .catch(err => sendResponse({ success: false, error: err.message }));
    return true;
  }

  // ── Debug Detach (Tháo debugger) ──
  if (request.cmd === "debug_detach") {
    const tabId = sender.tab.id;
    if (debuggerAttached[tabId]) {
      chrome.debugger.detach({ tabId }).catch(() => {});
      delete debuggerAttached[tabId];
    }
    sendResponse({ success: true });
    return true;
  }

  // ── Auto Focus Tab (kéo tab Gemini lên để paste clipboard) ──
  if (request.cmd === "focus_tab") {
    const tabId = sender.tab.id;
    chrome.tabs.query({ active: true, currentWindow: true }, tabs => {
      if (tabs.length > 0 && tabs[0].id !== tabId) {
        previousTabId = tabs[0].id;
        chrome.tabs.update(tabId, { active: true });
        chrome.windows.update(sender.tab.windowId, { focused: true });
      }
      sendResponse({ success: true });
    });
    return true;
  }

  // ── Restore Tab (trả về tab cũ sau khi paste xong) ──
  if (request.cmd === "restore_tab") {
    if (previousTabId !== null) {
      chrome.tabs.update(previousTabId, { active: true });
      previousTabId = null;
    }
    sendResponse({ success: true });
    return true;
  }
});

// Đảm bảo debugger đã gắn vào tab
async function ensureDebugger(tabId) {
  if (debuggerAttached[tabId]) return;
  try {
    await chrome.debugger.attach({ tabId }, "1.3");
    debuggerAttached[tabId] = true;
    console.log(`[Z115] Debugger attached to tab ${tabId}`);
  } catch (e) {
    // Nếu đã attached rồi thì bỏ qua
    if (e.message && e.message.includes("Already attached")) {
      debuggerAttached[tabId] = true;
    } else {
      throw e;
    }
  }
}

// Di chuột thật đến tọa độ (x, y) - tạo event isTrusted=true
async function handleDebugMouseMove(tabId, x, y) {
  await ensureDebugger(tabId);
  await chrome.debugger.sendCommand({ tabId }, "Input.dispatchMouseEvent", {
    type: "mouseMoved",
    x: Math.round(x),
    y: Math.round(y)
  });
}

// Click thật tại tọa độ (x, y) - tạo event isTrusted=true
async function handleDebugClick(tabId, x, y) {
  await ensureDebugger(tabId);
  const roundX = Math.round(x);
  const roundY = Math.round(y);
  
  // Di chuột đến vị trí trước
  await chrome.debugger.sendCommand({ tabId }, "Input.dispatchMouseEvent", {
    type: "mouseMoved",
    x: roundX,
    y: roundY
  });
  
  // Nhấn chuột
  await chrome.debugger.sendCommand({ tabId }, "Input.dispatchMouseEvent", {
    type: "mousePressed",
    x: roundX,
    y: roundY,
    button: "left",
    clickCount: 1
  });
  // Delay nhỏ mô phỏng nhấn phím
  await new Promise(r => setTimeout(r, 50));
  
  // Nhả chuột
  await chrome.debugger.sendCommand({ tabId }, "Input.dispatchMouseEvent", {
    type: "mouseReleased",
    x: roundX,
    y: roundY,
    button: "left",
    clickCount: 1
  });
}

// Lăn chuột thật tại tọa độ (x, y) - tạo event isTrusted=true
async function handleDebugScroll(tabId, x, y, deltaX, deltaY) {
  await ensureDebugger(tabId);
  await chrome.debugger.sendCommand({ tabId }, "Input.dispatchMouseEvent", {
    type: "mouseWheel",
    x: Math.round(x),
    y: Math.round(y),
    deltaX: Math.round(deltaX),
    deltaY: Math.round(deltaY)
  });
}

// ── Background Polling Alarm ──
chrome.alarms.create("z115_keepalive", { periodInMinutes: 0.5 });
chrome.alarms.onAlarm.addListener(alarm => {
  if (alarm.name === "z115_keepalive") {
    console.log("[Z115] Alarm keep-alive triggered to prevent service worker sleep");
  }
});

// Tự động dọn dẹp khi debugger bị detach (do người dùng hoặc tab đóng)
chrome.debugger.onDetach.addListener((source) => {
  delete debuggerAttached[source.tabId];
  console.log(`[Z115] Debugger detached from tab ${source.tabId}`);
});
