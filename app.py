import io
import traceback

from flask import Flask, request, send_file, jsonify, send_from_directory
from flask_cors import CORS

from bcsfe import core

# bcsfe 내부 데이터(로케일, 설정, 테마 등)를 사용하기 전에 반드시 초기화해야 함
core.core_data.init_data()

app = Flask(__name__, static_folder="static", static_url_path="")
CORS(app)

MAX_UPLOAD_MB = 5
app.config["MAX_CONTENT_LENGTH"] = MAX_UPLOAD_MB * 1024 * 1024


def load_save(raw_bytes: bytes) -> "core.SaveFile":
    """save 파일 바이트를 받아 bcsfe SaveFile 객체로 로드합니다."""
    data = core.Data(raw_bytes)
    # cc(국가 코드)는 자동 감지됨 (SaveFile.__init__ 내부 detect_cc 사용)
    save_file = core.SaveFile(data)
    return save_file


def apply_edits(save_file: "core.SaveFile", opts: dict):
    """opts(dict)에 담긴 옵션에 따라 세이브 파일을 수정합니다."""

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
        # 모든 종류의 캣프루트를 9999개로 채움
        save_file.catfruit = [9999 for _ in save_file.catfruit]

    if opts.get("unlock_all_cats"):
        level = opts.get("cat_level")
        want_true_form = bool(opts.get("true_form"))
        for cat in save_file.cats.get_all_cats():
            cat.unlock(save_file)
            if level:
                lv = max(int(level) - 1, 0)
                cat.upgrade.base = lv
                cat.upgrade.plus = 0
            if want_true_form:
                cat.true_form(save_file, set_current_form=False)

    return save_file


@app.route("/")
def index():
    return send_from_directory("static", "index.html")


@app.route("/api/edit", methods=["POST"])
def edit_save():
    if "save_file" not in request.files:
        return jsonify({"error": "save_file이 없습니다"}), 400

    upload = request.files["save_file"]
    raw_bytes = upload.read()

    if not raw_bytes:
        return jsonify({"error": "파일이 비어 있습니다"}), 400

    opts = request.form.to_dict()
    # 체크박스 값(on/off, true/false 문자열)을 boolean으로 변환
    for key in ("max_catfruit", "unlock_all_cats", "true_form"):
        if key in opts:
            opts[key] = opts[key] in ("true", "on", "1", True)

    try:
        save_file = load_save(raw_bytes)
        apply_edits(save_file, opts)
        out_data = save_file.to_data()
        out_bytes = bytes(out_data)
    except core.SaveError as e:
        return jsonify({"error": f"세이브 파일을 읽을 수 없습니다: {e}"}), 400
    except Exception:
        traceback.print_exc()
        return jsonify({"error": "세이브 파일 처리 중 오류가 발생했습니다. 파일 형식을 확인해주세요."}), 500

    buf = io.BytesIO(out_bytes)
    buf.seek(0)
    return send_file(
        buf,
        as_attachment=True,
        download_name="SAVE_DATA_edited",
        mimetype="application/octet-stream",
    )


@app.route("/api/edit-by-code", methods=["POST"])
def edit_save_by_code():
    """
    이어하기 코드 + 인증번호로 세이브를 서버에서 받아와 수정한 뒤,
    같은 계정에 다시 업로드하고 새 이어하기 코드/인증번호를 발급해서 돌려줍니다.

    주의: 이 코드/인증번호는 계정 전체에 대한 접근 권한과 동일합니다.
    본인 계정이 아닌 코드는 절대 입력/요청하지 마세요.
    """
    body = request.form.to_dict()
    transfer_code = (body.get("transfer_code") or "").strip()
    confirmation_code = (body.get("confirmation_code") or "").strip()
    cc_str = (body.get("cc") or "kr").strip().lower()

    if not transfer_code or not confirmation_code:
        return jsonify({"error": "이어하기 코드와 인증번호를 모두 입력해주세요"}), 400

    for key in ("max_catfruit", "unlock_all_cats", "true_form"):
        if key in body:
            body[key] = body[key] in ("true", "on", "1", True)

    try:
        cc = core.CountryCode(cc_str)
    except Exception:
        return jsonify({"error": "국가 코드가 올바르지 않습니다 (kr/en/jp/tw)"}), 400

    gv = core.GameVersion(120200)  # 이어하기 요청에는 정확한 버전이 중요하지 않음

    try:
        server_handler, result = core.ServerHandler.from_codes(
            transfer_code, confirmation_code, cc, gv, print=False
        )
    except Exception:
        traceback.print_exc()
        return jsonify({"error": "서버 요청 중 오류가 발생했습니다"}), 500

    if server_handler is None:
        return jsonify(
            {"error": "이어하기 코드 또는 인증번호가 올바르지 않습니다 (국가 코드도 확인해주세요)"}
        ), 400

    try:
        apply_edits(server_handler.save_file, body)
    except Exception:
        traceback.print_exc()
        return jsonify({"error": "세이브 수정 중 오류가 발생했습니다"}), 500

    try:
        codes = server_handler.get_codes()
    except Exception:
        traceback.print_exc()
        return jsonify({"error": "수정된 데이터를 서버에 업로드하는 중 오류가 발생했습니다"}), 500

    if codes is None:
        return jsonify({"error": "새 이어하기 코드 발급에 실패했습니다"}), 500

    new_transfer_code, new_confirmation_code = codes
    return jsonify(
        {
            "transfer_code": new_transfer_code,
            "confirmation_code": new_confirmation_code,
        }
    )


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000, debug=True)
