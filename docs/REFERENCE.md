# Tracyy — tham khảo

Phần chi tiết tách khỏi [README](../README.md) để trang đầu còn ngắn.

---

## Biến môi trường

| Biến | Mặc định | Tác dụng |
|---|---|---|
| `TRACYY_AUDIO_LATENCY` | `0.05` | Latency yêu cầu PortAudio, giây (0.002–0.515). CoreAudio/ASIO có dây chạy tốt ở 0.010–0.015. Đây là **sàn**, không phải giá trị cuối: thiết bị khai báo latency tối thiểu cao hơn (Bluetooth) sẽ được nâng theo thiết bị |
| `TRACYY_UI_TARGET_LOAD` | `0.10` | Ngưỡng tải UI; vượt ngưỡng thì scheduler giãn mọi tầng nhịp |
| `TRACYY_RESAMPLE_QUALITY` | `HQ` | Chất lượng soxr: `QQ`/`LQ`/`MQ`/`HQ`/`VHQ` |
| `TRACYY_LINEAR_RESAMPLE` | — | `1` để ép nội suy tuyến tính thay soxr |
| `TRACYY_MEDIA_WORKERS` | tự tính | Số process phân tích waveform |
| `TRACYY_GPU_TIMELINE` | `1` | `0` để tắt surface Qt Quick, quay về QPainter |
| `TRACYY_DATA_ROOT` | — | Ghi đè thư mục project |
| `TRACYY_CACHE_ROOT` | — | Ghi đè thư mục cache |
| `TRACYY_LOG_LEVEL` | `DEBUG` từ source, `INFO` khi build | Mức log |
| `TRACYY_LICENSE_BUILD_KEY` | — | Khoá AES cho `license_config.enc`, 64 ký tự hex |
| `TRACYY_USB_TOKEN_KEY` | — | Khoá private X25519 đọc admin USB token, base64 của 32 byte |

Log nằm ở `<data root>/logs/tracyy.log`, xoay vòng 2 MB × 4 file.

Hai biến khoá cuối bảng có thể thay bằng file `license_secrets.json` đặt cạnh
`license_config.enc`; xem [`tracyy/licensing/build_secrets.py`](../tracyy/licensing/build_secrets.py).

---

## Tracyy Live — sync qua LAN

Nhiều máy vào cùng một phiên trên LAN để cùng chuẩn bị cue. **Không có đồng hồ
chung: mỗi máy vẫn tự phát nhạc của nó.**

Phần đã có: tạo/tham gia phiên có tên, danh sách người đang kết nối, phân quyền
từ máy chính, cập nhật tên/bài đang mở, con trỏ và ô đang sửa của người khác hiện
trên cue table, tự thử nối lại khi mất mạng, khoá sửa cue trên máy khách khi mất
liên lạc.

Chưa có: đồng bộ nội dung cue hai chiều đầy đủ, tải nhạc qua mạng, đề xuất
offline.

### Dựng phiên

1. Chạy bản mã mới trên **cả hai máy**. Sync dùng protocol phiên bản 2; bản đã
   đóng gói trước thay đổi này cần build lại.
2. Máy chính: vào **Sync**, đặt tên, chọn IP của Wi-Fi/Ethernet mà máy khách cùng
   mạng, chọn cổng (mặc định **8090**) rồi **Tạo phiên**.
3. Máy khách: nhập đúng `IP:cổng` và mã hiện trên máy chính. `127.0.0.1` chỉ để
   thử trên cùng máy; `0.0.0.0` không phải IP để nối vào.
4. Máy chính cấp **Biên tập**, **Chỉ sửa cue**, hoặc **Chỉ xem**. Người mới mặc
   định chỉ xem. Quyền giữ qua lúc mạng phục hồi trong cùng phiên và cùng lần mở
   app; phiên mới bắt đầu lại ở chỉ xem.

Nếu không nối được: kiểm tra IP đã chọn, cổng, hai máy cùng mạng, và quyền nhận
kết nối của Tracyy trong firewall máy chính. Mở `http://IP:CỔNG/` trên trình duyệt
máy khách phải thấy lời chào Tracyy Live. Cách dùng LAN này không cần mở cổng ra
Internet.

### Kiểm thử riêng phần Sync

```bash
python -m pytest tests/test_sync_ui.py tests/test_sync_reliability.py tests/test_sync_network.py tests/test_sync_session.py tests/test_sync_store.py -o addopts=
```

Thử hai tiến trình trên một máy **không** thay thế việc xác nhận qua mạng thật
giữa macOS và Windows.

---

## Chẩn đoán khi máy chạy nặng

`window.tick_scheduler.statistics()` trả về tải UI thực tế: trạng thái hiện tại,
phần trăm thời gian dành cho công việc định kỳ, hệ số giãn nhịp, và chi phí trung
bình của từng job. Đây là chỗ đầu tiên nên nhìn.

`transport.audio_health()` trả về số lần underflow theo từng thiết bị và số lần
buffer bị đói — nếu chúng đứng yên ở 0 suốt một show thì `TRACYY_AUDIO_LATENCY`
đang dùng là an toàn trên máy đó.

Lưu ý: hai bộ đếm này **không** bắt được trường hợp PortAudio rơi về block siêu
nhỏ khi latency yêu cầu thấp hơn mức thiết bị chịu được — đường CoreAudio đó
không set cờ `output_underflow`, callback chỉ đơn giản là không được gọi đủ nhịp.
Muốn kiểm tra thì đếm số callback mỗi giây: `block × callback/s` phải xấp xỉ
sample rate. Trên AirPods Pro, latency 0.05 cho block 15 frame và ~3200
callback/s, tức deadline 0,31 ms — không đường nào chạy nổi bằng Python.
`_effective_output_latency()` nâng sàn theo `default_high_output_latency` của
thiết bị để tránh chuyện này.

---

## Hạn chế đã biết

- **Hai test phụ thuộc thứ tự chạy.** `test_presence_overlay.py` có hai test
  "không được vẽ gì" fail khi chạy *sau* `test_app_boot.py`, pass khi chạy riêng.
  `test_app_boot` gọi `apply_theme(app)` lên `QApplication` — singleton dùng chung
  cả session — và stylesheet toàn app đó đổi cách render chữ trong cell. Một pixel
  chữ xám `rgb(190,194,183)` rơi vào đúng khoảng cách 45 so với màu hồng cần tìm,
  bằng ngưỡng `OUTLINE = 45` vốn so sánh `<=`. Lỗi cách ly test, không phải lỗi
  tính năng.
- **Tracyy Live chưa đồng bộ nội dung cue hai chiều đầy đủ** và chưa tải nhạc qua
  mạng.
- **Chỉ thử trên macOS và Windows.** Đường Linux có trong code nhưng chưa chạy
  thật trong show.
