# Manager thường trú, giao tiếp bằng SendMessage

**Ngày:** 2026-09-10 · **Trạng thái:** CTO đã duyệt hình dạng, đang triển khai

## Vì sao

Hôm nay t5061 hỏi manager một câu ("có rebuild container không?") rồi ngồi im một
tiếng, vì **không có đường nào để hỏi**. Đó không phải lỗi của worker. Đo được ba chỗ hỏng:

1. **`deliver_answer` dùng `claude --resume <id> -p -- <msg>`.** Cái đó sinh một tiến trình
   headless mới chạy một lượt trên cùng transcript rồi thoát — nó **không** đi vào phiên tmux
   đang sống. Toàn bộ đường giao câu trả lời được viết cho worker `claude --bg`, và chết ngay
   khi worker chuyển sang phiên tương tác.
2. **`manager_daemon` không chạy**, không có systemd unit. README của repo đã ghi đúng hậu quả:
   *"nothing calls the code that would settle it or deliver an answer back, so the worker that
   filed it blocks indefinitely."*
3. **Worker không được dạy escalate.** `BRIEF-DOD.md` không có chữ "escalate" nào. Hàng đợi
   `escalations.jsonl` có 38 bản ghi, **0 đang mở** — không phải vì đã giải quyết hết, mà vì
   không ai còn dùng đường đó.

## Dữ kiện then chốt (đo ngày 2026-09-10, không phải suy đoán)

- **Phiên CLI CÓ `SendMessage` và `ListAgents`.** Đã thử round-trip thật: t8471 → t8419 tới nơi,
  t8419 ack ngược lại. Phiên desktop (CCD) **không** có `SendMessage`.
- **`mcp__ccd_session_mgmt__send_message` chỉ với tới phiên app**, trả `session not found` cho
  phiên tmux. `list_sessions` cũng không liệt kê phiên CLI.
- **`SendMessage` khớp tên chính xác kể cả emoji.** Gửi tới `T8471` bị từ chối; tên thật là
  `T8471 🔹`. cmew viết hoa chữ đầu và thêm ` 🔹`; effort ultracode thêm `🔥 ` phía trước.
- **tmux cấp cho session mới môi trường của CLIENT**, nên `env -u` ở shell gọi lệnh và
  `tmux set-environment -g -u` đều không gỡ được biến kế thừa. Phải bọc `env -u` vào chính lệnh
  tmux chạy.

## Quyết định của CTO

| Câu hỏi | Chốt |
|---|---|
| Ai nhận escalation đầu tiên | Một phiên Claude **thường trú** |
| Manager có xem/gõ được không | Có — phiên tmux `cc-manager`, `cmew a manager` |
| Quyền của manager | **Toàn quyền** theo `skills/engineering-manager/SKILL.md` |
| Model manager | **Opus, effort max** |
| Đánh thức CTO | Bảng điều phối + nhắn vào phiên desktop |
| Đường truyền | **`SendMessage` cho mọi giao tiếp; `cmew` chỉ tạo phiên** |

## Kiến trúc

```
CTO (desktop)  ──send-keys──▶  cc-manager  ──SendMessage──▶  worker
      ▲                            │  ▲                         │
      │                            │  └────SendMessage──────────┘
      │                    ghi escalations.jsonl
      └──── bảng điều phối ◀── bơm đọc sổ
```

**Manager** — phiên tmux `cc-manager`, Opus max, thường trú. Cầm SKILL.md. Dựng worker bằng
`parallel-task.sh` (provisioning), rồi **brief bằng `SendMessage`**. Nhận tin worker, quyết,
nhắn lại. Quyết không nổi thì ghi sổ + đẩy lên bảng + nhắn CTO.

**Worker** — nhận brief qua `SendMessage`. Kẹt thì `SendMessage` thẳng cho manager. Xong thì
báo cáo cho manager.

**`escalations.jsonl`** — đổi vai từ đường truyền thành **sổ ghi**: để escalation sống sót khi
manager chết, và để bảng có cái hiện.

**Daemon** — còn đúng hai việc: nhịp định kỳ để manager rà worker đứng im, và đưa câu trả lời
CTO bấm trên bảng vào manager.

`send-keys` chỉ còn ở **một chỗ**: vào pane `cc-manager` (từ phiên desktop, và từ daemon).

## Phạm vi triển khai

### A. `parallel-task.sh` — chỉ còn provisioning
- Bỏ ghi `BRIEF.md` và bỏ `send-keys` giao brief. Shell **không giao việc được** — `SendMessage`
  là công cụ của Claude, shell không gọi được.
- `dispatch` đổi tên nghĩa: dựng worktree + phiên + đăng ký, rồi **in ra tên agent chính xác**
  (kèm emoji) để manager dùng làm địa chỉ `SendMessage`.
- Giữ: chờ TUI sẵn sàng, trả lời hộp thoại tin cậy, dọn biến môi trường kế thừa, mặc định
  `opus`/`max`.

### B. Khởi động manager
- `parallel-task.sh manager-start`: dựng `cc-manager` bằng cmew (Opus, max), nạp
  `skills/engineering-manager/SKILL.md` làm tin đầu tiên, ghi tên agent chính xác vào registry.
- systemd user service giữ nó sống lại nếu chết.

### C. `manager.py` / `manager_daemon.py`
- **Xoá** `resume_argv()` và `deliver_answer()` — giao là việc của manager, không phải daemon.
- Daemon giữ: tick định kỳ, và một hàm mới `wake_manager(text)` dùng `send-keys` vào
  `cc-manager` (chờ pane idle, xác nhận chữ đã vào ô nhập trước khi Enter — cùng cách
  `parallel-task.sh` đã làm).
- systemd user timer.

### D. Worker biết escalate
- `BRIEF-DOD.md` thêm mục: kẹt thì `SendMessage` cho manager, kèm **tên agent chính xác của
  manager**, và ghi rõ khi nào escalate thay vì tự đoán.

### E. Registry mang tên hiển thị
- Mỗi entry lưu `agent_name` (tên chính xác kèm emoji) bên cạnh `session_id`, để manager không
  phải tự suy ra quy tắc viết hoa + emoji của cmew.

## Không làm (YAGNI)

- **Không** dựng message bus qua artifact db — đã đo: payload lớn qua prompt của bơm bị model
  soạn lại.
- **Không** dựng hộp thư file + Stop-hook — `SendMessage` đã chạy được, hook chỉ thêm chỗ hỏng im lặng.
- **Không** đọc transcript để lấy trả lời — worker nhắn ngược được rồi.

## Cách biết là xong

1. Worker kẹt gửi `SendMessage` cho manager, manager trả lời, worker chạy tiếp — **không có bàn
   tay người nào ở giữa**.
2. Manager dựng một worker mới và brief nó hoàn toàn bằng `SendMessage`.
3. Escalation manager không quyết nổi hiện trên bảng, CTO bấm trả lời, câu trả lời tới worker.
4. `grep -rn "resume_argv\|deliver_answer" bin/` không còn kết quả.
5. Toàn bộ test xanh.
