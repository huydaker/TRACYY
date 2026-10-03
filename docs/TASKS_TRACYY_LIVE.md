# Tracyy Live — danh sách công việc bàn giao

Ngày: 09/09/2026. Đây là task list trong repository, **không tạo task nền hay chạy triển khai mới**.

Đọc [README bàn giao](README_TRACYY_LIVE_HANDOFF.md) để biết phạm vi, source và ảnh. Checklist chưa đánh dấu là chưa hoàn thành, kể cả khi đã có mockup.

**Thứ tự làm (người dùng chốt 09/09/2026): T1 ✓ → T4 → T2 → T3 → T5 → T6.**
Presence/con trỏ lên trước đồng bộ cue; giữ nguyên số hiệu T để không gãy tham chiếu
trong tài liệu bàn giao.

## Đã có

- [x] ID cue/bài dạng chuỗi và code migrate project v3 → v4.
- [x] Host/client LAN protocol 2; tạo/join phiên, roster, cấp role.
- [x] Worker reconnect, loại phản hồi cũ, proxy bypass, credentials và kiểm tra roster.
- [x] Mất liên lạc chuyển trạng thái đỏ và guard thao tác sửa cue.
- [x] UI phiên gần đây, mở thư mục dữ liệu và xóa cache theo phiên.
- [x] Chọn hướng B: presence trên waveform trong File; Sync quản lý kết nối.
- [x] Dựng 3 ảnh đề xuất cursor/bảng cue bằng widget native, đưa vào docs.
- [ ] Người dùng duyệt chi tiết ba trạng thái bảng cue cuối cùng.

## T1 — Chốt model tài liệu và wire contract (ưu tiên đầu)

- [x] Audit ID ở tất cả điểm vào: waveform, table, import, undo, playlist. Kết quả:
      mọi đường sửa cue trong `controllers/cue.py` đã dùng ID chuỗi và guard quyền;
      `import_cue_csv` thiếu guard — đã vá (guard chạy trước mọi dialog). Riêng
      scheme gán lại ID int tuần tự trong CSV import **chưa** sửa, chủ đích để lại
      cho lúc CSV import tham gia sync.
- [x] Document/session ID (host mint mỗi lần tạo phiên, trả trong hello/join/heartbeat),
      revision đơn điệu. Canonical snapshot/hash **chưa làm** — thuộc phase B
      (`document.json` vẫn để dành cho snapshot compact). Ánh xạ track → local media
      dùng cơ chế sẵn có `track_id_for_path`/`adopt_track_id`.
- [x] Operation schema + validation (`tracyy/sync/document.py`: 4 kind cue), author do
      server suy từ credentials đã xác thực — envelope không có trường actor, server
      check `role_allows` cho từng op (lần đầu bảng quyền được kiểm phía server).
- [x] Quy tắc stale theo từng cue (`base_revision` cũ chỉ chặn op đụng cue người khác
      đã sửa sau đó), commit nguyên tử (fsync journal xong mới đổi state/ACK), journal
      NDJSON append-only tại `ShowStore.journal_path`, dedup theo `op_id` sống qua
      restart nhờ replay journal (cap 2000 entry, giới hạn có chủ đích, có test chốt).
- [x] Test model không Qt (`tests/test_sync_document.py`, 23 test) + tích hợp loopback
      (`tests/test_sync_submit_op.py`, 9 test). Wire tương thích: endpoint
      `/live/submit_op` và các trường `session_id`/`document_id`/`acked_revision`/`ops`
      đều additive, không bump protocol version; record journal mang tag `schema` để
      replay bỏ qua bản lạ.

Đạt khi: duplicate `op_id` không sinh cue thứ hai; request trái quyền/stale bị từ chối
có lý do; lỗi ghi journal không tạo ACK thành công. **Cả ba đã có test chứng minh**
(dedup kể cả khi base_revision đã tụt và sau host restart; reason
`forbidden`/`stale_revision`/`wrong_session`/`invalid`/`journal`; journal hỏng →
không ACK, không đổi revision/state).

Lưu ý phạm vi: đây là pipeline phía server + client `submit_operation`. UI chưa nối —
`cue.py` chưa đổi hành vi; client nhận `ops` qua heartbeat nhưng **chưa apply**
(`acked_revision` tạm tiến theo reply, khi T2 apply thật phải chuyển sang sau-apply).
Seam cho T2/undo: enum `MutationOrigin` trong `document.py`.

## T4 — Presence phương án B + bảng cue (làm trước T2 theo thứ tự đã chốt)

- [ ] Chuyển presence `track_path` sang `track_id`.
- [ ] Kênh presence nhanh có sequence/TTL/coalescing; độc lập journal cue.
- [ ] Hàng người tham gia trên waveform; playhead, cursor, selected cue.
- [ ] Table overlay neo cue ID + column; viền dòng/ô, tên nhỏ.
- [ ] Scroll/sort/zoom/DPI khác nhau vẫn đúng; khác bài hoặc ngoài viewport chỉ báo trạng thái.
- [ ] Cursor rời vùng/disconnect biến mất; không chặn input hay đổi local selection.
- [ ] Animation cue commit ngắn, giảm chuyển động, không animate toàn snapshot.
      (Riêng mục này cần cue commit từ T2 — dời sang T2 nếu T4 xong trước.)

Đạt khi: 2 máy xem/sửa độc lập, thấy đúng người và vị trí logic; không có cursor nhầm dòng, auto-scroll, seek hộ hay rebuild bảng mỗi tick.

## T2 — Đồng bộ cue end-to-end (sau T1, sau T4)

- [ ] Kênh gửi operation và nhận commit, snapshot tài liệu, catch-up theo revision.
- [ ] Host và client cùng qua một service commit; add/update/delete/move cue.
- [ ] Pending/ACK ở UI; không thay transport hay bài đang mở của máy khác.
- [ ] Retry mất ACK; reject late response của phiên cũ; resync gap/hash sai.
- [ ] Chỉ mở khóa sửa sau khi cả kết nối, quyền và tài liệu đã sẵn sàng.
- [ ] Audit import hàng loạt, cue types, undo/redo, modal mở lúc quyền bị thu hồi.

Đạt khi: host + hai client thấy cùng ID/nội dung/revision sau thêm/sửa/xóa; client B sửa bài khác không bị chuyển bài; reconnect không mất hay nhân đôi cue.

## T3 — Đồng bộ nhạc (sau T1, tích hợp cùng T2)

- [ ] Manifest/hash/version, tải `.part`, resume, verify và rename nguyên tử.
- [ ] Ánh xạ đường dẫn riêng Mac/Win; progress và trạng thái thiếu nhạc.
- [ ] Permission cho đề xuất thêm/thay bài; staging và chủ phiên phê duyệt.
- [ ] Tách cache tải lại được khỏi upload/nháp chưa duyệt; kiểm tra show ID ổn định.
- [ ] Xóa dữ liệu theo đúng phiên, không xóa file gốc host hoặc nháp người dùng.

Đạt khi: máy khách mới chưa có nhạc tải được đúng hash, nghe độc lập; đứt mạng tải tiếp không dùng file hỏng; bản thay chưa duyệt không đổi bản chuẩn.

## T5 — Offline proposal (sau T1, gửi qua T2)

- [ ] Tạo nháp độc lập từ base revision khi bản chung bị khóa.
- [ ] Lưu bền vững không chung cache có thể xóa; đính kèm media staging nếu cần.
- [ ] Gửi proposal sau reconnect, chủ phiên xem diff/conflict và duyệt/từ chối.
- [ ] Merge thành operations chuẩn, chống gửi trùng, giữ nháp tới khi có xác nhận.

Đạt khi: không có tự merge; sửa/sửa, sửa/xóa và thay media đều có kết quả rõ; dọn cache không làm mất đề xuất chưa gửi.

## T6 — Nghiệm thu hai máy và đóng gói

- [ ] macOS host → Windows client và chiều ngược lại, dùng bản build mới ở cả hai đầu.
- [ ] Firewall/IP thật, reconnect, host restart, thu hồi quyền, độ trễ và dữ liệu lớn.
- [ ] Đo latency thao tác/cursor, tải CPU/UI và audio underflow khi download.
- [ ] Test regression và ảnh UI thật; cập nhật README capability đã hoàn thành.

## Bản ghi kiểm tra khi bàn giao

- Source đối chiếu: protocol 2, project serialization v4, role model và các file Sync hiện có.
- Ảnh: dựng lại bằng `python3 docs/designs/tracyy-live/file_cue_presence_preview.py`, thành công; dữ liệu minh họa, không mở kết nối mạng.
- Kết quả pytest lượt bàn giao: **121 passed in 44.15s**, exit code 0, Python 3.11.8 / pytest 8.4.2 trên macOS. Gồm `test_project_ids.py`, `test_sync_ui.py`, `test_sync_reliability.py`, `test_sync_network.py`, `test_sync_session.py`, `test_sync_store.py`, `test_import_boundaries.py`; chạy với `-o addopts= --tb=short`.
- Đã kiểm tra các link tài liệu/ảnh nội bộ trong bộ bàn giao: không có đường dẫn thiếu.
- Chưa thử hai máy Mac/Windows vật lý; chưa test được các tính năng chưa triển khai ở T1–T5.

## Bản ghi kiểm tra lượt T1 (09/09/2026)

- Nhóm sync + IDs + hai file test mới: **155 passed in 68.19s**, exit code 0. Toàn bộ
  suite: **265 passed in 80.18s**. `python3 -m ruff check tracyy tools tests` sạch
  (ruff 0.16.6; có sửa 2 lỗi lint mới do ruff bản mới báo trên code cũ).
- File mới: `tracyy/sync/document.py`, `tests/test_sync_document.py`,
  `tests/test_sync_submit_op.py`. File sửa: `sync/server.py` (mint session/document id,
  `_submit_op`, heartbeat mang `ops`), `sync/client.py` (persist base/token,
  `submit_operation`, gửi `acked_revision`), `sync/protocol.py` (`PATH_SUBMIT_OP`),
  `sync/store.py` (`journal_path`), `controllers/playlist.py` (guard import CSV).
- Toàn bộ là test tự động trên một máy (loopback); chưa có gì ở T1 cần thử hai máy
  vật lý — việc đó bắt đầu có ý nghĩa từ T2 khi UI apply op.
