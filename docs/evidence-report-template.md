# Báo cáo xác minh — mẫu bắt buộc

Làm xong một vé thì nộp **một báo cáo**, không phải một đống tệp.

Trước đây một vé có chín attachment tên kiểu `d62-5_red_green_revert.txt`,
`verify-goal-verbfirst.png`. Từng tệp đều đúng, gộp lại thì không ai — TL, QC,
CTO — đọc ra được vé đòi gì và tệp nào chứng minh cái gì. Cột Bằng chứng trên
bảng điều phối giờ chỉ hiện **một mục duy nhất là báo cáo này**; vé có tệp mà
chưa có báo cáo thì bảng ghi "chưa có báo cáo", không tính là đã có bằng chứng.

## Quy tắc quan trọng nhất

**Mỗi tệp bằng chứng phải có một câu nói nó chứng minh điều gì.** Đó là thứ duy
nhất nối một tên tệp với một yêu cầu. Bộ sinh từ chối chạy nếu thiếu dù một câu.

Đọc hết báo cáo là hiểu. Không phải mở tệp nào.

## Cách làm

Viết `report.json` nằm cùng thư mục với các tệp bằng chứng, rồi chạy:

```bash
python3 ~/projects/claude-parallel-worktree-plugin/bin/evidence_report.py --manifest report.json --check
```

`--check` chỉ soát, không sinh gì. Sửa hết lỗi nó liệt kê rồi nộp:

```bash
python3 ~/projects/claude-parallel-worktree-plugin/bin/evidence_report.py --manifest report.json --apply
```

Lệnh đó sinh `report-AB<vé>.html` (người đọc) và `report-AB<vé>.json` (bảng đọc),
đính cả hai lên vé, và comment vào PR.

Chạy từ trong worktree của vé — `gh` phân giải số PR theo repo nó đang đứng.

## Schema

```jsonc
{
  "ticket": 6541,
  "type": "bug",              // bug | task | doc
  "title": "Title KP ra câu động từ, không phải cụm danh từ",
  "sprint": "Sprint 58",
  "pr": 732,
  "pr_url": "https://github.com/AIQuintaHQ/aiquinta-platform/pull/732",
  "commit": "c37c85b75",      // build-ref đang chạy — D62-4 đòi
  "run_by": "tanlocc",
  "run_at": "10/09 06:38 UTC",
  "env": "aiquinta-mfg-gateway-1",

  "requirement": {
    "source": "AC-B3.2 (US-5811 / FR-B3)",   // lấy từ đâu ra
    "text": "title phải là cụm danh từ cô đọng gọi tên chủ đề, không dùng động từ.",
    "evidence": [
      {"file": "loi-ban-dau.png", "proves": "QC báo 22/06: cả 2/2 pack publish đều ra title bắt đầu bằng “Giúp”."}
    ]
  },

  "changed": [
    "knowledge/application/title_generation.py — cắt động từ đầu câu, cắt tại ranh giới mệnh đề."
  ],

  "results": [
    {
      "req": "Title không bắt đầu bằng động từ",
      "verdict": "đạt",                       // đạt | đạt một phần | chưa chứng minh | không đạt
      "evidence": [
        {"file": "d62-5_red_green_revert.txt", "proves": "Gỡ fix ra thì 2 test đó đỏ, khôi phục thì xanh."}
      ],
      "note": "tuỳ chọn — thêm ngữ cảnh cho dòng này"
    }
  ],

  "red_green": {
    "how": "Revert riêng tệp source, giữ nguyên test — chứng minh test bắt được regression.",
    "red":   "…output pytest lúc ĐỎ…",
    "green": "…output pytest lúc XANH…"
  },

  "blockers": [
    "Nhánh dự phòng chưa có lần chạy thật nào chạm tới. MiniMax-M3 không dựng lại được tình huống."
  ],

  "checklist": {
    "D62-1": "CI xanh, chưa ai duyệt",   // true nếu xong, hoặc một câu lý do
    "D62-2": true,
    "D62-3": true,
    "D62-4": true,
    "D62-5": true,
    "D71-5": true
  }
}
```

Vé làm tài liệu (`"type": "doc"`) dùng cùng vỏ: mỗi `results` row là một mục của
tài liệu, `evidence` trỏ tới artefact. Không cần `red_green`.

## Chốt chặn — bộ sinh từ chối nếu

| Lỗi | Vì sao chặn |
|---|---|
| Một yêu cầu ghi "đạt" mà không bằng chứng nào | Lời khai, không phải bằng chứng |
| Bằng chứng không có câu `proves` | Đúng cái tình trạng chín-tệp-rời-rạc cần chấm dứt |
| `proves` chứa thẻ HTML | Bảng dựng bằng `h()`, không có đường markup — thẻ sẽ hiện ra dạng chữ |
| Trỏ tệp không tồn tại | Báo cáo trông đầy đủ mà link chết |
| Vé code/bug thiếu `red_green` | D62-5 |
| Diff đụng `apps/web` hoặc `packages/ui` mà không ảnh nào | Có người phải nhìn màn hình |
| Checklist thiếu mục, hoặc chưa tick mà không lý do | Ô trống đọc như đã xong |

Nó liệt kê **hết** lỗi một lượt, không phải từng lỗi một.

## Prose là chữ thường

Không thẻ HTML. Nhấn mạnh thì dùng ngoặc kép “…”. Cùng một `report.json` được
render hai nơi — tệp HTML và bảng điều phối — và bảng không có đường markup nào.

## Không đính bằng chứng giả

Bằng chứng phải là output thật của lần chạy thật. Chép tay, dựng lại, hay ảnh
chụp từ lần khác đều không tính. Chỗ nào không chứng minh được thì ghi
`"chưa chứng minh"` và nói rõ vì sao — đó là câu trả lời hợp lệ, còn tô xanh một
ô không có gì đứng sau thì không.

## Kẹt thì nhắn, đừng đoán

Ngày 10/09 một worker hỏi một câu ("có rebuild container không?") rồi ngồi im
một tiếng. Không phải nó lười — lúc đó không có đường nào để hỏi. Giờ có:
`SendMessage` thẳng cho manager.

Nhắn ngay, đừng tự quyết, khi:

- Việc sắp làm **không lùi lại được**: xoá dữ liệu, push lên `main`, đụng vào môi trường thật.
- **Spec tự mâu thuẫn** — hai chỗ đòi hai đằng, chọn bên nào cũng là chọn hộ người viết spec.
- Quyết định là **của PO/BA**, không phải của người viết code: đổi phạm vi, đổi hành vi người
  dùng nhìn thấy, bỏ bớt một yêu cầu.
- **Thiếu quyền hoặc thiếu credential**: không có token, không vào được máy, một hộp thoại quyền
  không ai bấm hộ được.
- **Cùng một cách đã hỏng nhiều lần** — tới lần thứ ba vẫn cùng một lỗi thì cái sai nằm ở giả
  định, không nằm ở lần thử tiếp theo.

### Nhắn cho ai

`SendMessage`, địa chỉ là **tên hiển thị chính xác của manager**. cmew đổi tên phiên khi hiển
thị: manager hiện ra dạng `Manager 🔹`, và gửi tới `Manager` bị **từ chối** — không phải "gửi rồi
mà chưa ai đọc", mà là không gửi được.

Đừng đoán tên đó. Chạy `ListAgents`, đọc tên đúng từng ký tự (kể cả emoji), copy nguyên si.

### Một tin nhắn dùng được có bốn phần

1. **Kẹt ở đâu** — một câu: vé nào, đang đứng ở chỗ nào.
2. **Đã thử gì** — từng cách đã chạy và kết quả của nó, để manager không bảo làm lại đúng cái
   vừa hỏng.
3. **Có những phương án nào** — nhìn ra được thì nêu A/B kèm cái giá của mỗi bên; không nhìn ra
   thì nói thẳng là chưa thấy phương án nào.
4. **Đang có bằng chứng gì** — output, log, ảnh. Đường dẫn tệp là đủ.

### Hai điều không được làm

- **Không ngồi im chờ.** Không nhắn thì không ai biết bạn đang kẹt: nhìn từ bên ngoài, đang kẹt
  và đang làm việc giống hệt nhau.
- **Không bịa kết quả để đi tiếp.** Cùng một luật với "Không đính bằng chứng giả" ở trên: chỗ nào
  chưa chứng minh được thì ghi `"chưa chứng minh"` và nói rõ vì sao. **Một cái kẹt mô tả chính xác
  đáng giá hơn một kết quả dựng ra cho có** — nó nói cho người đọc biết phải quyết cái gì, còn ô
  tô xanh không có gì đứng sau thì chỉ giấu mất chỗ đó.

Nhắn xong thì làm tiếp phần không phụ thuộc câu trả lời. Chờ là chờ đúng cái đang vướng, không
phải dừng cả vé.
