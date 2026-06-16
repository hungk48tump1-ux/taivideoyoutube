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
        "message-content",
        ".model-response-text"
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

if (typeof window !== 'undefined') {
    window.GeminiSelectors = GeminiSelectors;
}
