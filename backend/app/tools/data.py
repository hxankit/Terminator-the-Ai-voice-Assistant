"""Document, knowledge-base, data-analysis, code-execution and file-generation tools."""
import io, sys
from pathlib import Path
from bson import ObjectId
from pydantic import BaseModel, Field
from ..db import db
from ..files import save_generated
from ..rag.extract import extract, ExtractError
from ..rag.service import retrieve
from . import sandbox
from .base import tool, ToolError, ToolContext


async def _doc(ctx: ToolContext, document_id: str) -> dict:
    try:
        d = await db().documents.find_one({"_id": ObjectId(document_id), "user_id": ctx.user["_id"]})
    except Exception:
        d = None
    if not d:
        raise ToolError(f"Document {document_id} not found")
    return d


class KBArgs(BaseModel):
    query: str = Field(min_length=2, max_length=500)
    document_ids: list[str] = Field(default_factory=list, description="Optionally restrict to these documents")


@tool("kb_search", "Search the user's uploaded documents (knowledge base). Returns relevant passages with "
      "filename and location; cite them as [filename, location].", KBArgs)
async def kb_search(a: KBArgs, ctx: ToolContext):
    ids = [ObjectId(i) for i in a.document_ids if ObjectId.is_valid(i)] or None
    hits = await retrieve(ctx.user, a.query, k=6, document_ids=ids)
    return hits or "No relevant passages found in the knowledge base."


class ListDocsArgs(BaseModel):
    pass


@tool("list_documents", "List the user's uploaded documents with ids, types and status.", ListDocsArgs)
async def list_documents(a: ListDocsArgs, ctx: ToolContext):
    return [{"id": str(d["_id"]), "filename": d["filename"], "status": d.get("status"), "chunks": d.get("chunks")}
            async for d in db().documents.find({"user_id": ctx.user["_id"]}).sort("created_at", -1).limit(50)]


class ReadDocArgs(BaseModel):
    document_id: str
    max_chars: int = Field(20000, ge=500, le=60000)


@tool("read_document", "Read the full extracted text of an uploaded document (use for summaries / extraction).", ReadDocArgs)
async def read_document(a: ReadDocArgs, ctx: ToolContext):
    d = await _doc(ctx, a.document_id)
    try:
        secs = extract(Path(d["path"]))
    except ExtractError as e:
        raise ToolError(str(e))
    text = "\n\n".join(f"[{s['location']}]\n{s['text']}" for s in secs)
    return text[:a.max_chars] + (f"\n...[truncated, {len(text)} chars total]" if len(text) > a.max_chars else "")


class TableArgs(BaseModel):
    document_id: str


@tool("profile_table", "Profile a CSV/Excel document: sheets, columns, dtypes, row counts, summary statistics, "
      "missing values and first rows.", TableArgs)
async def profile_table(a: TableArgs, ctx: ToolContext):
    import pandas as pd
    d = await _doc(ctx, a.document_id)
    p = Path(d["path"])
    if p.suffix.lower() == ".csv":
        frames = {"csv": pd.read_csv(p)}
    elif p.suffix.lower() in (".xlsx", ".xls"):
        frames = pd.read_excel(p, sheet_name=None)
    else:
        raise ToolError("profile_table only supports CSV/Excel")
    out = {}
    for name, df in frames.items():
        out[name] = {"rows": len(df), "columns": {c: str(t) for c, t in df.dtypes.items()},
                     "missing": {c: int(v) for c, v in df.isna().sum().items() if v},
                     "describe": df.describe(include="all").round(3).fillna("").astype(str).to_dict(),
                     "head": df.head(5).astype(str).to_dict(orient="records")}
    return out


class PyArgs(BaseModel):
    code: str = Field(min_length=1, max_length=50000)
    document_ids: list[str] = Field(default_factory=list,
                                    description="Uploaded documents to copy into the working dir (by original filename)")


@tool("run_python", "Execute Python 3 in an isolated sandbox (no network, 1GB RAM, time-limited). pandas, numpy, "
      "openpyxl are available. Attached documents are in the working directory under their filenames. "
      "Save files to ./out/ to return them to the user as downloads. Print results to stdout.",
      PyArgs, risk="sandboxed", requires=sandbox.unavailable_reason)
async def run_python(a: PyArgs, ctx: ToolContext):
    files = {}
    for did in a.document_ids:
        d = await _doc(ctx, did)
        files[d["filename"]] = Path(d["path"]).read_bytes()
    res = await sandbox.run_python(a.code, files)
    links = [await save_generated(ctx.user, n, b, ctx.conversation_id) for n, b in res.pop("outputs").items()]
    if res["timed_out"]:
        res["stderr"] += "\n[killed: time limit exceeded]"
    res["files"] = links
    return res


class Sheet(BaseModel):
    name: str = Field("Sheet1", max_length=31)
    columns: list[str] = Field(min_length=1, max_length=200)
    rows: list[list] = Field(max_length=100000)


class ExcelArgs(BaseModel):
    filename: str = Field("report.xlsx", max_length=120)
    sheets: list[Sheet] = Field(min_length=1, max_length=20)


@tool("create_excel", "Create an Excel (.xlsx) report from tabular data and return a download link.", ExcelArgs, risk="write")
async def create_excel(a: ExcelArgs, ctx: ToolContext):
    from openpyxl import Workbook
    from openpyxl.styles import Font, PatternFill
    wb = Workbook()
    wb.remove(wb.active)
    for s in a.sheets:
        ws = wb.create_sheet(s.name)
        ws.append(s.columns)
        for c in ws[1]:
            c.font, c.fill = Font(bold=True, color="FFFFFF"), PatternFill("solid", fgColor="334155")
        for r in s.rows:
            ws.append([v if isinstance(v, (int, float, str, type(None))) else str(v) for v in r])
        for i, col in enumerate(s.columns, 1):
            width = max([len(str(col))] + [len(str(r[i - 1])) for r in s.rows[:200] if len(r) >= i])
            ws.column_dimensions[ws.cell(1, i).column_letter].width = min(max(10, width + 2), 60)
        ws.freeze_panes = "A2"
    buf = io.BytesIO()
    wb.save(buf)
    name = a.filename if a.filename.endswith(".xlsx") else a.filename + ".xlsx"
    return await save_generated(ctx.user, name, buf.getvalue(), ctx.conversation_id)


class FileArgs(BaseModel):
    filename: str = Field(max_length=120)
    content: str = Field(max_length=500000)


@tool("create_file", "Create a downloadable text file (markdown report, CSV, JSON, code...).", FileArgs, risk="write")
async def create_file(a: FileArgs, ctx: ToolContext):
    return await save_generated(ctx.user, a.filename, a.content.encode(), ctx.conversation_id)
