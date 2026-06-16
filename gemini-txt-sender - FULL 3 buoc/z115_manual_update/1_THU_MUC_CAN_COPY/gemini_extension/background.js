// background.js - Service Worker giúp bypass CORS và Mixed Content khi fetch localhost từ HTTPS
chrome.runtime.onMessage.addListener((request, sender, sendResponse) => {
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
});
