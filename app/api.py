"""HTTP API. Thin layer: validation + delegation to services."""
from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Optional

from fastapi import APIRouter, Depends, File, Request, UploadFile
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field

from . import config
from .ai.correction_service import ai_status, suggest_correction
from .document.models import Block
from .editor import arcs
from .editor.chapter_editor import service
from .editor.glossary import GlossaryEntry
from .editor.validation import validate_translation
from .errors import ChapterError, NotFound
from .logging_setup import get_logger
from .publishing import publish_service
from .publishing.jobs import jobs
from .storage.project_state import store

router = APIRouter(prefix="/api")
log = get_logger("API")

MAX_UPLOAD = 100 * 1024 * 1024


class OpenPath(BaseModel):
    path: str


class LoadReq(BaseModel):
    ko_choice: int = 0
    vi_choice: int = 0


class DraftReq(BaseModel):
    blocks: list[Block]
    title: Optional[str] = None
    ko_blocks: Optional[list[Block]] = None     # the Korean text, only when the user edited it


class AddChapterReq(BaseModel):
    ko_text: str = Field("", max_length=400_000)
    vi_text: str = Field("", max_length=400_000)
    title: str = Field("", max_length=300)


class StatusReq(BaseModel):
    event: str = Field(pattern="^(review|ready|unready)$")
    confirm: bool = False


# --- projects -----------------------------------------------------------------
@router.get("/projects")
def list_projects():
    return store.list_projects()


@router.post("/projects/upload")
async def upload(file: UploadFile = File(...)):
    data = await file.read(MAX_UPLOAD + 1)
    if len(data) > MAX_UPLOAD:
        from .errors import DocxError
        raise DocxError("File quá lớn (>100MB).", code="DOCX_TOO_LARGE")
    project = service.upload(file.filename or "book.docx", data)
    return service.index(project["id"])


@router.post("/projects/open")
def open_path(req: OpenPath):
    project = service.open_path(req.path)
    return service.index(project["id"])


@router.get("/projects/{pid}")
def project_index(pid: str):
    return service.index(pid)


# --- chapters -----------------------------------------------------------------
@router.post("/projects/{pid}/chapters/{number}/load")
def load_chapter(pid: str, number: int, req: LoadReq | None = None):
    req = req or LoadReq()
    return service.load(pid, number, req.ko_choice, req.vi_choice)


@router.post("/projects/{pid}/chapters/{number}/add")
def add_chapter(pid: str, number: int, req: AddChapterReq):
    if not 1 <= number <= 9999:
        raise ChapterError("Số chương phải từ 1 đến 9999.", code="BAD_NUMBER")
    return service.add_missing(pid, number, req.ko_text, req.vi_text, req.title)


@router.get("/projects/{pid}/chapters/{number}")
def get_chapter(pid: str, number: int):
    return service.get(pid, number)


@router.put("/projects/{pid}/chapters/{number}/draft")
def save_draft(pid: str, number: int, req: DraftReq):
    return service.save_draft(pid, number, req.blocks, req.title, req.ko_blocks)


@router.post("/projects/{pid}/chapters/{number}/reset")
def reset_chapter(pid: str, number: int):
    return service.reset(pid, number)


@router.post("/projects/{pid}/chapters/{number}/status")
def set_status(pid: str, number: int, req: StatusReq):
    return service.set_status(pid, number, req.event, req.confirm)


@router.post("/projects/{pid}/chapters/{number}/export")
def export_chapter(pid: str, number: int):
    path = service.export(pid, number)
    return {"path": str(path), "filename": path.name, "download": f"/api/projects/{pid}/chapters/{number}/docx"}


@router.get("/projects/{pid}/chapters/{number}/docx")
def download_docx(pid: str, number: int):
    rec = store.get_chapter(pid, number)
    if not rec or not rec.get("exports"):
        raise NotFound("Chưa export chương này.")
    path = rec["exports"][-1]["path"]
    return FileResponse(path, filename=f"Chapter_{number}.docx",
                        media_type="application/vnd.openxmlformats-officedocument.wordprocessingml.document")


class ValidateReq(BaseModel):
    blocks: list[Block]


@router.post("/projects/{pid}/chapters/{number}/validate")
def validate(pid: str, number: int, req: ValidateReq):
    rec = service.get(pid, number)
    return validate_translation(req.blocks, [Block.model_validate(b) for b in rec.get("ko_blocks", [])],
                                store.get_glossary(pid))


# --- AI -----------------------------------------------------------------------
class SuggestReq(BaseModel):
    selected: str = Field(min_length=1, max_length=4000)
    paragraph_index: int = -1
    context_before: list[str] = Field(default_factory=list, max_length=5)
    context_after: list[str] = Field(default_factory=list, max_length=5)
    instruction: str = Field("", max_length=500)


@router.get("/ai/status")
def ai_status_route():
    return ai_status()


@router.post("/projects/{pid}/chapters/{number}/suggest")
def suggest(pid: str, number: int, req: SuggestReq):
    rec = store.get_chapter(pid, number)
    if rec is None:
        raise NotFound(f"Chương {number} chưa được load.")
    return suggest_correction(chapter=rec, glossary=store.get_glossary(pid), selected=req.selected,
                              context_before=req.context_before, context_after=req.context_after,
                              instruction=req.instruction)


# --- automatic chapter titles by story arc (see app/editor/arcs.py) -----------------------
class ArcIn(BaseModel):
    id: Optional[str] = None
    name: str = Field(max_length=200)
    ko: list[str] = Field(default_factory=list, max_length=20)
    big: bool = False
    notes: str = Field("", max_length=2000)
    last_n: Optional[int] = Field(None, ge=0, le=100000)


class ArcsReq(BaseModel):
    arcs: list[ArcIn] = Field(max_length=300)
    template: Optional[str] = Field(None, max_length=200)


class TitleReq(BaseModel):
    arc_id: Optional[str] = None
    new_name: str = Field("", max_length=200)


@router.get("/projects/{pid}/arcs")
def get_arcs(pid: str):
    store.get_project(pid)
    return arcs.view(pid)


@router.put("/projects/{pid}/arcs")
def put_arcs(pid: str, req: ArcsReq):
    store.get_project(pid)
    return arcs.update(pid, [a.model_dump() for a in req.arcs], req.template)


@router.post("/projects/{pid}/arcs/seed")
def seed_arcs(pid: str):
    store.get_project(pid)
    return arcs.seed(pid)


@router.post("/projects/{pid}/chapters/{number}/title/suggest")
def suggest_title(pid: str, number: int, req: TitleReq):
    store.get_project(pid)
    return arcs.suggest(pid, number, arc_id=req.arc_id, new_name=req.new_name)


@router.post("/projects/{pid}/chapters/{number}/title/apply")
def apply_title(pid: str, number: int, req: TitleReq):
    store.get_project(pid)
    return arcs.apply(pid, number, arc_id=req.arc_id, new_name=req.new_name)


class ArcNameReq(BaseModel):
    ko: str = Field(min_length=1, max_length=200)
    model: str = "claude-opus-5-5"


@router.post("/projects/{pid}/arcs/translate-name")
def translate_arc_name(pid: str, req: ArcNameReq):
    store.get_project(pid)
    return {"name": arcs.translate_name(pid, req.ko, req.model)}


class RetranslateReq(BaseModel):
    model: str = "claude-opus-5-5"
    effort: str = Field("medium", pattern="^(low|medium|high|xhigh|max)$")
    instruction: str = Field("", max_length=1000)
    auto_fix: bool = True
    parallel: int = Field(1, ge=1, le=4)
    current: list[str] | None = Field(None, max_length=5000)   # Vietnamese paragraphs to revise instead of re-translating


@router.post("/projects/{pid}/chapters/{number}/retranslate")
def retranslate(pid: str, number: int, req: RetranslateReq):
    """Translate the whole chapter again from the Korean source. Returns the result only — the UI applies it."""
    from .ai.retranslate import retranslate_chapter
    return retranslate_chapter(pid, number, model=req.model, effort=req.effort, instruction=req.instruction,
                                auto_fix=req.auto_fix, parallel=req.parallel, current=req.current)


# --- Wattpad ------------------------------------------------------------------
class PublishReq(BaseModel):
    story_id: str = Field(min_length=1, max_length=40, pattern=r"^\d+$")
    story_title: str = ""
    title: str = Field(min_length=1, max_length=200)
    mode: str = Field("publish", pattern="^(publish|draft)$")
    confirm: bool = False
    part_url: str = Field("", max_length=500)   # Wattpad link of this chapter: update that part instead of adding one


@router.get("/wattpad/status")
def wattpad_status():
    return publish_service.status()


@router.get("/wattpad/myworks.png")
def wattpad_myworks_snapshot():
    path = config.LOG_DIR / "wattpad" / "myworks-latest.png"
    if not path.exists():
        raise NotFound("Chưa có ảnh chụp My Works.")
    return FileResponse(path, media_type="image/png", headers={"Cache-Control": "no-store"})


@router.post("/wattpad/login")
def wattpad_login():
    return publish_service.publisher.login()


@router.post("/wattpad/stories")
def wattpad_stories():
    return publish_service.publisher.stories()


class ManualStoryReq(BaseModel):
    link: str = Field(min_length=1, max_length=500)
    title: str = Field("", max_length=200)


@router.post("/wattpad/stories/manual")
def wattpad_add_story(req: ManualStoryReq):
    return publish_service.add_manual_story(req.link, req.title)


@router.post("/projects/{pid}/chapters/{number}/publish")
def publish_chapter(pid: str, number: int, req: PublishReq):
    return publish_service.publisher.publish(pid, number, story_id=req.story_id, story_title=req.story_title,
                                             title=req.title, mode=req.mode, confirm=req.confirm, part_url=req.part_url)


class BatchPublishReq(BaseModel):
    story_id: str = Field(min_length=1, max_length=40, pattern=r"^\d+$")
    story_title: str = ""
    chapters: list[int] = Field(min_length=1, max_length=publish_service.BATCH_MAX)
    mode: str = Field("publish", pattern="^(publish|draft)$")
    confirm: bool = False


@router.post("/projects/{pid}/publish-batch")
def publish_batch(pid: str, req: BatchPublishReq):
    return publish_service.publisher.publish_batch(pid, req.chapters, story_id=req.story_id,
                                                   story_title=req.story_title, mode=req.mode, confirm=req.confirm)


@router.get("/wattpad/batch")
def wattpad_batch():
    return {"batch": publish_service.publisher.batch_status()}


@router.post("/wattpad/batch/{batch_id}/cancel")
def wattpad_batch_cancel(batch_id: str):
    return {"batch": publish_service.publisher.cancel_batch(batch_id)}


@router.get("/jobs/{job_id}")
def job_status(job_id: str):
    return jobs.get(job_id)


@router.post("/jobs/{job_id}/cancel")
def job_cancel(job_id: str):
    publish_service.publisher.bridge.cancel(job_id)
    return jobs.get(job_id)


# --- browser extension bridge (see app/publishing/extension_bridge.py) ------------
def require_extension(request: Request) -> None:
    origin = request.headers.get("origin", "")
    if request.headers.get("x-nt-extension") != "1" or origin.startswith(("http://", "https://")):
        from .publishing.extension_bridge import ExtensionAuthError
        raise ExtensionAuthError("Chỉ extension Novel Translator mới được gọi API này.")
    # 1.0.0 extensions did not send a version
    publish_service.publisher.bridge.version = request.headers.get("x-nt-version", "1.0.0")[:20]


ext = APIRouter(prefix="/api/ext", dependencies=[Depends(require_extension)])


class ExtEvent(BaseModel):
    stage: Optional[str] = Field(None, max_length=40)
    part_id: Optional[str] = Field(None, pattern=r"^\d{1,15}$")
    from_part: Optional[str] = Field(None, pattern=r"^\d{0,15}$")
    part_gone: bool = False
    tab: Optional[str] = Field(None, max_length=40)
    message: Optional[str] = Field(None, max_length=1000)
    final: bool = False
    ok: bool = False
    published: bool = False
    error_code: Optional[str] = Field(None, max_length=60)
    diag: Optional[dict] = None


class ExtStories(BaseModel):
    stories: list[dict] = Field(default_factory=list, max_length=500)
    page_url: str = ""


class ExtScanChunk(BaseModel):
    parts: list[dict] = Field(default_factory=list, max_length=100)


class ExtScanEvent(BaseModel):
    message: Optional[str] = Field(None, max_length=300)
    story_done: bool = False
    error: Optional[str] = Field(None, max_length=300)
    done: bool = False
    tab: Optional[str] = Field(None, max_length=40)


@ext.get("/scan")
def ext_scan(tab: str = ""):
    from .ai.wattpad_terms import wattpad_scan
    publish_service.publisher.bridge.touch()
    return {"task": wattpad_scan.task_for_extension(tab[:40])}


@ext.post("/scan/{task_id}/chunk")
def ext_scan_chunk(task_id: str, req: ExtScanChunk):
    from .ai.wattpad_terms import wattpad_scan
    return wattpad_scan.ingest(task_id, req.parts)


@ext.post("/scan/{task_id}/event")
def ext_scan_event(task_id: str, req: ExtScanEvent):
    from .ai.wattpad_terms import wattpad_scan
    return wattpad_scan.event(task_id, req.model_dump(exclude_none=True))


@ext.get("/config")
def ext_config():
    return publish_service.publisher.bridge.ext_config()


@ext.get("/task")
def ext_task(tab: str = ""):
    return {"task": publish_service.publisher.bridge.current_task(tab[:40])}


@ext.post("/task/{task_id}/event")
def ext_event(task_id: str, ev: ExtEvent):
    return publish_service.publisher.bridge.event(task_id, ev.model_dump(exclude_none=True))


@ext.post("/stories")
def ext_stories(req: ExtStories):
    publish_service.publisher.bridge.touch()
    return publish_service.save_stories_from_extension(req.stories, req.page_url)


@ext.post("/diag")
def ext_diag(diag: dict):
    return {"path": publish_service.publisher.bridge.save_diag(diag)}


class ExtNovelpia(BaseModel):
    novel_no: str = Field(pattern=r"^\d{1,12}$")
    content_no: str = Field(pattern=r"^\d{1,12}$")
    novel_title: str = Field("", max_length=300)
    number: int = Field(0, ge=0, le=9999)  # "EP.12" -> 12; Prologue / 외전 -> 0
    title: str = Field("", max_length=300)
    text: str = Field(min_length=1, max_length=500_000)


@ext.post("/novelpia")
def ext_novelpia(req: ExtNovelpia):
    """Saves one chapter read by the extension from the user's own logged-in Novelpia tab, then rebuilds
    `<novel>.txt` ("제N화 title" headings) so it can be opened in the Translate tab like any raw file."""
    from .storage.project_state import slugify
    d = config.DATA_DIR / "novelpia" / req.novel_no
    d.mkdir(parents=True, exist_ok=True)
    (d / f"{req.content_no}.json").write_text(json.dumps(req.model_dump(), ensure_ascii=False), encoding="utf-8")
    eps = sorted((json.loads(f.read_text(encoding="utf-8")) for f in d.glob("*.json")),
                 key=lambda e: (e["number"], int(e["content_no"])))
    out = d / f"{slugify(req.novel_title or req.novel_no)}.txt"
    out.write_text("\n\n".join(f"제{e['number']}화 {e['title']}".strip() + "\n\n" + e["text"] for e in eps) + "\n",
                   encoding="utf-8")
    return {"path": str(out), "chapters": len(eps), "number": req.number}


# --- glossary -----------------------------------------------------------------
@router.get("/projects/{pid}/glossary")
def get_glossary(pid: str):
    return store.get_glossary(pid)


@router.put("/projects/{pid}/glossary")
def put_glossary(pid: str, entries: list[GlossaryEntry]):
    log.info("Glossary saved: %d entries", len(entries))
    return store.save_glossary(pid, [e.model_dump() for e in entries])


# --- AI translation of a whole novel (see app/ai/translation_jobs.py) ----------------
class TranslatePath(BaseModel):
    path: str


class TranslateStyle(BaseModel):
    style: str = Field("", max_length=20000)


class TranslateJobReq(BaseModel):
    sid: str
    model: str = "claude-opus-5-5"
    effort: str = Field("medium", pattern="^(low|medium|high|xhigh|max)$")
    concurrency: int = Field(2, ge=1, le=4)
    from_number: Optional[int] = None
    to_number: Optional[int] = None
    glossary_project: Optional[str] = None
    style: str = Field("", max_length=20000)


@router.get("/translate/info")
def translate_info():
    from .ai import novel_translator as nt
    import os
    from .ai import antigravity, claude_code, grok, grok_cli, omniroute
    return {"models": [{"id": k, "label": v["label"], "price": v["price"], "subscription": bool(v.get("subscription")),
                        "provider": v.get("provider", "api")} for k, v in nt.MODELS.items()],
            "claude_code": claude_code.status(),
            "antigravity": antigravity.status(),
            "grok": grok.status(),
            "grok_cli": grok_cli.status(),
            "omniroute": omniroute.status(),
            "default_model": nt.DEFAULT_MODEL, "style": tj().get_style(), "default_style": nt.DEFAULT_STYLE,
            "provider": os.getenv("TRANSLATE_PROVIDER", "anthropic"),
            "has_key": bool(os.getenv("ANTHROPIC_API_KEY", "").strip())}


@router.get("/translate/antigravity")
def translate_antigravity(refresh: bool = False):
    """Runs `agy models`: shows whether the CLI is signed in and which Gemini models it offers."""
    from .ai import antigravity
    st = dict(antigravity.status(probe=True, force=refresh))
    st["picked"] = {t: antigravity.pick_model(t, st["models"], "high") for t in ("pro", "flash")}
    return st


@router.get("/translate/grok")
def translate_grok(refresh: bool = False):
    """Lists the Grok models the XAI_API_KEY can use (GET /v1/models) and the slugs the tool will pick."""
    from .ai import grok
    return grok.status(probe=True, force=refresh)


@router.get("/translate/omniroute")
def translate_omniroute(refresh: bool = False):
    """Asks the OmniRoute gateway for its model list: is the key valid and does OMNIROUTE_MODEL exist?"""
    from .ai import omniroute
    return omniroute.status(probe=True, force=refresh)


@router.get("/translate/grok-cli")
def translate_grok_cli(refresh: bool = False):
    """Runs `grok models`: is the CLI installed and signed in, and which models it offers."""
    from .ai import grok_cli
    return grok_cli.status(probe=True, force=refresh)


def tj():
    from .ai import translation_jobs
    return translation_jobs


@router.post("/translate/analyze")
async def translate_analyze(file: UploadFile = File(...)):
    data = await file.read(MAX_UPLOAD + 1)
    if len(data) > MAX_UPLOAD:
        from .errors import DocxError
        raise DocxError("File quá lớn (>100MB).", code="DOCX_TOO_LARGE")
    return tj().analyze(file.filename or "truyen.txt", data)


@router.post("/translate/analyze-path")
def translate_analyze_path(req: TranslatePath):
    return tj().analyze_path(req.path)


@router.put("/translate/style")
def translate_save_style(req: TranslateStyle):
    return {"style": tj().save_style(req.style)}


@router.get("/translate/jobs")
def translate_jobs():
    return tj().manager.list()


@router.post("/translate/jobs")
def translate_create(req: TranslateJobReq):
    return tj().manager.create(sid=req.sid, model=req.model, effort=req.effort, concurrency=req.concurrency,
                               from_number=req.from_number, to_number=req.to_number,
                               glossary_project=req.glossary_project, style=req.style)


@router.get("/translate/jobs/{jid}")
def translate_job(jid: str):
    return tj().manager.get(jid)


@router.post("/translate/jobs/{jid}/{action}")
def translate_action(jid: str, action: str, idx: Optional[int] = None):
    m = tj().manager
    if action == "pause":
        return m.pause(jid)
    if action == "resume":
        return m.start(jid)
    if action == "retry-failed":
        return m.retry_failed(jid)
    if action == "retry-chapter" and idx is not None:
        return m.retry_chapter(jid, idx)
    if action == "build":
        m.build_output(jid)
        return m.get(jid)
    if action == "save-terms":
        return translate_save_terms(jid)
    if action == "open":
        out = m.build_output(jid)
        project = store.upsert_project(Path(out["docx"]).name, source_path=out["docx"])
        return {"project_id": project["id"]}
    raise NotFound("Hành động không hợp lệ.")


@router.delete("/translate/jobs/{jid}")
def translate_delete(jid: str):
    tj().manager.delete(jid)
    return {"ok": True}


@router.get("/translate/jobs/{jid}/download")
def translate_download(jid: str, fmt: str = "docx"):
    job = tj().manager.get(jid)
    out = job.get("output") or {}
    path = out.get(fmt)
    if not path or not Path(path).exists():
        raise NotFound("Chưa có file kết quả — bấm “Xuất file” trước.")
    media = ("application/vnd.openxmlformats-officedocument.wordprocessingml.document" if fmt == "docx"
             else "text/plain; charset=utf-8")
    return FileResponse(path, filename=Path(path).name, media_type=media)


# --- termbase: consistent fixed terms across chapters (see app/ai/termbase.py) ------
def tb():
    from .ai import termbase
    return termbase


class NotesReq(BaseModel):
    notes: str = Field("", max_length=20000)


class ImportText(BaseModel):
    text: str = Field(min_length=1, max_length=2_000_000)
    overwrite: bool = True


class ScanReq(BaseModel):
    from_number: Optional[int] = None
    to_number: Optional[int] = None
    model: str = "claude-opus-5-5"


class DecideReq(BaseModel):
    accept: list[dict] = Field(default_factory=list)
    reject: list[str] = Field(default_factory=list)


@router.get("/projects/{pid}/termbase")
def termbase_overview(pid: str):
    return tb().overview(pid)


@router.put("/projects/{pid}/termbase/notes")
def termbase_notes(pid: str, req: NotesReq):
    return {"notes": tb().set_notes(pid, req.notes)}


@router.post("/projects/{pid}/termbase/import")
async def termbase_import_file(pid: str, file: UploadFile = File(...), overwrite: bool = True):
    data = await file.read(20 * 1024 * 1024)
    name = file.filename or "data.txt"
    if name.lower().endswith(".docx"):
        import tempfile
        from .document.docx_reader import read_docx
        with tempfile.NamedTemporaryFile(suffix=".docx", delete=False) as f:
            f.write(data)
        try:
            text = "\n".join(b.text for b in read_docx(f.name))
        finally:
            Path(f.name).unlink(missing_ok=True)
    else:
        text = None
        for enc in ("utf-8-sig", "cp949", "utf-16"):
            try:
                text = data.decode(enc)
                break
            except UnicodeDecodeError:
                continue
        if text is None:
            from .errors import DocxError
            raise DocxError("Không đọc được file (hãy lưu dạng UTF-8).", code="TXT_ENCODING")
    return tb().import_data(pid, text, name, overwrite)


@router.post("/projects/{pid}/termbase/import-text")
def termbase_import_text(pid: str, req: ImportText):
    return tb().import_data(pid, req.text, "", req.overwrite)


@router.post("/projects/{pid}/termbase/check")
def termbase_check(pid: str):
    return tb().check_consistency(pid)


@router.get("/projects/{pid}/termbase/scan-estimate")
def termbase_scan_estimate(pid: str, from_number: Optional[int] = None, to_number: Optional[int] = None):
    return tb().scan_estimate(pid, from_number, to_number)


@router.post("/projects/{pid}/termbase/scan")
def termbase_scan(pid: str, req: ScanReq):
    return tb().scanner.start(pid, req.from_number, req.to_number, req.model)


@router.post("/projects/{pid}/termbase/scan/cancel")
def termbase_scan_cancel(pid: str):
    return tb().scanner.cancel(pid)


class WattpadScanReq(BaseModel):
    stories: list[dict] = Field(min_length=1, max_length=40)
    from_number: Optional[int] = None
    to_number: Optional[int] = None
    korean_path: str = Field(max_length=500)
    model: str = "claude-opus-5-5"


@router.get("/projects/{pid}/termbase/wattpad-scan")
def wattpad_scan_state(pid: str):
    from .ai.wattpad_terms import wattpad_scan
    return {**wattpad_scan.state(pid), "extension_connected": publish_service.publisher.bridge.connected()}


@router.post("/projects/{pid}/termbase/wattpad-scan")
def wattpad_scan_start(pid: str, req: WattpadScanReq):
    from .ai.wattpad_terms import wattpad_scan
    stories = [{"id": str(s["id"]), "title": str(s.get("title", ""))[:200]} for s in req.stories if re.fullmatch(r"\d{1,15}", str(s.get("id", "")))]
    return wattpad_scan.start(pid, stories=stories, from_number=req.from_number, to_number=req.to_number,
                              korean_path=req.korean_path, model=req.model)


@router.post("/projects/{pid}/termbase/wattpad-scan/cancel")
def wattpad_scan_cancel(pid: str):
    from .ai.wattpad_terms import wattpad_scan
    return wattpad_scan.cancel(pid)


@router.post("/projects/{pid}/termbase/proposals")
def termbase_decide(pid: str, req: DecideReq):
    return tb().scanner.decide(pid, req.accept, req.reject)


# (reached through translate_action: the generic /translate/jobs/{jid}/{action} route matches first)
def translate_save_terms(jid: str):
    job = tj().manager.get(jid)
    if not job.get("glossary_project"):
        raise NotFound("Lượt dịch này không gắn với truyện nào (chưa chọn “Dùng glossary của”).")
    terms = [{"source": s, "target": t} for s, t in (job.get("auto_terms") or {}).items()]
    n = tb().scanner.add_proposals(job["glossary_project"], terms, origin="translation")
    return {"added": n, "project_id": job["glossary_project"]}


# --- bringing raw data in (see app/tools/data_import.py) ---------------------------
class CleanRoughReq(BaseModel):
    path: str
    story: Optional[str] = Field(None, max_length=80)


class PrepareRawReq(BaseModel):
    path: str
    story: Optional[str] = Field(None, max_length=80)
    from_number: Optional[int] = None
    to_number: Optional[int] = None


class StoryReq(BaseModel):
    name: str = Field(min_length=1, max_length=80)


@router.post("/tools/clean-rough")
def tools_clean_rough(req: CleanRoughReq):
    from .tools.data_import import clean_rough_file
    return clean_rough_file(req.path, req.story)


@router.post("/tools/prepare-raw")
def tools_prepare_raw(req: PrepareRawReq):
    from .tools.data_import import prepare_raw
    return prepare_raw(req.path, req.story, req.from_number, req.to_number)


@router.get("/stories")
def stories():
    return store.list_stories()


@router.put("/projects/{pid}/story")
def set_story(pid: str, req: StoryReq):
    return store.set_story(pid, req.name)


@router.get("/translate/sources")
def translate_sources():
    from .ai.translation_jobs import SOURCES
    from .storage.project_state import _read_json
    out = []
    for f in SOURCES.glob("*/meta.json"):
        m = _read_json(f) or {}
        if m:
            out.append({k: m.get(k) for k in ("sid", "name", "kind", "created_at", "todo", "already_translated", "chars")})
    return sorted(out, key=lambda m: m.get("created_at") or "", reverse=True)


@router.get("/translate/sources/{sid}")
def translate_source(sid: str):
    return tj().source_meta(sid)
