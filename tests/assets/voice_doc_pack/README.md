# HolonCode voice document test pack

Four small files for testing the Voice + WhatsApp document bridge: while you are
on a call with the HolonCode agent, you send one of these on WhatsApp, and the
agent should read back values that only exist inside the file.

Regenerate the files at any time with:

```powershell
.\.venv\Scripts\python.exe scripts\make_voice_doc_test_pack.py
```

## Before you start

- The Meta, agent, and voice workers must be running (restart them after any
  code change, otherwise the old extractor stays loaded).
- `GOOGLE_API_KEY` must be set in `.env`. The image case depends on it, because
  no local OCR engine is installed on this machine.
- Confirm extraction still works offline before burning a live call:

```powershell
.\.venv\Scripts\python.exe scripts\verify_doc_pack_extraction.py
```

Expected output: all four rows `ok`, PDF under a second, XLSX near instant,
DOCX and PNG in the single-digit seconds.

## How each test runs

Call the HolonCode WhatsApp number from the phone that is registered as the
business owner, then for each case:

1. Say the trigger line. The agent must call `request_whatsapp_input`, which
   sends you a WhatsApp prompt. If the agent only tells you to send something
   and no WhatsApp prompt arrives, the tool did not fire; that is a failure.
2. Send the file on WhatsApp, as a document (not compressed), except the PNG
   which goes as a photo.
3. Ask the follow-up question and check the answer against the expected value.

You can also send the file first and say the trigger line a few seconds later.
The bridge buffers media that arrives during an active call, so the agent should
still pick it up. Either order is a valid test.

### 1. PDF - `HolonCode_Services.pdf`

- Say: "I'm sending our services and pricing PDF on WhatsApp now."
- Ask: "What support email and price list code are in that PDF?"
- Expect: `support@holoncode.dev` and `HC-PRICE-2026-04`.
- Follow-up: "What does the WhatsApp AI Agent Setup cost?" - `INR 78,500`.

### 2. DOCX - `HolonCode_Onboarding_Note.docx`

- Say: "I'm sending the onboarding note on WhatsApp, please read it."
- Ask: "What are the Saturday hours in that document?"
- Expect: `10:00 AM to 2:00 PM IST`.
- Follow-up: "Who is the escalation contact?" - `priya.nair@holoncode.dev`.

### 3. XLSX - `HolonCode_Price_List.xlsx`

- Say: "I'm sending the price list spreadsheet on WhatsApp."
- Ask: "What is the price and lead time for SKU HC-104?"
- Expect: `INR 26,500` and `10 days` (CRM Integration).
- This is the only asset that also populates the `tables` field, so it checks
  that structured rows survive extraction.

### 4. Image - `HolonCode_Price_Card.png`

- Say: "I'm sending a photo of our price card on WhatsApp."
- Ask: "What is the card code and until when is the offer valid?"
- Expect: `HC-CARD-77` and `30 September 2026`.

## What the values are, for checking answers

| File | Distinctive values |
| --- | --- |
| `HolonCode_Services.pdf` | support@holoncode.dev, +91 80 4718 2290, HC-PRICE-2026-04, INR 78,500, 18 percent GST, 30 day validity |
| `HolonCode_Onboarding_Note.docx` | HC-ONBOARD-118, Sat 10:00 AM to 2:00 PM IST, 7 day refund window, priya.nair@holoncode.dev |
| `HolonCode_Price_List.xlsx` | HC-101 to HC-106, HC-104 CRM Integration INR 26,500 / 10 days |
| `HolonCode_Price_Card.png` | HC-CARD-77, offer valid till 30 September 2026 |

## Known limits

- **1500 character clip.** `format_whatsapp_input_result` in `voice/tools.py`
  passes only the first 1500 extracted characters to the agent. Every file here
  is well under that, but a real customer document longer than roughly two pages
  will be silently truncated.
- **Docling warm-up.** The first DOCX or XLSX processed after a worker restart
  can take about 12 extra seconds while Docling loads its layout model. Later
  documents in the same worker are fast. PDFs skip this entirely because they
  use the native text extractor first.
- **No local OCR.** Docling reports "No OCR engine found" on this machine, so
  images and scanned PDFs go to Gemini. Without `GOOGLE_API_KEY` those cases
  return no text and the agent will say the file could not be read.
- **30 second cap.** `PROCESSING_TIMEOUT_SECONDS` in
  `shared/document_processor.py` aborts anything slower, and the caller hears
  silence while extraction runs, so keep test files small.
- Voice notes are supported too (Deepgram transcription), but there is no audio
  asset in this pack.

## If something fails

Check the worker log for the bridge and extraction lines:

```powershell
Select-String -Path logs\agent-worker.log -Pattern "CROSS_CHANNEL|Docling|Fallback extract" | Select-Object -Last 30
```

- No `CROSS_CHANNEL` line at all: the message did not reach the bridge, so it
  was treated as normal chat. Check that the call was still active.
- `CROSS_CHANNEL buffered`: the file arrived before the tool fired and was held
  for the agent. That is the expected path when you send the file first.
- `Fallback extract failed`: extraction itself broke. Re-run
  `scripts/verify_doc_pack_extraction.py` to reproduce it offline.
