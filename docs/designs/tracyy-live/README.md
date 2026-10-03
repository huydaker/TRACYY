# Ảnh thiết kế Tracyy Live

Đây là **preview thiết kế với dữ liệu giả lập**, không phải ảnh chứng minh đồng bộ đã chạy. Waveform, loading cue và bảng cue dùng widget Qt native của Tracyy; lớp hiện diện được vẽ minh họa. Giữ bố cục tổng thể/transport hiện tại khi tích hợp, không coi crop này là thay thế toàn bộ màn hình File.

| Ảnh | Ý nghĩa |
|---|---|
| [presence-option-b.png](presence-option-b.png) | Phương án B đã chọn: hàng hiện diện phía trên waveform |
| [file-cue-cursor.png](file-cue-cursor.png) | Linh trong bảng cue, Huy ở waveform |
| [file-cue-editing.png](file-cue-editing.png) | Chỉ rõ Huy đang sửa ô LABEL, giữ selection riêng |
| [file-cue-outside-view.png](file-cue-outside-view.png) | Cue người kia ngoài viewport: thông báo + Đi tới cue, không tự cuộn |

![Con trỏ trong bảng cue](file-cue-cursor.png)

![Sửa ô LABEL](file-cue-editing.png)

![Hai máy cuộn khác nhau](file-cue-outside-view.png)

## Dựng lại 3 ảnh bảng cue

Từ root dự án, sau khi cài dependencies:

```bash
python3 docs/designs/tracyy-live/file_cue_presence_preview.py
```

Script tự tìm root dự án theo vị trí của nó, dùng Qt offscreen và waveform software, ghi đè **ba PNG preview trong thư mục này**. Không khởi động host/client, không chỉnh project hay dữ liệu cue thật. Font giữa các hệ điều hành có thể khác. Ảnh `presence-option-b.png` là bản giữ lại từ lần duyệt trước, không do script này dựng lại.

Chi tiết hành vi và triển khai: [README bàn giao](../../README_TRACYY_LIVE_HANDOFF.md).
