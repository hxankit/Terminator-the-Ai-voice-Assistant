"""Text extraction for supported document types."""
import io, json
from pathlib import Path

SUPPORTED = {".pdf", ".docx", ".txt", ".md", ".markdown", ".csv", ".xlsx", ".xls", ".json"}


class ExtractError(Exception):
    pass


def extract(path: Path) -> list[dict]:
    """Return a list of {text, location} sections (location = page/sheet for citations)."""
    ext = path.suffix.lower()
    if ext not in SUPPORTED:
        raise ExtractError(f"Unsupported file type {ext}")
    if ext == ".pdf":
        from pypdf import PdfReader
        reader = PdfReader(str(path))
        secs = [{"text": (p.extract_text() or ""), "location": f"page {i + 1}"} for i, p in enumerate(reader.pages)]
        if not any(s["text"].strip() for s in secs):
            raise ExtractError("PDF has no extractable text (scanned image?). OCR is not configured.")
        return secs
    if ext == ".docx":
        import docx
        d = docx.Document(str(path))
        parts = [p.text for p in d.paragraphs]
        for t in d.tables:
            for row in t.rows:
                parts.append(" | ".join(c.text for c in row.cells))
        return [{"text": "\n".join(parts), "location": "document"}]
    if ext in (".csv", ".xlsx", ".xls"):
        import pandas as pd
        frames = {"csv": pd.read_csv(path)} if ext == ".csv" else pd.read_excel(path, sheet_name=None)
        secs = []
        for name, df in frames.items():
            # serialize rows in blocks so each chunk keeps the header
            for start in range(0, len(df), 40):
                secs.append({"text": df.iloc[start:start + 40].to_csv(index=False),
                             "location": f"{name} rows {start + 1}-{min(start + 40, len(df))}"})
        return secs
    raw = path.read_text(errors="replace")
    if ext == ".json":
        try:
            raw = json.dumps(json.loads(raw), indent=1)
        except json.JSONDecodeError as e:
            raise ExtractError(f"Invalid JSON: {e}")
    return [{"text": raw, "location": "document"}]


def normalize(text: str) -> str:
    import re
    text = text.replace("\x00", "")
    text = re.sub(r"[ \t]+", " ", text)
    return re.sub(r"\n{3,}", "\n\n", text).strip()


def chunk(text: str, size: int = 1200, overlap: int = 200) -> list[str]:
    """Split on paragraph boundaries, packing into ~size-char chunks with overlap."""
    paras = [p.strip() for p in text.split("\n\n") if p.strip()]
    chunks, cur = [], ""
    for p in paras:
        while len(p) > size:  # hard-split very long paragraphs
            head, p = p[:size], p[size - overlap:]
            if cur:
                chunks.append(cur); cur = ""
            chunks.append(head)
        if len(cur) + len(p) + 2 > size and cur:
            chunks.append(cur)
            cur = cur[-overlap:] + "\n\n" + p
        else:
            cur = f"{cur}\n\n{p}" if cur else p
    if cur:
        chunks.append(cur)
    return chunks
