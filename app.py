import io
import time
import uuid
import threading
import traceback
from collections import OrderedDict
from concurrent.futures import ThreadPoolExecutor

from flask import Flask, request, send_file, jsonify, send_from_directory
from flask_cors import CORS

from bcsfe import core

# bcsfe 내부 데이터(로케일, 설정, 테마 등)를 사용하기 전에 반드시 초기화해야 함
core.core_data.init_data()

app = Flask(__name__, static_folder="static", static_url_path="")
CORS(app)

MAX_UPLOAD_MB = 5
app.config["MAX_CONTENT_LENGTH"] = MAX_UPLOAD_MB * 1024 * 1024

# ---------------------------------------------------------------------------
# 동시 처리 30명 제한 + 대기열
# ---------------------------------------------------------------------------
MAX_CONCURRENT = 30
executor = ThreadPoolExecutor(max_workers=MAX_CONCURRENT)

JOBS_LOCK = threading.Lock()
JOBS = OrderedDict()  # job_id -> job info
QUEUE_ORDER = []  # 아직 처리 시작 안 한 job_id들의 순서

JOB_TTL_SECONDS = 10 * 60  # 완료된 작업 결과 보관 시간


def _cleanup_old_jobs():
    now = time.time()
    with JOBS_LOCK:
        expired = [
            jid
            for jid, job in JOBS.items()
            if job["status"] in ("done", "error")
            and job["finished_at"] is not None
            and now - job["finished_at"] > JOB_TTL_SECONDS
        ]
        for jid in expired:
            del JOBS[jid]


def _create_job(kind: str) -> str:
    job_id = uuid.uuid4().hex
    with JOBS_LOCK:
        JOBS[job_id] = {
            "kind": kind,
            "status": "queued",
            "created_at": time.time(),
            "finished_at": None,
            "result": None,
            "error": None,
        }
        QUEUE_ORDER.append(job_id)
    return job_id


def _get_queue_position(job_id: str) -> int:
    with JOBS_LOCK:
        if job_id not in QUEUE_ORDER:
            return 0
        return QUEUE_ORDER.index(job_id) + 1


def _mark_processing(job_id: str):
    with JOBS_LOCK:
        if job_id in QUEUE_ORDER:
            QUEUE_ORDER.remove(job_id)
        JOBS[job_id]["status"] = "processing"


def _mark_done(job_id: str, result: dict):
    with JOBS_LOCK:
        JOBS[job_id]["status"] = "done"
        JOBS[job_id]["result"] = result
        JOBS[job_id]["finished_at"] = time.time()


def _mark_error(job_id: str, message: str, http_status: int = 500):
    with JOBS_LOCK:
        JOBS[job_id]["status"] = "error"
        JOBS[job_id]["error"] = {"message": message, "status": http_status}
        JOBS[job_id]["finished_at"] = time.time()


# ---------------------------------------------------------------------------
# 세이브 수정 로직
# ---------------------------------------------------------------------------
def load_save(raw_bytes: bytes) -> "core.SaveFile":
    """save 파일 바이트를 받아 bcsfe SaveFile 객체로 로드합니다."""
    data = core.Data(raw_bytes)
    # cc(국가 코드)는 자동 감지됨 (SaveFile.__init__ 내부 detect_cc 사용)
    save_file = core.SaveFile(data)
    return save_file


def _safe(warnings: list, label: str, fn):
    """개별 기능 적용을 시도하고, 실패해도 전체 작업이 죽지 않게 감쌉니다.
    (설치된 bcsfe 버전에 따라 일부 내부 속성명이 다를 수 있어 방어적으로 처리)"""
    try:
        fn()
    except Exception as e:
        warnings.append(f"'{label}' 기능은 이번 세이브/버전에서 적용하지 못했습니다 ({e.__class__.__name__})")


def apply_edits(save_file: "core.SaveFile", opts: dict) -> list:
    """opts(dict)에 담긴 옵션에 따라 세이브 파일을 수정합니다.
    반환값: 적용 중 건너뛴 기능에 대한 경고 메시지 목록"""
    warnings: list = []

    # ---- 재화 ----
    if "catfood" in opts:
        save_file.catfood = int(opts["catfood"])

    if "xp" in opts:
        save_file.xp = int(opts["xp"])

    if "np" in opts:
        save_file.np = int(opts["np"])

    if "rare_tickets" in opts:
        save_file.rare_tickets = int(opts["rare_tickets"])

    if "platinum_tickets" in opts:
        save_file.platinum_tickets = int(opts["platinum_tickets"])

    if "legend_tickets" in opts:
        save_file.legend_tickets = int(opts["legend_tickets"])

    if opts.get("max_catfruit"):
        def _f():
            save_file.catfruit = [9999 for _ in save_file.catfruit]
        _safe(warnings, "캣프루트 전체 최대치", _f)

    if opts.get("max_catseyes"):
        def _f():
            save_file.catseyes = [9999 for _ in save_file.catseyes]
        _safe(warnings, "캣아이 전체 최대치", _f)

    if opts.get("max_talent_orbs"):
        def _f():
            orbs = save_file.talent_orbs
            for k in list(orbs.keys()):
                orbs[k] = 9999
            save_file.talent_orbs = orbs
        _safe(warnings, "인식표(탈렌트 오브) 전체 최대치", _f)

    # ---- 고양이 ----
    if opts.get("unlock_all_cats"):
        level = opts.get("cat_level")
        want_true_form = bool(opts.get("true_form"))
        want_fourth_form = bool(opts.get("fourth_form"))
        want_max_talents = bool(opts.get("max_talents"))
        exclude_673 = bool(opts.get("exclude_673"))

        for cat in save_file.cats.get_all_cats():
            if exclude_673 and cat.id == 673:
                continue

            def _unlock(cat=cat):
                cat.unlock(save_file)
            _safe(warnings, "모든 고양이 해금", _unlock)

            if level:
                def _lvl(cat=cat):
                    lv = max(int(level) - 1, 0)
                    cat.upgrade.base = lv
                    cat.upgrade.plus = 0
                _safe(warnings, "모든 고양이 레벨 설정", _lvl)

            if want_true_form:
                def _tf(cat=cat):
                    cat.true_form(save_file, set_current_form=False)
                _safe(warnings, "궁극진화(트루폼) 전체 적용", _tf)

            if want_fourth_form:
                def _ff(cat=cat):
                    # 일부 고양이만 4단 진화가 존재하므로, 없으면 자동으로 건너뜀
                    fourth = getattr(cat, "fourth_form", None)
                    if fourth is not None:
                        fourth(save_file, set_current_form=False)
                _safe(warnings, "4단 진화(초진화) 전체 적용", _ff)

            if want_max_talents:
                def _tal(cat=cat):
                    for talent in getattr(cat, "talents", []):
                        max_v = getattr(talent, "max_value", None)
                        if max_v is not None:
                            talent.value = max_v
                _safe(warnings, "특수능력(탈렌트) 전체 최대치", _tal)

    # ---- 도감 / 장비 / 메달 ----
    if opts.get("unlock_enemy_guide"):
        def _f():
            for enemy in save_file.enemy_guide.get_all_enemies():
                enemy.unlock(save_file)
        _safe(warnings, "적 도감(에너미 가이드) 전체 해금", _f)

    if opts.get("unlock_equip_slots"):
        def _f():
            save_file.slots = 20
        _safe(warnings, "장비(특능) 슬롯 전체 해금", _f)

    if opts.get("all_medals"):
        def _f():
            for medal in save_file.medals.get_all_medals():
                medal.obtain(save_file)
        _safe(warnings, "냥메달 전체 획득", _f)

    return warnings


def _to_bool_opts(d: dict) -> dict:
    for key in (
        "max_catfruit",
        "max_catseyes",
        "max_talent_orbs",
        "unlock_all_cats",
        "true_form",
        "fourth_form",
        "max_talents",
        "exclude_673",
        "unlock_enemy_guide",
        "unlock_equip_slots",
        "all_medals",
    ):
        if key in d:
            d[key] = d[key] in ("true", "on", "1", True)
    return d


# ---------------------------------------------------------------------------
# 백그라운드 작업 함수
# ---------------------------------------------------------------------------
def _run_file_job(job_id: str, raw_bytes: bytes, opts: dict):
    _mark_processing(job_id)
    try:
        save_file = load_save(raw_bytes)
        warnings = apply_edits(save_file, opts)
        out_bytes = bytes(save_file.to_data())
    except core.SaveError as e:
        _mark_error(job_id, f"세이브 파일을 읽을 수 없습니다: {e}", 400)
        return
    except Exception:
        traceback.print_exc()
        _mark_error(job_id, "세이브 파일 처리 중 오류가 발생했습니다. 파일 형식을 확인해주세요.", 500)
        return

    _mark_done(job_id, {"file_bytes": out_bytes, "warnings": warnings})


def _run_code_job(job_id: str, transfer_code: str, confirmation_code: str, cc_str: str, opts: dict):
    _mark_processing(job_id)

    try:
        cc = core.CountryCode(cc_str)
    except Exception:
        _mark_error(job_id, "국가 코드가 올바르지 않습니다 (kr/en/jp/tw)", 400)
        return

    gv = core.GameVersion(120200)  # 이어하기 요청에는 정확한 버전이 중요하지 않음

    try:
        server_handler, _result = core.ServerHandler.from_codes(
            transfer_code, confirmation_code, cc, gv, print=False
        )
    except Exception:
        traceback.print_exc()
        _mark_error(job_id, "서버 요청 중 오류가 발생했습니다", 500)
        return

    if server_handler is None:
        _mark_error(
            job_id,
            "이어하기 코드 또는 인증번호가 올바르지 않습니다 (국가 코드도 확인해주세요)",
            400,
        )
        return

    try:
        warnings = apply_edits(server_handler.save_file, opts)
    except Exception:
        traceback.print_exc()
        _mark_error(job_id, "세이브 수정 중 오류가 발생했습니다", 500)
        return

    try:
        codes = server_handler.get_codes()
    except Exception:
        traceback.print_exc()
        _mark_error(job_id, "수정된 데이터를 서버에 업로드하는 중 오류가 발생했습니다", 500)
        return

    if codes is None:
        _mark_error(job_id, "새 이어하기 코드 발급에 실패했습니다", 500)
        return

    new_transfer_code, new_confirmation_code = codes
    _mark_done(
        job_id,
        {
            "transfer_code": new_transfer_code,
            "confirmation_code": new_confirmation_code,
            "warnings": warnings,
        },
    )


# ---------------------------------------------------------------------------
# 라우트
# ---------------------------------------------------------------------------
@app.route("/")
def index():
    return send_from_directory("static", "index.html")


@app.route("/api/jobs/file", methods=["POST"])
def submit_file_job():
    _cleanup_old_jobs()

    if "save_file" not in request.files:
        return jsonify({"error": "save_file이 없습니다"}), 400

    upload = request.files["save_file"]
    raw_bytes = upload.read()
    if not raw_bytes:
        return jsonify({"error": "파일이 비어 있습니다"}), 400

    opts = _to_bool_opts(request.form.to_dict())

    job_id = _create_job("file")
    executor.submit(_run_file_job, job_id, raw_bytes, opts)

    return jsonify({"job_id": job_id, "queue_position": _get_queue_position(job_id)})


@app.route("/api/jobs/code", methods=["POST"])
def submit_code_job():
    _cleanup_old_jobs()

    body = request.form.to_dict()
    transfer_code = (body.get("transfer_code") or "").strip()
    confirmation_code = (body.get("confirmation_code") or "").strip()
    cc_str = (body.get("cc") or "kr").strip().lower()

    if not transfer_code or not confirmation_code:
        return jsonify({"error": "이어하기 코드와 인증번호를 모두 입력해주세요"}), 400

    opts = _to_bool_opts(body)

    job_id = _create_job("code")
    executor.submit(
        _run_code_job, job_id, transfer_code, confirmation_code, cc_str, opts
    )

    return jsonify({"job_id": job_id, "queue_position": _get_queue_position(job_id)})


@app.route("/api/jobs/<job_id>", methods=["GET"])
def job_status(job_id):
    with JOBS_LOCK:
        job = JOBS.get(job_id)
        if job is None:
            return jsonify({"error": "존재하지 않는 작업입니다"}), 404
        status = job["status"]
        error = job["error"]

    if status == "queued":
        return jsonify(
            {
                "status": "queued",
                "queue_position": _get_queue_position(job_id),
                "max_concurrent": MAX_CONCURRENT,
            }
        )
    if status == "processing":
        return jsonify({"status": "processing"})
    if status == "error":
        return jsonify({"status": "error", "error": error["message"]}), error["status"]
    if status == "done":
        if job["kind"] == "file":
            return jsonify(
                {
                    "status": "done",
                    "kind": "file",
                    "download_url": f"/api/jobs/{job_id}/download",
                    "warnings": job["result"].get("warnings", []),
                }
            )
        return jsonify({"status": "done", "kind": "code", "result": job["result"]})

    return jsonify({"error": "알 수 없는 상태입니다"}), 500


@app.route("/api/jobs/<job_id>/download", methods=["GET"])
def job_download(job_id):
    with JOBS_LOCK:
        job = JOBS.get(job_id)
        if job is None or job["status"] != "done" or job["kind"] != "file":
            return jsonify({"error": "다운로드할 파일이 없습니다"}), 404
        file_bytes = job["result"]["file_bytes"]

    buf = io.BytesIO(file_bytes)
    buf.seek(0)
    return send_file(
        buf,
        as_attachment=True,
        download_name="SAVE_DATA_edited",
        mimetype="application/octet-stream",
    )


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000, debug=True)
